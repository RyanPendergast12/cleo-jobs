#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jobwatch_skills_export.py — "Needed Skills CSV" module for jobwatch.

Fully isolated from both jobwatch.py's dedup schema AND jobwatch_readiness.py's
tables: everything here lives in its own tables inside the shared jobwatch.db
file (skills_export_seen, skills_log, skills_agg, skills_export_meta), so this
module can never corrupt either of the other two schemas.

Reuses jobwatch_readiness.py's already-built-and-tested skill vocabulary,
category regexes, and skill-extraction regex (SKILLS_VOCAB, categorize(),
_extract_skills()) instead of re-implementing skill matching a second time.
That's the one intentional coupling this module has — everything else
(storage, dedup, CSV generation, posting) is fully separate.

What it does, once per run (call run_pass(raw_jobs) from jobwatch.py):
  1. For every raw job NOT already logged (its own dedup table, so re-runs of
     the same 2-hour scrape never double-count), extracts matched skills +
     category using jobwatch_readiness's vocabulary, and:
       - appends one row to skills_log (this job's title/company/source/
         category/matched skills/url) — the "one row per job posting" CSV
       - increments the (skill, category) counter in skills_agg — the
         "one row per skill, aggregated" CSV
  2. Once per calendar day (UTC), builds both CSVs from all-time data and
     posts them as file attachments to #needed-skills via
     NEEDED_SKILLS_WEBHOOK_URL, with a short embed summary (top skills,
     total jobs logged, categories covered).

Integration in jobwatch.py:
  1. import jobwatch_skills_export as jse
  2. NEEDED_SKILLS_WEBHOOK_URL env var read from os.environ
  3. jse.run_pass(raw, DB_PATH, NEEDED_SKILLS_WEBHOOK_URL, dry=dry) called
     once per pass, right alongside the existing jr.run_pass(...) call in
     pass_once() — same raw, pre-filter job list (whole scraped market, not
     just what survives keyword/remote/ghost filtering), for the same reason
     jr uses it: skill demand should reflect the real market, not your
     narrowed feed.

No new deps — uses only stdlib csv/io plus `requests`, which jobwatch.py
already depends on.
"""

import csv
import io
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests

import jobwatch_readiness as jr   # reuse SKILLS_VOCAB, categorize(), _extract_skills(), _fingerprint()

# ───────────────────────────── CONFIG ─────────────────────────────

# True = post the CSVs to Discord every pass. False = only once per UTC
# calendar day. Mirrors jr.POST_EVERY_RUN's toggle pattern; default here is
# False since a daily skills snapshot is plenty and keeps #needed-skills quiet.
POST_EVERY_RUN = False

TOP_SKILLS_IN_SUMMARY = 10   # how many skills to list in the Discord embed itself

# ───────────────────────────── STORAGE ─────────────────────────────

def _db(db_path="jobwatch.db"):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS skills_export_seen (
        fingerprint TEXT PRIMARY KEY
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS skills_log (
        fingerprint TEXT PRIMARY KEY,
        first_seen  TEXT,
        source      TEXT,
        title       TEXT,
        company     TEXT,
        category    TEXT,
        skills      TEXT,   -- semicolon-separated, e.g. "Splunk;AWS;Python"
        url         TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS skills_agg (
        skill    TEXT,
        category TEXT,
        job_count INTEGER DEFAULT 0,
        PRIMARY KEY (skill, category)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS skills_export_meta (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    con.commit()
    return con

# ───────────────────────────── INGEST ─────────────────────────────

def ingest(raw_jobs, db_path="jobwatch.db"):
    """Fold every NEWLY-seen raw job (pre-filter — the whole scraped market)
    into skills_log + skills_agg, once ever per unique job. Safe to call
    every pass. Jobs with no matched skills and no category still get logged
    (skills='' ) so skills_by_job.csv reflects the true total volume seen,
    not just the ones with hits."""
    con = _db(db_path)
    new_rows = 0
    for job in raw_jobs:
        fp = jr._fingerprint(job)
        if con.execute("SELECT 1 FROM skills_export_seen WHERE fingerprint=?", (fp,)).fetchone():
            continue
        con.execute("INSERT OR IGNORE INTO skills_export_seen (fingerprint) VALUES (?)", (fp,))

        title = getattr(job, "title", "") or ""
        text = f"{title} {getattr(job, 'description', '') or ''}"
        category = jr.categorize(title) or ""
        skills = sorted(jr._extract_skills(text))

        con.execute("""
            INSERT INTO skills_log (fingerprint, first_seen, source, title, company, category, skills, url)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(fingerprint) DO NOTHING
        """, (fp, datetime.now(timezone.utc).isoformat(), getattr(job, "source", ""),
              title, getattr(job, "company", ""), category, ";".join(skills), getattr(job, "url", "")))

        for skill in skills:
            con.execute("""
                INSERT INTO skills_agg (skill, category, job_count)
                VALUES (?,?,1)
                ON CONFLICT(skill, category) DO UPDATE SET job_count = job_count + 1
            """, (skill, category or "(uncategorized)"))

        new_rows += 1
    con.commit()
    if new_rows:
        print(f"  [SKILLS-EXPORT] logged {new_rows} newly-seen job(s)")
    con.close()

# ───────────────────────────── CSV GENERATION ─────────────────────────────

def build_aggregate_csv(db_path="jobwatch.db") -> bytes:
    """skill, category, job_count — all-time, one row per (skill, category) pair."""
    con = _db(db_path)
    rows = con.execute(
        "SELECT skill, category, job_count FROM skills_agg ORDER BY job_count DESC, skill ASC"
    ).fetchall()
    con.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["skill", "category", "job_count"])
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")

def build_by_job_csv(db_path="jobwatch.db") -> bytes:
    """One row per logged job posting: title, company, source, category, skills, url, first_seen."""
    con = _db(db_path)
    rows = con.execute(
        "SELECT title, company, source, category, skills, url, first_seen "
        "FROM skills_log ORDER BY first_seen DESC"
    ).fetchall()
    con.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["title", "company", "source", "category", "skills", "url", "first_seen"])
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")

def _overall_top_skills(db_path="jobwatch.db", n=TOP_SKILLS_IN_SUMMARY):
    """Collapse skills_agg across categories into overall totals for the embed summary."""
    con = _db(db_path)
    rows = con.execute("SELECT skill, job_count FROM skills_agg").fetchall()
    con.close()
    totals = Counter()
    for skill, count in rows:
        totals[skill] += count
    return totals.most_common(n)

def _total_jobs_logged(db_path="jobwatch.db") -> int:
    con = _db(db_path)
    (n,) = con.execute("SELECT COUNT(*) FROM skills_log").fetchone()
    con.close()
    return n

# ───────────────────────────── DISCORD ─────────────────────────────

def post_csvs(webhook_url, db_path="jobwatch.db", dry=False) -> bool:
    """Returns True only if both CSVs were actually delivered to Discord.
    Same 'only mark posted if truly delivered' contract as jr.post_octagram,
    for the same reason: a misconfigured webhook shouldn't permanently skip
    the day even after being fixed."""
    agg_csv = build_aggregate_csv(db_path)
    job_csv = build_by_job_csv(db_path)
    top_skills = _overall_top_skills(db_path)
    total_jobs = _total_jobs_logged(db_path)

    top_lines = "\n".join(f"{i+1}. {skill} — {count}" for i, (skill, count) in enumerate(top_skills)) or "no data yet"

    embed = {
        "title": "📄 Needed Skills Export",
        "description": "All-time skill demand across every scraped job posting, before keyword/remote filtering.",
        "color": 0x3ba55d,
        "fields": [
            {"name": f"Top {len(top_skills)} skills overall", "value": top_lines, "inline": False},
            {"name": "Jobs logged (all-time)", "value": str(total_jobs), "inline": True},
        ],
        "footer": {"text": datetime.now(timezone.utc).strftime("Updated %Y-%m-%d %H:%M UTC")},
    }

    if dry:
        print("  [SKILLS-EXPORT] --dry mode — would post CSVs (not marking as posted):")
        print(f"     total jobs logged: {total_jobs}")
        for skill, count in top_skills:
            print(f"     {skill}: {count}")
        return False
    if not webhook_url or not webhook_url.startswith("http"):
        print("  [SKILLS-EXPORT] NEEDED_SKILLS_WEBHOOK_URL not set/invalid — skipping post "
              "(will retry next run, NOT marked as posted today)")
        return False

    try:
        resp = requests.post(
            webhook_url,
            data={"payload_json": json.dumps({"username": "JobWatch", "embeds": [embed]})},
            files={
                "file1": ("skills_aggregate.csv", agg_csv, "text/csv"),
                "file2": ("skills_by_job.csv", job_csv, "text/csv"),
            },
            timeout=30,
        )
        if resp.status_code == 429:
            import time
            time.sleep(float(resp.json().get("retry_after", 5)))
            return False
        elif not resp.ok:
            print(f"  [SKILLS-EXPORT] webhook error {resp.status_code}: {resp.text[:200]} — NOT marked as posted")
            return False
        else:
            print("  [SKILLS-EXPORT] posted both CSVs")
            return True
    except Exception as e:
        print(f"  [SKILLS-EXPORT] post error: {e} — NOT marked as posted")
        return False

# ───────────────────────────── ORCHESTRATION ─────────────────────────────

def _already_posted_today(con):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = con.execute("SELECT value FROM skills_export_meta WHERE key='last_posted'").fetchone()
    return row is not None and row[0] == today

def _mark_posted_today(con):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con.execute("INSERT INTO skills_export_meta (key, value) VALUES ('last_posted', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today,))
    con.commit()

def maybe_post(webhook_url, db_path="jobwatch.db", dry=False, force=False):
    con = _db(db_path)
    if not POST_EVERY_RUN and not force and not dry and _already_posted_today(con):
        print("  [SKILLS-EXPORT] already posted today — skipping (pass FORCE_SKILLS_EXPORT=1 to override)")
        con.close()
        return
    delivered = post_csvs(webhook_url, db_path=db_path, dry=dry)
    if delivered:
        _mark_posted_today(con)
    con.close()

def run_pass(raw_jobs, db_path="jobwatch.db", webhook_url="", dry=False, force=False):
    """Convenience wrapper called from jobwatch.py once per pass with the FULL
    pre-filter raw job list — same list jr.run_pass gets, for the same reason:
    skill demand should reflect the whole scraped market, not your narrowed feed."""
    ingest(raw_jobs, db_path=db_path)
    maybe_post(webhook_url, db_path=db_path, dry=dry, force=force)

