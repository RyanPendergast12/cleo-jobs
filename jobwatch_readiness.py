#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jobwatch_readiness.py — Cleo's "Job Readiness Octagram" module.

Fully isolated from jobwatch.py's dedup schema: everything here lives in its
own tables inside the shared jobwatch.db file (market_history, readiness_seen,
readiness_meta), so this module can never corrupt the main `seen` dedup table.

What it does, once per run (call run_pass(raw_jobs) from jobwatch.py):
  1. Categorizes every raw job (pre-filter, so it reflects the WHOLE scraped
     market, not just what survives your keyword/remote/ghost filters) into
     one of 8 fixed categories.
  2. Records skill demand, YoE requirements, degree signals, and licensure
     signals from each *newly seen* job into market_history (all-time —
     each unique job counted once, ever, via its own dedup table so re-runs
     of the same 2-hour scrape don't inflate the averages).
  3. Once per calendar day, scores your resume.json against the all-time
     aggregated market data for each of the 8 categories, renders an 8-axis
     "octagram" radar chart PNG, and posts it to #job-readiness.

Integration in jobwatch.py (already wired if you're reading this after I
patched it):
  1. import jobwatch_readiness as jr
  2. JOB_READINESS_WEBHOOK_URL env var read from os.environ
  3. jr.run_pass(raw, DB_PATH, JOB_READINESS_WEBHOOK_URL, dry=dry) called once
     per pass, right after `raw` is collected in pass_once()

Deps: pip install matplotlib   (radar chart rendering — pure-Python, no
      system packages needed, same tier of dependency as `staticmap`)
"""

import io
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests

# ───────────────────────────── CONFIG ─────────────────────────────

RESUME_JSON_PATH = "resume.json"

# The 8 fixed octagram spokes, in display order (12 o'clock, then clockwise).
# Each is (display_name, title-match regex). ORDER MATTERS: more specific
# patterns are checked first so specific healthcare tracks do not fall through
# to general administrative roles.
CATEGORIES = [
    ("Patient Access", re.compile(r"patient\s+(?:access|services|registration)|medical\s+reception", re.I)),
    ("Medical Office", re.compile(r"medical\s+office|clinic\s+coordinator|front\s+office", re.I)),
    ("Care Coordination", re.compile(r"care\s+coordinator|referral\s+coordinator|intake\s+coordinator", re.I)),
    ("Health Administration", re.compile(r"health(?:care| services)?\s+(?:administrator|coordinator)|health\s+information", re.I)),
    ("Human Resources", re.compile(r"human\s+resources|\bhr\b|people\s+operations", re.I)),
    ("Administrative Operations", re.compile(r"administrative\s+(?:assistant|coordinator)|office\s+coordinator|operations\s+coordinator|executive\s+assistant", re.I)),
    ("Public Health Programs", re.compile(r"public\s+health|health\s+education|community\s+health", re.I)),
    ("Outreach and Resources", re.compile(r"outreach\s+coordinator|community\s+outreach|resource\s+coordinator|program\s+coordinator", re.I)),
]
CATEGORY_ORDER = [c[0] for c in CATEGORIES]

# Reasonable priors for "years of experience typically required," used only
# until market_history has real observed data for that category, and blended
# in afterward as a floor so one weird posting can't swing a category wildly.
CATEGORY_YOE_PRIOR = {
    "Patient Access": 1.0,
    "Medical Office": 1.0,
    "Care Coordination": 1.5,
    "Health Administration": 1.5,
    "Human Resources": 1.5,
    "Administrative Operations": 1.5,
    "Public Health Programs": 2.0,
    "Outreach and Resources": 1.5,
}

# Canonical skill name -> list of surface forms to match (case-insensitive,
# word-boundary guarded so "SPL" doesn't fire inside "split").
_SKILL_SURFACE_FORMS = {
    "Patient Scheduling": ["patient scheduling", "appointment scheduling"],
    "Patient Intake": ["patient intake", "patient registration"],
    "Insurance Verification": ["insurance verification", "benefits verification"],
    "Referral Verification": ["referral verification", "referral authorization"],
    "Patient Communication": ["patient communication", "patient-facing"],
    "Provider Communication": ["provider communication", "physician communication"],
    "Medical Office Operations": ["medical office", "clinic operations", "front office"],
    "Phreesia": ["phreesia"],
    "Vitals": ["vital signs", "patient vitals"],
    "Specimen Handling": ["specimen handling", "specimen collection", "blood samples"],
    "Onboarding": ["onboarding", "new hire orientation"],
    "Offboarding": ["offboarding", "employee separation"],
    "HR Records": ["hr records", "personnel records", "employee records"],
    "Compliance Documentation": ["compliance documentation", "compliance records"],
    "Records Auditing": ["records auditing", "data audits", "audit records"],
    "Data Entry": ["data entry"],
    "Workday": ["workday"],
    "Oracle": ["oracle hcm", "oracle hr", "oracle"],
    "ADP": ["\\badp\\b"],
    "Employee Relations": ["employee relations"],
    "Benefits": ["benefits administration", "employee benefits"],
    "Employee Wellness": ["employee wellness", "wellness programs"],
    "Expense Management": ["expense management", "expense reports", "expense reimbursement"],
    "Travel Coordination": ["travel coordination", "corporate travel", "travel arrangements"],
    "Customer Service": ["customer service", "client service", "guest service"],
    "POS": ["point of sale", "pos system"],
    "Payment Processing": ["payment processing", "card transactions", "cash handling"],
    "Social Media": ["social media", "content creation"],
    "Email Marketing": ["email marketing", "email campaigns"],
    "Community Outreach": ["community outreach", "community engagement"],
    "Crisis Hotline Support": ["crisis hotline", "crisis support"],
    "Resource Coordination": ["resource coordination", "resource referrals", "community resources"],
    "HIPAA": ["hipaa"],
    "EHR": ["electronic health record", "electronic medical record", "\\behr\\b", "\\bemr\\b"],
    "Medical Terminology": ["medical terminology"],
    "Microsoft Office": ["microsoft office", "microsoft 365", "excel", "outlook"],
}

def _compile_skill(forms):
    alt = "|".join(forms)
    return re.compile(r"(?<![a-z0-9])(?:" + alt + r")(?![a-z0-9])", re.I)

SKILLS_VOCAB = {name: _compile_skill(forms) for name, forms in _SKILL_SURFACE_FORMS.items()}

_DEGREE_MASTERS_RE = re.compile(r"master'?s\s+degree|\bm\.?s\.?\s+in\b", re.I)
_DEGREE_BACHELOR_RE = re.compile(r"bachelor'?s\s+degree|\bb\.?s\.?\s+in\b", re.I)
_CLEARANCE_RE = re.compile(
    r"registered nurse|licensed practical nurse|nurse practitioner|physician assistant|"
    r"certified medical assistant|cma certification|cpr certification|bls certification|"
    r"cpc certification|certified professional coder",
    re.I,
)
_YOE_RE = re.compile(r"(\d+)\+?\s*(?:-\s*\d+\+?\s*)?years?\s*(?:of\s+)?(?:experience|exp\.?)\b", re.I)

# Octagram scoring weights — must sum to 1.0
W_SKILLS, W_YOE, W_DEGREE, W_CLEARANCE = 0.45, 0.25, 0.15, 0.15

TOP_N_SKILLS_PER_CATEGORY = 15   # how many most-in-demand skills define "full marks"

# True = post the octagram to Discord every pass (every 2 hours per your cron).
# False = only once per UTC calendar day (the original design). Flip this back
# to False later if #job-readiness gets too noisy at every-2-hours cadence.
POST_EVERY_RUN = True

# ───────────────────────────── RESUME ─────────────────────────────

def load_resume(json_path=RESUME_JSON_PATH):
    """Load {'years', 'degree_level', 'advanced_degree_in_progress', 'clearance', 'skills'}
    from the resume.json sidecar. This is the only supported path — no PDF parsing —
    since resume formatting changes over time and the sidecar is a stable contract."""
    if not (json_path and os.path.exists(json_path)):
        print(f"[readiness] {json_path} not found — readiness scoring disabled this run")
        return None
    try:
        data = json.load(open(json_path, "r", encoding="utf-8"))
        return {
            "years": float(data.get("years", 0) or 0),
            "degree_level": data.get("degree_level", "BS"),
            "advanced_degree_in_progress": bool(data.get("advanced_degree_in_progress", False)),
            "clearance": data.get("clearance"),
            "skills": set(data.get("skills", [])),
        }
    except Exception as e:
        print(f"[readiness] resume.json present but unreadable ({e}) — readiness scoring disabled this run")
        return None

# ───────────────────────────── CATEGORIZATION / EXTRACTION ─────────────────────────────

def categorize(title):
    for name, pat in CATEGORIES:
        if pat.search(title or ""):
            return name
    return None

def _extract_skills(text):
    return {name for name, pat in SKILLS_VOCAB.items() if pat.search(text or "")}

def _extract_yoe(text):
    m = _YOE_RE.search(text or "")
    return float(m.group(1)) if m else None

# ───────────────────────────── STORAGE ─────────────────────────────

def _db(db_path="jobwatch.db"):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS market_history (
        category      TEXT PRIMARY KEY,
        job_count     INTEGER DEFAULT 0,
        skills_json   TEXT DEFAULT '{}',
        yoe_sum       REAL DEFAULT 0,
        yoe_count     INTEGER DEFAULT 0,
        masters_count INTEGER DEFAULT 0,
        bachelor_count INTEGER DEFAULT 0,
        clearance_count INTEGER DEFAULT 0
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS readiness_seen (
        fingerprint TEXT PRIMARY KEY
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS readiness_meta (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    con.commit()
    return con

def _fingerprint(job):
    # Reuses the same identity notion as jobwatch.py's Job.fingerprint() but
    # computed independently here so this module has zero import dependency
    # on jobwatch.py's Job dataclass (keeps it usable/testable standalone).
    import hashlib
    key = f"{(getattr(job, 'company', '') or '').strip().lower()}|" \
          f"{(getattr(job, 'title', '') or '').strip().lower()}|" \
          f"{(getattr(job, 'url', '') or '').strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()

def ingest(raw_jobs, db_path="jobwatch.db"):
    """Fold every NEWLY-seen raw job (pre-filter — the whole scraped market)
    into market_history, once ever per unique job. Safe to call every pass."""
    con = _db(db_path)
    by_cat = defaultdict(list)
    new_count = 0
    for job in raw_jobs:
        fp = _fingerprint(job)
        if con.execute("SELECT 1 FROM readiness_seen WHERE fingerprint=?", (fp,)).fetchone():
            continue
        con.execute("INSERT OR IGNORE INTO readiness_seen (fingerprint) VALUES (?)", (fp,))
        new_count += 1
        title = getattr(job, "title", "") or ""
        cat = categorize(title)
        if cat:
            by_cat[cat].append(job)

    for cat, jobs in by_cat.items():
        row = con.execute(
            "SELECT job_count, skills_json, yoe_sum, yoe_count, masters_count, bachelor_count, clearance_count "
            "FROM market_history WHERE category=?", (cat,)
        ).fetchone()
        if row:
            job_count, skills_json, yoe_sum, yoe_count, masters_count, bachelor_count, clearance_count = row
            skills = Counter(json.loads(skills_json))
        else:
            job_count, yoe_sum, yoe_count, masters_count, bachelor_count, clearance_count = 0, 0.0, 0, 0, 0, 0
            skills = Counter()

        for job in jobs:
            text = f"{getattr(job, 'title', '') or ''} {getattr(job, 'description', '') or ''}"
            job_count += 1
            skills.update(_extract_skills(text))
            yoe = _extract_yoe(text)
            if yoe is not None:
                yoe_sum += yoe
                yoe_count += 1
            if _DEGREE_MASTERS_RE.search(text):
                masters_count += 1
            if _DEGREE_BACHELOR_RE.search(text):
                bachelor_count += 1
            if _CLEARANCE_RE.search(text):
                clearance_count += 1

        con.execute("""
            INSERT INTO market_history
                (category, job_count, skills_json, yoe_sum, yoe_count, masters_count, bachelor_count, clearance_count)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(category) DO UPDATE SET
                job_count=excluded.job_count, skills_json=excluded.skills_json,
                yoe_sum=excluded.yoe_sum, yoe_count=excluded.yoe_count,
                masters_count=excluded.masters_count, bachelor_count=excluded.bachelor_count,
                clearance_count=excluded.clearance_count
        """, (cat, job_count, json.dumps(dict(skills)), yoe_sum, yoe_count,
              masters_count, bachelor_count, clearance_count))
    con.commit()
    if new_count:
        print(f"  [READINESS] ingested {new_count} newly-seen job(s) across {len(by_cat)} matched categor(y/ies)")
    con.close()

# ───────────────────────────── SCORING ─────────────────────────────

def compute_readiness(resume, db_path="jobwatch.db"):
    """Return {category: {'score': 0-100, 'job_count': N, 'top_missing': [...],
    'avg_yoe_required': float}} for all 8 fixed categories, all-time."""
    con = _db(db_path)
    results = {}
    for cat in CATEGORY_ORDER:
        row = con.execute(
            "SELECT job_count, skills_json, yoe_sum, yoe_count, masters_count, bachelor_count, clearance_count "
            "FROM market_history WHERE category=?", (cat,)
        ).fetchone()

        if not row or row[0] == 0:
            # No market data yet for this category — neutral/unscored midpoint.
            results[cat] = {"score": 50.0, "job_count": 0, "top_missing": [], "avg_yoe_required": CATEGORY_YOE_PRIOR[cat]}
            continue

        job_count, skills_json, yoe_sum, yoe_count, masters_count, bachelor_count, clearance_count = row
        demand = Counter(json.loads(skills_json))
        top_skills = [s for s, _ in demand.most_common(TOP_N_SKILLS_PER_CATEGORY)]

        # Skill score: weighted by how often each top skill is demanded.
        total_weight = sum(demand[s] for s in top_skills) or 1
        matched_weight = sum(demand[s] for s in top_skills if s in resume["skills"])
        skill_score = matched_weight / total_weight
        missing = [s for s in top_skills if s not in resume["skills"]][:5]

        # YoE score: blend observed market avg with the category prior so one
        # noisy posting can't swing things; ratio capped at 1.0 (more YoE than
        # required is full marks, not a penalty).
        prior = CATEGORY_YOE_PRIOR[cat]
        observed_avg = (yoe_sum / yoe_count) if yoe_count else prior
        avg_required = (observed_avg + prior) / 2 if yoe_count else prior
        yoe_score = min(resume["years"] / avg_required, 1.0) if avg_required > 0 else 1.0

        # Degree score: bachelor's alone is fully satisfied. If a meaningful share
        # of postings specifically call for a master's, give partial credit since
        # the M.S. is in progress rather than complete.
        masters_share = masters_count / job_count
        if masters_share > 0.35 and not resume.get("advanced_degree_in_progress"):
            degree_score = 0.6
        elif masters_share > 0.35:
            degree_score = 0.85   # in-progress credit
        else:
            degree_score = 1.0

        # Licensure score: only reduced when a meaningful share of postings in this
        # category ask for a credential not documented in the resume sidecar.
        clearance_share = clearance_count / job_count
        if clearance_share > 0.15 and not resume.get("clearance"):
            clearance_score = max(1.0 - clearance_share, 0.3)
        else:
            clearance_score = 1.0

        overall = (W_SKILLS * skill_score + W_YOE * yoe_score +
                   W_DEGREE * degree_score + W_CLEARANCE * clearance_score) * 100

        results[cat] = {
            "score": round(overall, 1),
            "job_count": job_count,
            "top_missing": missing,
            "avg_yoe_required": round(avg_required, 1),
        }
    con.close()
    return results

# ───────────────────────────── OCTAGRAM RENDERING ─────────────────────────────

def render_octagram(results, traction=None) -> bytes:
    """Render the 8-axis octagram as a PNG (dark theme, matches Discord embed color).

    When `traction` (a jobwatch_pipeline rollup) is given, a second dashed
    polygon shows the real recruiter/interview callback rate per category —
    only across axes with at least MIN_SAMPLE resolved applications.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    labels = CATEGORY_ORDER
    values = [results[c]["score"] for c in labels]
    n = len(labels)

    t_rates = None
    if traction:
        t_cats = traction.get("categories", {})
        t_rates = [
            (t_cats.get(c, {}).get("rate") if isinstance(t_cats.get(c), dict) else None)
            for c in labels
        ]
        if not any(r is not None for r in t_rates):
            t_rates = None

    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    values_closed = values + values[:1]
    angles_closed = angles + angles[:1]

    fig = plt.figure(figsize=(9, 9), dpi=140, facecolor="#0b0e14")
    ax = fig.add_subplot(111, polar=True, facecolor="#0b0e14")
    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)

    ax.set_ylim(0, 100)
    ax.set_yticks([25, 50, 75, 100])
    ax.set_yticklabels(["25%", "50%", "75%", "100%"], color="#8892a0", fontsize=8)
    ax.set_xticks(angles)
    ax.set_xticklabels([])  # custom labels placed manually below for better wrapping
    ax.grid(color="#2a2f3a", linewidth=0.8)
    ax.spines["polar"].set_color("#2a2f3a")

    ax.plot(angles_closed, values_closed, color="#7c5cff", linewidth=2.2, label="Market fit (skills)")
    ax.fill(angles_closed, values_closed, color="#7c5cff", alpha=0.25)
    ax.scatter(angles, values, color="#7c5cff", s=45, zorder=5, edgecolors="white", linewidths=0.8)

    # Traction overlay: a green diamond on each axis at that category's
    # recruiter/interview callback rate, with a thin radial stem. No connecting
    # polygon — too few categories usually clear MIN_SAMPLE for a shape to read.
    if t_rates is not None:
        real = [(ang, r) for ang, r in zip(angles, t_rates) if r is not None]
        for ang, r in real:
            ax.plot([ang, ang], [0, r], color="#3ddc97", linewidth=1.3, alpha=0.55, zorder=4)
        ax.scatter([a for a, _ in real], [r for _, r in real], color="#3ddc97",
                   s=80, marker="D", zorder=6, edgecolors="#0b0e14", linewidths=1.1,
                   label="Recruiter/interview callback rate")
        for ang, r in real:
            ax.text(ang, min(r + 9, 96), f"{r:.0f}%", ha="center", va="center",
                    color="#3ddc97", fontsize=8.5, fontweight="bold", zorder=7)
        ax.legend(loc="upper left", bbox_to_anchor=(-0.18, 1.08), frameon=False,
                  labelcolor="white", fontsize=9)

    label_suffix = " fit" if t_rates is not None else ""
    for angle, label, val in zip(angles, labels, values):
        ax.text(angle, 118, f"{label}\n{val:.0f}%{label_suffix}", ha="center", va="center",
                color="white", fontsize=9.5, fontweight="bold", linespacing=1.4)

    fig.suptitle("Job Readiness Octagram", color="white", fontsize=17, fontweight="bold", y=0.98)
    # The market-fit-only view keeps its subtitle; with the traction overlay the
    # top-centre space is taken by the first category label, so the
    # legend (top-left) carries the explanation instead.
    if t_rates is None:
        fig.text(0.5, 0.945, "Your Experience Match vs. Avg. Job Requirements",
                 color="#8892a0", fontsize=10, ha="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight", pad_inches=0.5)
    plt.close(fig)
    buf.seek(0)
    return buf.read()

# ───────────────────────────── DISCORD ─────────────────────────────

def _summary_fields(results, traction=None):
    ranked = sorted(results.items(), key=lambda kv: kv[1]["score"])
    weakest = ranked[0]
    strongest = ranked[-1]
    fields = [
        {"name": "Strongest fit", "value": f"{strongest[0]} — {strongest[1]['score']:.0f}%", "inline": True},
        {"name": "Weakest fit", "value": f"{weakest[0]} — {weakest[1]['score']:.0f}%", "inline": True},
    ]
    if weakest[1]["top_missing"]:
        fields.append({"name": f"Top gaps for {weakest[0]}", "value": ", ".join(weakest[1]["top_missing"]), "inline": False})
    total_jobs = sum(r["job_count"] for r in results.values())
    fields.append({"name": "Jobs aggregated (all-time)", "value": str(total_jobs), "inline": True})
    if traction:
        cats = traction.get("categories", {})
        scored = [c for c in CATEGORY_ORDER
                  if isinstance(cats.get(c), dict) and cats[c].get("rate") is not None]
        reached, total = traction.get("reached_total", 0), traction.get("rows_total", 0)
        if total:
            fields.append({
                "name": "Traction (from ledger)",
                "value": f"{reached}/{total} reached recruiter/interview"
                         + (f" · scored: {', '.join(scored)}" if scored else " · no category has 3+ resolved yet"),
                "inline": False,
            })
    return fields

def post_octagram(results, webhook_url, dry=False, traction=None):
    """Returns True only if the octagram was actually delivered to Discord.
    Returns False for dry runs, missing webhooks, or send failures — callers
    must NOT mark the day as 'posted' unless this returns True, or a day where
    the webhook was misconfigured gets permanently skipped even after the fix."""
    png = render_octagram(results, traction=traction)
    description = ("Cleo's all-time resume match vs. aggregated market requirements across 8 target categories."
                  if not traction else
                  "Skills vs. market requirements (purple) overlaid with your real recruiter/interview "
                  "callback rate from the application ledger (green).")
    embed = {
        "title": "📊 Job Readiness Octagram",
        "description": description,
        "color": 0x7c5cff,
        "image": {"url": "attachment://octagram.png"},
        "fields": _summary_fields(results, traction=traction),
        "footer": {"text": datetime.now(timezone.utc).strftime("Updated %Y-%m-%d %H:%M UTC")},
    }
    if dry:
        print("  [READINESS] --dry mode — would post octagram (not marking as posted):")
        for cat in CATEGORY_ORDER:
            print(f"     {cat}: {results[cat]['score']:.0f}%  ({results[cat]['job_count']} jobs)")
        return False
    if not webhook_url or not webhook_url.startswith("http"):
        print("  [READINESS] JOB_READINESS_WEBHOOK_URL not set/invalid — skipping post "
              "(will retry next run, NOT marked as posted today)")
        return False
    try:
        resp = requests.post(
            webhook_url,
            data={"payload_json": json.dumps({"username": "JobWatch", "embeds": [embed]})},
            files={"file": ("octagram.png", png, "image/png")},
            timeout=30,
        )
        if resp.status_code == 429:
            import time
            time.sleep(float(resp.json().get("retry_after", 5)))
            return False   # caller may retry next run rather than assume delivery
        elif not resp.ok:
            print(f"  [READINESS] webhook error {resp.status_code}: {resp.text[:200]} — NOT marked as posted")
            return False
        else:
            print("  [READINESS] posted octagram")
            return True
    except Exception as e:
        print(f"  [READINESS] post error: {e} — NOT marked as posted")
        return False

# ───────────────────────────── ORCHESTRATION ─────────────────────────────

def _already_posted_today(con):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = con.execute("SELECT value FROM readiness_meta WHERE key='last_posted'").fetchone()
    return row is not None and row[0] == today

def _mark_posted_today(con):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    con.execute("INSERT INTO readiness_meta (key, value) VALUES ('last_posted', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today,))
    con.commit()

def _load_traction(db_path):
    """Lazy import avoids a cycle (jobwatch_pipeline imports this module)."""
    try:
        import jobwatch_pipeline
        return jobwatch_pipeline.load_traction(db_path)
    except Exception:
        return None

def maybe_post(webhook_url, db_path="jobwatch.db", dry=False, force=False):
    resume = load_resume()
    if resume is None:
        return
    con = _db(db_path)
    if not POST_EVERY_RUN and not force and not dry and _already_posted_today(con):
        print("  [READINESS] already posted today — skipping (pass FORCE_READINESS=1 to override)")
        con.close()
        return
    results = compute_readiness(resume, db_path=db_path)
    delivered = post_octagram(results, webhook_url, dry=dry, traction=_load_traction(db_path))
    if delivered:
        _mark_posted_today(con)
    con.close()

def run_pass(raw_jobs, db_path="jobwatch.db", webhook_url="", dry=False, force=False):
    """Convenience wrapper called from jobwatch.py once per pass with the FULL
    pre-filter raw job list (so market_history reflects the whole scraped
    market, not just what survives keyword/remote/ghost filtering)."""
    ingest(raw_jobs, db_path=db_path)
    maybe_post(webhook_url, db_path=db_path, dry=dry, force=force)
