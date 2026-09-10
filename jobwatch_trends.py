#!/usr/bin/env python3
"""Twice-weekly job-market trend chart for JobWatch.

Every alert candidate is retained once in ``job_trend_events``. On Mondays and
Thursdays (UTC), the jobs discovered since the prior snapshot are aggregated
into three series and posted to the dedicated #job-map webhook. The chart only
shows the latest 61 days; the SQLite snapshot history is never pruned.
"""

import hashlib
import io
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import requests


POST_WEEKDAYS_UTC = {0, 3}  # Monday and Thursday
WINDOW_DAYS = 61
HIGH_PRIORITY_MIN = 70.0
MEDIUM_PRIORITY_MIN = 50.0


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _db(db_path):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS job_trend_events (
        fingerprint TEXT PRIMARY KEY,
        observed_at TEXT NOT NULL,
        fit_score REAL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS job_trend_snapshots (
        period_end TEXT PRIMARY KEY,
        period_start TEXT NOT NULL,
        total_jobs INTEGER NOT NULL,
        high_priority INTEGER NOT NULL,
        medium_priority INTEGER NOT NULL
    )""")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_trend_events_observed "
        "ON job_trend_events(observed_at)"
    )
    con.commit()
    return con


def _fingerprint(job):
    method = getattr(job, "fingerprint", None)
    if callable(method):
        return method()
    key = "|".join(str(getattr(job, field, "") or "").strip().lower()
                   for field in ("company", "title", "location", "url"))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _score(job):
    fit = getattr(job, "fit", None) or {}
    value = fit.get("score") if isinstance(fit, dict) else None
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def ingest(jobs, db_path="jobwatch.db", observed_at=None):
    """Retain each unique alert candidate once; return the inserted count."""
    stamp = _utc(observed_at).isoformat()
    con = _db(db_path)
    inserted = 0
    try:
        for job in jobs:
            cur = con.execute(
                "INSERT OR IGNORE INTO job_trend_events "
                "(fingerprint, observed_at, fit_score) VALUES (?,?,?)",
                (_fingerprint(job), stamp, _score(job)),
            )
            inserted += cur.rowcount
        con.commit()
    finally:
        con.close()
    if inserted:
        print(f"  [JOB-TREND] retained {inserted} new alert candidate(s)")
    return inserted


def _last_snapshot(con):
    return con.execute(
        "SELECT period_end, period_start, total_jobs, high_priority, medium_priority "
        "FROM job_trend_snapshots ORDER BY period_end DESC LIMIT 1"
    ).fetchone()


def _candidate(con, now):
    last = _last_snapshot(con)
    start = last[0] if last else "0001-01-01T00:00:00+00:00"
    end = _utc(now).isoformat()
    total, high, medium = con.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN fit_score >= ? THEN 1 ELSE 0 END),
               SUM(CASE WHEN fit_score >= ? AND fit_score < ? THEN 1 ELSE 0 END)
        FROM job_trend_events
        WHERE observed_at > ? AND observed_at <= ?
    """, (HIGH_PRIORITY_MIN, MEDIUM_PRIORITY_MIN, HIGH_PRIORITY_MIN, start, end)).fetchone()
    return {
        "period_start": start,
        "period_end": end,
        "total_jobs": int(total or 0),
        "high_priority": int(high or 0),
        "medium_priority": int(medium or 0),
    }


def _dry_candidate(con, jobs, now):
    candidate = _candidate(con, now)
    scores = [_score(job) for job in jobs]
    candidate["total_jobs"] += len(jobs)
    candidate["high_priority"] += sum(s is not None and s >= HIGH_PRIORITY_MIN for s in scores)
    candidate["medium_priority"] += sum(
        s is not None and MEDIUM_PRIORITY_MIN <= s < HIGH_PRIORITY_MIN for s in scores
    )
    return candidate


def _chart_rows(con, candidate, now):
    cutoff = (_utc(now) - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = [dict(zip(
        ("period_end", "period_start", "total_jobs", "high_priority", "medium_priority"), row
    )) for row in con.execute("""
        SELECT period_end, period_start, total_jobs, high_priority, medium_priority
        FROM job_trend_snapshots WHERE period_end >= ? ORDER BY period_end
    """, (cutoff,)).fetchall()]
    rows.append(candidate)
    return rows


def render_chart(rows):
    """Render the rolling trend chart as dark-theme PNG bytes."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    dates = [datetime.fromisoformat(row["period_end"]) for row in rows]
    series = (
        ("All matching jobs", "total_jobs", "#58a6ff"),
        ("High priority (70%+)", "high_priority", "#3fb950"),
        ("Medium priority (50–69%)", "medium_priority", "#d29922"),
    )

    fig, ax = plt.subplots(figsize=(10, 5.8), dpi=150, facecolor="#0b0e14")
    ax.set_facecolor("#0b0e14")
    for label, key, color in series:
        values = [row[key] for row in rows]
        ax.plot(dates, values, label=label, color=color, linewidth=2.5,
                marker="o", markersize=5)

    ax.set_title("Cleo Job Market Trend", color="white", fontsize=17,
                 fontweight="bold", pad=18)
    ax.text(0.5, 1.01, "New JobWatch matches per Monday/Thursday interval · rolling 61 days",
            transform=ax.transAxes, color="#8b949e", fontsize=9.5,
            ha="center", va="bottom")
    ax.set_ylabel("Jobs discovered", color="#c9d1d9")
    ax.tick_params(colors="#8b949e")
    ax.grid(axis="y", color="#30363d", linewidth=0.8, alpha=0.8)
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    ax.yaxis.set_major_locator(MaxNLocator(integer=True, min_n_ticks=4))
    # Explicit bounds prevent Matplotlib's single-date autoscaling from
    # spanning several years and choosing repeated January ticks.
    latest = max(_utc(date) for date in dates)
    ax.set_xlim(latest - timedelta(days=WINDOW_DAYS), latest + timedelta(days=1))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(
        minticks=3, maxticks=9, tz=timezone.utc))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=timezone.utc))
    ax.set_xlabel(f"Snapshot date (UTC) · latest {latest:%b %d, %Y}",
                  color="#c9d1d9")
    ax.legend(loc="upper left", frameon=False, labelcolor="#c9d1d9", fontsize=9)
    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor(),
                bbox_inches="tight", pad_inches=0.25)
    plt.close(fig)
    return buf.getvalue()


def _delta(current, previous, key):
    if previous is None:
        return "first snapshot"
    change = current[key] - previous[key]
    return f"{change:+d} vs. prior interval"


def post_chart(rows, webhook_url, dry=False):
    latest = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    interval_start = datetime.fromisoformat(latest["period_start"]) if not latest["period_start"].startswith("0001-") else None
    interval_end = datetime.fromisoformat(latest["period_end"])
    interval = ((interval_start.strftime("%b %d") + "–") if interval_start else "Through ") + interval_end.strftime("%b %d, %Y")
    embed = {
        "title": "📈 Job Market Trend",
        "description": "New Cleo-targeted matches captured between snapshots. The chart shows 61 days; full history remains stored in Cleo Jobs.",
        "color": 0x58A6FF,
        "image": {"url": "attachment://job-market-trend.png"},
        "fields": [
            {"name": "Latest interval", "value": interval, "inline": False},
            {"name": "All jobs", "value": f"{latest['total_jobs']} ({_delta(latest, previous, 'total_jobs')})", "inline": True},
            {"name": "High · 70%+", "value": f"{latest['high_priority']} ({_delta(latest, previous, 'high_priority')})", "inline": True},
            {"name": "Medium · 50–69%", "value": f"{latest['medium_priority']} ({_delta(latest, previous, 'medium_priority')})", "inline": True},
        ],
        "footer": {"text": interval_end.strftime("Updated %Y-%m-%d %H:%M UTC")},
    }
    png = render_chart(rows)
    if dry:
        print("  [JOB-TREND] --dry — would post chart: "
              f"all={latest['total_jobs']}, high={latest['high_priority']}, "
              f"medium={latest['medium_priority']}")
        return False
    if not webhook_url or not webhook_url.startswith("http"):
        print("  [JOB-TREND] JOB_MAP_WEBHOOK_URL not set/invalid — skipping")
        return False
    try:
        response = requests.post(
            webhook_url,
            data={"payload_json": json.dumps({"username": "JobWatch", "embeds": [embed]})},
            files={"file": ("job-market-trend.png", png, "image/png")},
            timeout=30,
        )
        if response.status_code == 429:
            print("  [JOB-TREND] Discord rate-limited the chart; next run will retry")
            return False
        if not response.ok:
            print(f"  [JOB-TREND] webhook error {response.status_code}: {response.text[:200]}")
            return False
        print("  [JOB-TREND] posted rolling market chart")
        return True
    except Exception as exc:
        print(f"  [JOB-TREND] post error: {exc}")
        return False


def maybe_post(webhook_url, db_path="jobwatch.db", dry=False, force=False,
               now=None, dry_jobs=()):
    now = _utc(now)
    con = _db(db_path)
    try:
        last = _last_snapshot(con)
        already_today = bool(last and last[0][:10] == now.date().isoformat())
        if not force and not dry and now.weekday() not in POST_WEEKDAYS_UTC:
            return False
        if already_today and not force:
            return False
        candidate = _dry_candidate(con, dry_jobs, now) if dry else _candidate(con, now)
        if not last and candidate["total_jobs"] == 0:
            print("  [JOB-TREND] no retained jobs yet — chart will start after the first match")
            return False
        rows = _chart_rows(con, candidate, now)
        delivered = post_chart(rows, webhook_url, dry=dry)
        if delivered:
            con.execute("""INSERT INTO job_trend_snapshots
                (period_end, period_start, total_jobs, high_priority, medium_priority)
                VALUES (?,?,?,?,?)""", (
                    candidate["period_end"], candidate["period_start"],
                    candidate["total_jobs"], candidate["high_priority"],
                    candidate["medium_priority"],
                ))
            con.commit()
        return delivered
    finally:
        con.close()


def run_pass(new_jobs, db_path="jobwatch.db", webhook_url="", dry=False,
             force=False, now=None):
    """Record this run's new alert candidates and post when the gate is due."""
    jobs = list(new_jobs)
    if dry:
        print(f"  [JOB-TREND] --dry — would retain {len(jobs)} candidate(s)")
    else:
        ingest(jobs, db_path=db_path, observed_at=now)
    return maybe_post(webhook_url, db_path=db_path, dry=dry, force=force,
                      now=now, dry_jobs=jobs)
