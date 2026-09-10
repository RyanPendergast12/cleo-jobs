#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jobwatch_pipeline.py — real application traction for Cleo's readiness octagram.

The readiness octagram (jobwatch_readiness.py) answers "do my skills match what
postings ask for?". This module adds the other half: "am I actually getting
callbacks?" — pulled from the hand-maintained application ledger (the
"THE END GAME MY JOBS" tab of the MUH JOBS Google Sheet).

The sheet is read through a token-guarded Apps Script web app (deployed from
Extensions -> Apps Script on the sheet), so no Google Cloud project, OAuth, or
service-account key is involved. Each ledger row is classified into its furthest
funnel stage and rolled up per octagram category; jobwatch_readiness overlays
the result as a second polygon.

Fully isolated from the other modules' schemas — its own tables
(pipeline_snapshot, pipeline_meta) inside the shared jobwatch.db.

Wiring in jobwatch.py (already patched if you're reading this):
    import jobwatch_pipeline as jp
    jp.run_pass(DB_PATH, dry=dry)     # once per pass, before jr.run_pass(...)

Config — both required, else this module is a silent no-op:
    GSHEET_WEBAPP_URL     the /exec URL of the deployed Apps Script web app
    GSHEET_WEBAPP_TOKEN   the shared secret the script checks against ?token=

Classification reads the ledger's Status column plus the Call? / Interview? /
Offer? columns. Reaching a recruiter/phone screen or beyond
counts as success even if the role later ended in rejection. Keep those columns
current — free-text Notes are deliberately NOT parsed (too noisy to trust).

No extra dependencies: stdlib + requests (already a jobwatch dep).
"""

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import requests

from jobwatch_readiness import CATEGORY_ORDER, categorize

WEBAPP_URL = os.environ.get("GSHEET_WEBAPP_URL", "")
WEBAPP_TOKEN = os.environ.get("GSHEET_WEBAPP_TOKEN", "")

MIN_SAMPLE = 3          # resolved (success + miss) apps a category needs before
                        # its traction rate is shown instead of "n/a"
FETCH_TIMEOUT = 20

# Status values (lower-cased) meaning "reached a human screen or further".
_SUCCESS_STATUS = {
    "call", "phone screen", "phone call", "recruiter", "recruiter screen",
    "screen", "screening", "interview", "interviewing", "interviews",
    "onsite", "on-site", "onsite round", "final", "final round",
    "offer", "offered", "accepted", "hired",
}
# Status values meaning "ended without reaching a screen".
_MISS_STATUS = {
    "rejection", "rejected", "reject", "declined", "closed", "no",
    "ghosted", "ghost", "withdrawn", "not selected",
}
_YES = {"yes", "y", "true", "1", "x", "✓", "✔"}


def _clean_row(raw: dict) -> dict:
    """Strip header whitespace ('Call? ' -> 'Call?') and drop blank-key columns."""
    out = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if name:
            out[name] = value
    return out


def _yes(row: dict, *names) -> bool:
    return any(str(row.get(n, "")).strip().lower() in _YES for n in names)


def classify(row: dict) -> str:
    """Return 'success' | 'miss' | 'pending' for one ledger row.

    'success' wins over a later rejection: reaching a recruiter/interview is the
    signal we care about, regardless of the eventual outcome.
    """
    row = _clean_row(row)
    status = str(row.get("Status", "")).strip().lower()
    if status in _SUCCESS_STATUS or _yes(row, "Call?", "Interview?", "Offer?"):
        return "success"
    if status in _MISS_STATUS:
        return "miss"
    return "pending"


# Typos seen in the live ledger, fixed before fallback matching.
_TYPO_FIX = {"administratior": "administrator", "cooridnator": "coordinator", "healtcare": "healthcare"}

# More permissive than jobwatch_readiness.categorize() (which stays strict for
# market aggregation). Only consulted when the strict match returns None. Order
# matters: more specific healthcare and HR tracks are evaluated before general
# administrative operations.
_FALLBACK = [(name, re.compile(pat, re.I)) for name, pat in [
    ("Patient Access", r"patient\s+(?:access|services|registration)|medical\s+reception"),
    ("Medical Office", r"medical\s+office|clinic|front\s+office"),
    ("Care Coordination", r"care\s+coordin|referral|intake"),
    ("Health Administration", r"healthcare\s+administr|health\s+services|health\s+information"),
    ("Human Resources", r"human\s+resources|\bhr\b|people\s+operations|benefits"),
    ("Public Health Programs", r"public\s+health|health\s+education|community\s+health"),
    ("Outreach and Resources", r"outreach|community\s+engagement|resource"),
    ("Administrative Operations", r"administrat|office\s+coordin|operations|executive\s+assistant"),
]]


def _category(row: dict):
    """'Category' column wins; then strict categorize(); then a permissive
    keyword fallback over Position + Industry + Role; then None."""
    override = str(row.get("Category", "")).strip()
    if override in CATEGORY_ORDER:
        return override
    position = str(row.get("Position", ""))
    strict = categorize(position)
    if strict:
        return strict
    haystack = " ".join(str(row.get(k, "")) for k in ("Position", "Industry", "Role")).lower()
    for typo, fix in _TYPO_FIX.items():
        haystack = haystack.replace(typo, fix)
    for name, pattern in _FALLBACK:
        if pattern.search(haystack):
            return name
    return None


def _empty_bucket() -> dict:
    return {"applied": 0, "success": 0, "miss": 0, "pending": 0, "rate": None}


def rollup(rows) -> dict:
    """Classify every row and aggregate per octagram category.

    Returns {"categories": {name: bucket}, "unclassified": bucket,
             "rows_total": int, "reached_total": int} where each bucket is
    {applied, success, miss, pending, rate} and rate is a 0-100 float or None
    when fewer than MIN_SAMPLE applications have resolved.
    """
    categories = {name: _empty_bucket() for name in CATEGORY_ORDER}
    unclassified = _empty_bucket()

    for raw in rows:
        row = _clean_row(raw)
        if not str(row.get("Position", "")).strip() and not str(row.get("Company", "")).strip():
            continue  # blank trailing row
        name = _category(row)
        bucket = categories[name] if name in categories else unclassified
        outcome = classify(row)
        bucket["applied"] += 1
        bucket[outcome] += 1

    for bucket in list(categories.values()) + [unclassified]:
        resolved = bucket["success"] + bucket["miss"]
        bucket["rate"] = round(100 * bucket["success"] / resolved, 1) if resolved >= MIN_SAMPLE else None

    return {
        "categories": categories,
        "unclassified": unclassified,
        "rows_total": sum(b["applied"] for b in categories.values()) + unclassified["applied"],
        "reached_total": sum(b["success"] for b in categories.values()) + unclassified["success"],
    }


def fetch_rows(url: str = "", token: str = "", timeout: int = FETCH_TIMEOUT):
    """GET the ledger JSON from the Apps Script web app. Returns list[dict]."""
    url = url or WEBAPP_URL
    token = token or WEBAPP_TOKEN
    if not url or not token:
        raise RuntimeError("GSHEET_WEBAPP_URL / GSHEET_WEBAPP_TOKEN not set")
    resp = requests.get(url, params={"token": token}, timeout=timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"ledger endpoint did not return JSON (starts: {resp.text[:120]!r})"
        ) from exc
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"ledger endpoint error: {data['error']}")
    if not isinstance(data, list):
        raise RuntimeError(f"ledger endpoint returned {type(data).__name__}, expected a list")
    return data


# ───────────────────────────── STORAGE ─────────────────────────────

def _db(db_path: str):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS pipeline_snapshot (
        day           TEXT PRIMARY KEY,
        taken_at      TEXT,
        rows_total    INTEGER,
        reached_total INTEGER,
        payload       TEXT
    )""")
    con.execute("CREATE TABLE IF NOT EXISTS pipeline_meta (key TEXT PRIMARY KEY, value TEXT)")
    return con


def store_snapshot(db_path: str, roll: dict) -> None:
    now = datetime.now(timezone.utc)
    with closing(_db(db_path)) as con, con:
        con.execute(
            "INSERT INTO pipeline_snapshot (day, taken_at, rows_total, reached_total, payload) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET taken_at=excluded.taken_at, "
            "rows_total=excluded.rows_total, reached_total=excluded.reached_total, "
            "payload=excluded.payload",
            (now.strftime("%Y-%m-%d"), now.isoformat(),
             roll["rows_total"], roll["reached_total"], json.dumps(roll)),
        )


def load_traction(db_path: str):
    """Most recent stored rollup, or None. Consumed by jobwatch_readiness."""
    try:
        with closing(_db(db_path)) as con:
            row = con.execute(
                "SELECT payload FROM pipeline_snapshot ORDER BY day DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    return json.loads(row[0]) if row else None


# ───────────────────────────── ORCHESTRATION ─────────────────────────────

def run_pass(db_path: str = "jobwatch.db", dry: bool = False) -> None:
    """Called once per jobwatch pass, before jobwatch_readiness.run_pass().

    Non-fatal by contract: any failure just leaves the previous snapshot in
    place (or none), and the octagram falls back to market-fit only.
    """
    if not WEBAPP_URL or not WEBAPP_TOKEN:
        return  # feature not configured

    try:
        rows = fetch_rows()
    except Exception as exc:  # network, auth, bad payload — all non-fatal
        print(f"  [PIPELINE] ledger fetch failed ({exc}); readiness uses market fit only")
        return

    roll = rollup(rows)
    if not dry:
        store_snapshot(db_path, roll)

    unclassified = roll["unclassified"]["applied"]
    print(f"  [PIPELINE] {roll['rows_total']} application(s) tracked, "
          f"{roll['reached_total']} reached recruiter/interview"
          + (f"; {unclassified} unclassified" if unclassified else ""))
    scored = [
        f"{name} {bucket['rate']:.0f}% ({bucket['success']}/{bucket['success'] + bucket['miss']})"
        for name, bucket in roll["categories"].items() if bucket["rate"] is not None
    ]
    if scored:
        print("     traction by category: " + " · ".join(scored))


if __name__ == "__main__":
    import sys
    _dry = "--dry" in sys.argv
    _rows = fetch_rows()
    _roll = rollup(_rows)
    print(json.dumps(_roll, indent=2))
    if not _dry:
        store_snapshot("jobwatch.db", _roll)
