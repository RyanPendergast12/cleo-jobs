#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
jobwatch.py — Pull Cleo-targeted jobs from multiple boards, dedupe, score, and post to Discord.

Design goals (your asks):
  - NO duplicates: cross-source fingerprint + SQLite "seen" store that persists across runs.
  - NO ghost jobs: age filter + repost detection + vagueness/salary heuristics + ATS verification hook.
  - Discord delivery: rich embeds, batched, rate-limit aware.

Sources, ranked by how reliable they are to pull (read SOURCE NOTES at bottom):
  RELIABLE (feed/API):     NoDesk, RemoteOK, We Work Remotely (all RSS/JSON, no auth)
  GOOD (open, no feed):    isecjobs.com (best-effort HTML scrape, verify output)
  SEMI (internal API):     HiringCafe (broken architecture-side, not just Cloudflare)
  FRAGILE (scrape, ToS):   LinkedIn (guest endpoint), Built In
  EMAIL (Gmail IMAP):      Indeed (indeed_email) — reads your Indeed job-alert emails
                           instead of scraping indeed.com, sidestepping the Cloudflare
                           IP-reputation wall entirely. Glassdoor (glassdoor_email) is
                           the same pattern but still a stub pending a real digest
                           HTML sample. See EMAIL ADAPTERS section below.
  HIGH-RISK (Playwright, disabled by default): Indeed, Glassdoor direct-scrape adapters
                           are still in this file but turned off in ENABLED_SOURCES —
                           Cloudflare's IP-reputation scoring treats GitHub's shared
                           cloud IPs with more suspicion than a home connection, JS
                           rendering via Playwright doesn't fix that. Superseded by the
                           email adapters above.
  DISABLED (dead ends):    CareerHound (paid signup wall, no public data)

Run:  python jobwatch.py            # one pass, then exit (use cron/GitHub Actions to schedule)
      python jobwatch.py --loop     # run forever, sleeping POLL_MINUTES between passes
      python jobwatch.py --dry      # don't post to Discord, just print what WOULD be sent

Deps: pip install requests feedparser beautifulsoup4 playwright
      playwright install chromium   # one-time browser binary download, ~150MB
      (no extra dep for the email adapters — imaplib/email are Python stdlib)
"""

import argparse
import base64
import email
import gzip
import hashlib
import html
import imaplib
import json
import io
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from urllib.parse import urlencode, urlparse, parse_qs, unquote

import requests

import jobwatch_readiness as jr
import jobwatch_trends as jt
import jobwatch_skills_export as jse
import jobwatch_pipeline as jp
import jobwatch_display as display
import jobwatch_apply as applications
import jobwatch_enrichment as online_enrichment
import jobwatch_match as matching
import jobwatch_workplace as workplace

try:
    import feedparser
except ImportError:
    feedparser = None
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# ───────────────────────────── CONFIG — edit this block ─────────────────────────────

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PUT_YOUR_DISCORD_WEBHOOK_URL_HERE")
JOB_MAP_WEBHOOK_URL  = os.environ.get("JOB_MAP_WEBHOOK_URL", "")   # separate webhook for #job-map
                                                                      # twice-weekly trend chart; blank disables it
JOB_READINESS_WEBHOOK_URL = os.environ.get("JOB_READINESS_WEBHOOK_URL", "")  # #job-readiness —
                                                                      # posts the Job Readiness Octagram
                                                                      # once/day. Leave blank to disable.
FORCE_READINESS_POST = os.environ.get("FORCE_READINESS", "").lower() in ("1", "true", "yes")
                                                                      # bypass the once/day gate for this
                                                                      # run only — set via workflow_dispatch
                                                                      # input when you want to force-post
                                                                      # right now instead of waiting for
                                                                      # the UTC day to roll over
FORCE_JOB_MAP_POST = os.environ.get("FORCE_JOB_MAP", "").lower() in ("1", "true", "yes")
                                                                      # bypass the Monday/Thursday gate
                                                                      # for a manual chart test
NEEDED_SKILLS_WEBHOOK_URL = os.environ.get("NEEDED_SKILLS_WEBHOOK_URL", "")  # #needed-skills —
                                                                      # posts skill-demand CSVs once/day.
                                                                      # Leave blank to disable.
FORCE_SKILLS_EXPORT = os.environ.get("FORCE_SKILLS_EXPORT", "").lower() in ("1", "true", "yes")
                                                                      # bypass the once/day gate, same
                                                                      # pattern as FORCE_READINESS_POST
JOBDATA_API_KEY      = os.environ.get("JOBDATA_API_KEY", "")        # foorilla/jobdataapi.com key
                                                                      # free tier (~10 req/hr) works
                                                                      # without one; set this for
                                                                      # pagination + date slicing
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID", "")                  # api.adzuna.com — free signup
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")                # at developer.adzuna.com
GMAIL_USER = os.environ.get("GMAIL_USER", "")                        # Gmail address receiving your
                                                                      # Indeed/Glassdoor job-alert emails
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")        # 16-char Gmail App Password
                                                                      # (requires 2-Step Verification
                                                                      # turned on — NOT your normal
                                                                      # Gmail password). Used read-only:
                                                                      # the email adapters never send,
                                                                      # delete, or modify mail.
# Locally: set this once per shell session before testing —
#   Windows (cmd):        set DISCORD_WEBHOOK_URL=https://discordapp.com/api/webhooks/...
#   Windows (PowerShell):  $env:DISCORD_WEBHOOK_URL="https://discordapp.com/api/webhooks/..."
# On GitHub Actions: comes from a repository secret instead, see the workflow file.

def _hourly_gate(divisor: int) -> bool:
    """True only when the current UTC hour is a multiple of `divisor`.
    Lets a source stay wired into the 2-hour cron without actually being
    called every pass — e.g. divisor=6 -> fires at 00/06/12/18 UTC (4x/day),
    divisor=24 -> fires once/day. Used to respect each free API's own
    'don't poll us that often' guidance without touching jobwatch.yml's cron."""
    return datetime.now(timezone.utc).hour % divisor == 0

DB_PATH = "jobwatch.db"
POLL_MINUTES = 90                 # how often --loop runs a pass
USER_AGENT = "Mozilla/5.0 (compatible; cleo-jobs/1.0; personal job alerts)"

# What you actually want. Title must match an INCLUDE term and none of the EXCLUDE terms.
INCLUDE_KEYWORDS = [
    # Healthcare and patient operations
    "patient access", "patient services", "patient coordinator", "patient care coordinator",
    "medical office", "medical receptionist", "clinic coordinator", "healthcare coordinator",
    "health services coordinator", "referral coordinator", "scheduling coordinator",
    "intake coordinator", "care coordinator", "medical records", "health information",
    # Human resources and people operations
    "human resources coordinator", "human resources assistant", "hr coordinator", "hr assistant",
    "people operations coordinator", "people coordinator", "onboarding coordinator",
    "benefits coordinator", "recruiting coordinator", "talent coordinator",
    # Administrative, operations, and public-health coordination
    "administrative coordinator", "administrative assistant", "office coordinator",
    "operations coordinator", "program coordinator", "public health", "community health",
    "health education coordinator", "outreach coordinator", "executive assistant",
]
EXCLUDE_COMPANIES = [
    # lead-gen / aggregator patterns: each appeared repeatedly with generic,
    # scattered titles. Add more as you spot them — plain casual text is fine,
    # matched case-insensitively.
    "cyber focus ai",
    "Haystack",
    "DataAnnotation",
    "Jobgether",
    "Careerscape",
]
EXCLUDE_KEYWORDS = [
    # Checked before INCLUDE_KEYWORDS in _matches_wanted(), so any term here
    # wins over an include match. Seniority terms keep the feed at IC entry/
    # mid level; drop manager/director/etc. if you want those too.
    "commission only", "outside sales", "account executive", "intern", "senior", "manager",
    "supervisor", "staff", "director", "principal", "lead", "head of", "chief",
    "vice president", "vp", "sr.",
    # Licensed clinical roles that the supplied resume does not support.
    "registered nurse", "licensed practical nurse", "nurse practitioner", "physician assistant",
    "dental hygienist", "pharmacist", "licensed therapist", "clinical social worker",
]
REMOTE_ONLY = False                # Cleo accepts remote or Las Vegas-area onsite/hybrid roles.

# Ghost-job thresholds. Combination of signals = suppression (per 2026 ghost-job research).
MAX_AGE_DAYS = 30                 # listings older than this are dropped outright
REPOST_GHOST_THRESHOLD = 2        # seen reposted N+ times with new dates => ghost
MIN_DESC_CHARS = 220             # very short descriptions read as placeholder/boilerplate
GHOST_SCORE_SUPPRESS = 2          # total ghost-signal points at/above which we hide the job
SUPPRESS_GHOSTS = True            # True = never send. False = send but tag "⚠ possible ghost"
VERIFY_ON_ATS = False             # if True, HEAD-check the apply URL still resolves (slower)
INDEED_GLASSDOOR_DELAY = 6.0      # seconds between requests to the two high-risk sources.
                                    # Keep this generous — these are the two sources most
                                    # likely to get your home IP rate-limited if hammered.
LINKEDIN_FETCH_FULL_DESC = True   # fetch each job's full description (for Experience/Comp
                                    # text-mining) via LinkedIn's per-job guest endpoint.
                                    # Runs after title/company filtering, before remote
                                    # classification, so search filters are not mistaken
                                    # for evidence about the individual posting. Set to
                                    # False to go back to zero extra LinkedIn requests.
LINKEDIN_DETAIL_DELAY = 2.0       # politeness delay between these per-job detail fetches

# Per-source search terms (used by API/scrape sources that take a query).
SEARCH_TERMS = [
    "patient access representative",
    "medical office assistant",
    "patient services representative",
    "healthcare coordinator",
    "referral coordinator",
    "human resources coordinator",
    "administrative coordinator",
    "public health program coordinator",
]
LOCATION = "Las Vegas, Nevada, United States"
LINKEDIN_SEARCH_MARKETS = (
    (LOCATION, ""),              # local onsite/hybrid roles
    ("United States", "2"),      # nationwide remote roles
)

# Toggle sources on/off here.
ENABLED_SOURCES = {
    "nodesk": True,
    "careerhound": False,         # confirmed dead end: paid signup-gated, no public data at all
    "cybersecurityroles": False,  # not relevant to Cleo's target market
    "hiringcafe": False,          # dead: the /api/search-jobs endpoint is gone and the live
                                  # site filters client-side only. src_hiringcafe() always
                                  # raises. Re-enable only with an Apify actor / Playwright path.
    "linkedin": True,
    "wellfound": False,           # off by default: heavy bot-protection, expect breakage
    "builtin": False,             # no stable public feed; _autodiscover_feed() finds nothing
                                  # and src_builtin() always raises. Needs the search XHR wired
                                  # like hiringcafe before it can be turned back on.
    "remoteok": True,             # free public API, reliable
    "weworkremotely": True,       # free RSS, reliable
    "isecjobs": False,            # ⚠ SUNSET: isecjobs.com shutting down mid-2026, replaced by foorilla
    "foorilla": False,            # cybersecurity-specific source; intentionally disabled here
                                  # structured JSON with company, salary, experience built-in
    "indeed": False,               # OFF — superseded by indeed_email below. Cloudflare's IP-
                                    # reputation scoring on GitHub Actions' shared cloud IPs made
                                    # this unreliable regardless of the Playwright JS-rendering
                                    # upgrade. Flip back on only if you want to run it from a
                                    # residential IP instead of Actions.
    "glassdoor": False,            # OFF — same Cloudflare IP-reputation ceiling as indeed above.
                                    # Will be superseded by glassdoor_email once that parser is
                                    # built (needs a raw digest HTML sample first — see below).
    "indeed_email": True,          # Gmail IMAP adapter — parses your Indeed job-alert emails
                                    # instead of scraping indeed.com directly. Requires
                                    # GMAIL_USER / GMAIL_APP_PASSWORD to be set. Read-only.
    "glassdoor_email": False,      # STUB — disabled until a real Glassdoor digest email HTML
                                    # sample is captured and the parser is built against it.
    "remotive": True,              # remotive.com API — gated to 4x/day (their own rate guidance)
    "jobicy": True,                # jobicy.com API — gated to 4x/day (their own rate guidance)
    "himalayas": True,             # himalayas.app API — gated to 1x/day (data refreshes daily)
    "adzuna": False,               # off until ADZUNA_APP_ID/APP_KEY secrets are set
}

# ───────────────────────────── DATA MODEL ─────────────────────────────

@dataclass
class Job:
    source: str
    title: str
    company: str
    url: str
    location: str = ""
    remote: bool = False
    work_mode: str = ""            # "Remote"/"Hybrid"/"Onsite" once classified, "" if unknown
    posted_at: str = ""           # ISO8601 if known
    salary: str = ""
    experience: str = ""          # e.g. "3+ years of experience", when found in the text
    description: str = ""
    native_id: str = ""           # source's own id if available
    ghost_flags: list = field(default_factory=list)
    fit: dict = field(default_factory=dict)
    online_posting: dict = field(default_factory=dict)
    work_mode_source: str = ""
    work_mode_note: str = ""

    def fingerprint(self) -> str:
        """Source-agnostic identity so the same role across boards collapses to one."""
        key = f"{_norm(self.company)}|{_norm(self.title)}|{_norm(self.location)}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

# ───────────────────────────── NORMALIZATION HELPERS ─────────────────────────────

_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|co|gmbh|plc|limited|incorporated)\b\.?", re.I)
_DECOR = re.compile(r"\((remote|hybrid|us|usa|onsite|contract|full[- ]time)\)", re.I)

def _norm(s: str) -> str:
    s = (s or "").lower()
    s = html.unescape(s)
    s = _DECOR.sub("", s)
    s = _SUFFIXES.sub("", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def _is_remote(text: str) -> bool:
    return display.is_remote(text)

def _matches_wanted(title: str) -> bool:
    t = (title or "").lower()
    if any(x in t for x in EXCLUDE_KEYWORDS):
        return False
    return any(x in t for x in INCLUDE_KEYWORDS)

def _parse_date(value) -> str:
    if not value:
        return ""
    if isinstance(value, time.struct_time):
        return datetime(*value[:6], tzinfo=timezone.utc).isoformat()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(str(value), fmt).astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return ""

def _age_days(iso: str):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None

def _extract_experience(title: str, description: str = "", explicit: str = "") -> str:
    return display.experience(title, description, explicit)

def _parse_relative_age(text: str) -> str:
    """Parse '5h ago' / '12h ago' / '1d ago' / '2w ago' style relative timestamps
    (isecjobs's format) into an ISO8601 string so age-based ghost filtering can use it.
    Truncated to day-level precision deliberately: since the source text is relative
    ('12h ago'), recomputing it against 'now' at two different call times would otherwise
    drift forward by however much wall-clock time passed between runs, even for a listing
    that never actually changed — which the repost-detection logic would misread as a
    fresh repost every single run. Day-level rounding makes that false positive rare
    (only near a midnight boundary) instead of constant."""
    if not text:
        return ""
    m = re.search(r"\b(\d+)\s*(h|d|w|mo)\s*ago\b", text, re.IGNORECASE)
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n),
             "w": timedelta(weeks=n), "mo": timedelta(days=n * 30)}.get(unit)
    if not delta:
        return ""
    dt = (datetime.now(timezone.utc) - delta).replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.isoformat()

# ───────────────────────────── HTTP ─────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

def _get(url, **kw):
    kw.setdefault("timeout", 25)
    return SESSION.get(url, **kw)

# ───────────────────────────── ADAPTERS ─────────────────────────────
# Each adapter returns list[Job]. All wrapped in try/except by run_source so one
# broken board never kills the pass.

def fetch_rss(feed_url, source_name):
    """Generic RSS/Atom adapter. Works for any board exposing a feed."""
    if feedparser is None:
        raise RuntimeError("pip install feedparser")
    parsed = feedparser.parse(feed_url, request_headers={"User-Agent": USER_AGENT})
    jobs = []
    for e in parsed.entries:
        desc = re.sub(r"<[^>]+>", " ", e.get("summary", "") or "")
        title = html.unescape(e.get("title", "") or "")
        # Feeds often pack "Title at Company" or use author/tags for company.
        company = e.get("author", "") or ""
        m = re.search(r"\bat\s+(.+)$", title)
        if not company and m:
            company = m.group(1).strip()
            title = title[: m.start()].strip()
        jobs.append(Job(
            source=source_name,
            title=title,
            company=company or "(unknown)",
            url=e.get("link", ""),
            location=", ".join(t.get("term", "") for t in e.get("tags", []) if t.get("term"))[:80],
            remote=_is_remote(title + " " + desc),
            posted_at=_parse_date(e.get("published_parsed") or e.get("updated_parsed")),
            description=desc.strip(),
            native_id=e.get("id", "") or e.get("link", ""),
        ))
    return jobs

def _autodiscover_feed(base):
    """Try the common feed paths a job board exposes, return first that parses."""
    candidates = ["/index.xml", "/feed", "/rss", "/rss.xml", "/jobs.rss", "/feed.xml", "/atom.xml"]
    for path in candidates:
        url = base.rstrip("/") + path
        try:
            r = _get(url)
            if r.ok and ("<rss" in r.text[:600].lower() or "<feed" in r.text[:600].lower()):
                return url
        except requests.RequestException:
            continue
    return None

def src_nodesk():
    # Verified Hugo-style feed.
    return fetch_rss("https://nodesk.co/remote-jobs/index.xml", "nodesk")

def src_careerhound():
    # Confirmed by checking the live site: this is a paid, signup-gated lead-gen product
    # with no public job list and no RSS feed. There is nothing to automate here without
    # logging into a paid account, which would violate their ToS. Left disabled.
    raise RuntimeError("CareerHound has no public data — it's a signup-gated paid product, not a feed-able board.")

def src_cybersecurityroles():
    feed = _autodiscover_feed("https://cybersecurityroles.com")
    if not feed:
        raise RuntimeError("No feed found on cybersecurityroles.com — confirm the exact domain & feed path.")
    return fetch_rss(feed, "cybersecurityroles")

def src_hiringcafe():
    """HiringCafe internal API. DISABLED in ENABLED_SOURCES — the /api/search-jobs
    endpoint is gone and the live site filters client-side only. Left here so an
    Apify-actor / Playwright reimplementation has a starting point; as written it
    will just raise. NOTE: also Cloudflare-gated from datacenter IPs."""
    jobs = []
    for term in SEARCH_TERMS:
        payload = {
            "searchState": {"searchQuery": term, "workplaceTypes": ["Remote"], "sortBy": "date"},
            "size": 40, "page": 0,
        }
        r = SESSION.post("https://hiring.cafe/api/search-jobs", json=payload,
                         headers={"Content-Type": "application/json"}, timeout=25)
        if not r.ok:
            raise RuntimeError(f"hiring.cafe returned {r.status_code} (endpoint dead / Cloudflare). Use a proxy/Apify actor.")
        for item in r.json().get("results", r.json().get("jobs", [])):
            info = item.get("job_information", item)
            jobs.append(Job(
                source="hiringcafe",
                title=info.get("title", ""),
                company=info.get("company_name", item.get("company", "")),
                url=item.get("apply_url", info.get("url", "")),
                location=info.get("location", ""),
                remote=_is_remote(info.get("location", "")),
                posted_at=_parse_date(item.get("estimated_publish_date", info.get("posted_at"))),
                salary=str(info.get("compensation", "") or ""),
                description=info.get("description", "")[:4000],
                native_id=str(item.get("id", item.get("job_id", ""))),
            ))
    return jobs

def src_linkedin():
    """LinkedIn guest endpoint. PUBLIC, no login, but against ToS and IP-rate-limited.
    f_TPR=r86400 restricts to the last 24h, which is exactly what an alert poller wants."""
    if BeautifulSoup is None:
        raise RuntimeError("pip install beautifulsoup4")
    jobs, seen_links = [], set()
    base = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
    for term in SEARCH_TERMS:
        # One page in each market keeps request volume controlled while covering
        # both Las Vegas-area jobs and nationwide remote roles.
        for location, workplace_type in LINKEDIN_SEARCH_MARKETS:
            q = {"keywords": term, "location": location, "f_TPR": "r86400",
                 "f_WT": workplace_type, "start": 0}
            r = _get(base + "?" + urlencode({k: v for k, v in q.items() if v != ""}))
            if r.status_code == 429:
                SOURCE_STATUS["linkedin"] = f"rate limited (429); retained {len(jobs)} fetched jobs"
                return jobs
            if not r.ok:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("li")
            if not cards:
                break
            for li in cards:
                a = li.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
                title_el = li.select_one("h3, .base-search-card__title")
                comp_el = li.select_one("h4, .base-search-card__subtitle")
                loc_el = li.select_one(".job-search-card__location")
                date_el = li.select_one("time")
                if not a or not title_el:
                    continue
                link = a.get("href", "").split("?")[0]
                if link in seen_links:
                    continue
                seen_links.add(link)
                jobs.append(Job(
                    source="linkedin",
                    title=title_el.get_text(strip=True),
                    company=(comp_el.get_text(strip=True) if comp_el else ""),
                    url=link,
                    location=(loc_el.get_text(strip=True) if loc_el else ""),
                    remote=_is_remote(loc_el.get_text() if loc_el else ""),
                    posted_at=_parse_date(date_el.get("datetime")) if date_el else "",
                    native_id=re.search(r"(\d+)(?:/?$)", link).group(1) if re.search(r"\d+", link) else link,
                ))
            time.sleep(2.5)  # be a polite guest
    return jobs

def _fetch_linkedin_details(job):
    """Reuse the guest detail response for description AND scoped workplace metadata."""
    if not job.native_id or not job.native_id.isdigit() or BeautifulSoup is None:
        return
    try:
        r = _get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job.native_id}")
        if not r.ok:
            job.work_mode_note = f"LinkedIn metadata unavailable (HTTP {r.status_code})"
            return
        soup = BeautifulSoup(r.text, "html.parser")
        heading = soup.select_one(".topcard__title")
        if heading and _norm(heading.get_text(" ", strip=True)) != _norm(job.title):
            job.work_mode_note = "Posting identity mismatch"
            return
        node = soup.select_one('[class*="description"] > section > div')
        if node and not job.description:
            job.description = node.get_text("\n", strip=True)[:12000]
        mode, source = workplace.from_soup(soup, job.title, job.company)
        if not workplace.apply(job, mode, source):
            job.work_mode_note = source
    except requests.RequestException:
        job.work_mode_note = "LinkedIn metadata temporarily unavailable"


def src_wellfound():
    """Wellfound (ex-AngelList). Heavily bot-protected GraphQL; expect this to break.
    Best handled via a headless browser (you already run Playwright) or an Apify actor.
    Left as an explicit stub so it never silently returns junk."""
    raise RuntimeError("Wellfound needs a headless-browser path (Playwright) — not wired up. Disabled by default.")

def src_builtin():
    """Built In is a Next.js app with no public feed. Their internal search API shape changes;
    autodiscover a feed first, else tell the user to capture the XHR from DevTools."""
    feed = _autodiscover_feed("https://builtin.com")
    if feed:
        return fetch_rss(feed, "builtin")
    raise RuntimeError("Built In has no stable feed — capture its /api search XHR in DevTools and wire it like hiringcafe.")

def src_remoteok():
    """RemoteOK's official public JSON API. Free, no login, no proxy, CORS-enabled.
    Response is a list where item[0] is a legal disclaimer object — skip it."""
    r = _get("https://remoteok.com/api")
    if not r.ok:
        raise RuntimeError(f"remoteok.com returned {r.status_code}")
    data = r.json()
    jobs = []
    for item in data:
        if not isinstance(item, dict) or "position" not in item:
            continue  # skips the legal-notice item[0]
        tags = item.get("tags", []) or []
        salary_min, salary_max = item.get("salary_min"), item.get("salary_max")
        salary = f"${salary_min:,}-${salary_max:,}" if salary_min and salary_max else ""
        jobs.append(Job(
            source="remoteok",
            title=item.get("position", ""),
            company=item.get("company", ""),
            url=item.get("url", "") or item.get("apply_url", ""),
            location=item.get("location", "") or "Remote",
            remote=True,
            posted_at=_parse_date(item.get("date")),
            salary=salary,
            description=re.sub(r"<[^>]+>", " ", item.get("description", "") or "")[:12000]
                        + (" Tags: " + ", ".join(tags) if tags else ""),
            native_id=str(item.get("id", "")),
        ))
    return jobs

def src_weworkremotely():
    """We Work Remotely's all-jobs RSS feed. No dedicated security category exists on WWR,
    so we pull everything and let INCLUDE_KEYWORDS do the narrowing (same pattern as NoDesk)."""
    return fetch_rss("https://weworkremotely.com/remote-jobs.rss", "weworkremotely")

def src_isecjobs():
    """isecjobs.com (formerly infosec-jobs.com) — cybersecurity-specific, fully public,
    no login wall, no Cloudflare gate observed. No stable JSON/RSS feed, so this is a
    best-effort HTML scrape. CONFIRMED LIMITATION (checked the live listing page directly):
    company name is NOT shown on the search results page at all, only on each job's
    individual detail page — fetching that per job would multiply requests well beyond
    what's polite for a free site, so company stays blank here. Salary, tags, location,
    and posted-time ARE all on the listing page and get parsed below."""
    if BeautifulSoup is None:
        raise RuntimeError("pip install beautifulsoup4")
    jobs, seen = [], set()
    for term in SEARCH_TERMS:
        url = "https://isecjobs.com/?" + urlencode({"q": term, "remote": "true"})
        r = _get(url)
        if not r.ok:
            raise RuntimeError(f"isecjobs.com returned {r.status_code}")
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href*='/job/']"):
            href = a.get("href", "")
            full = href if href.startswith("http") else "https://isecjobs.com" + href
            if not href or full in seen:
                continue
            seen.add(full)
            title = a.get_text(strip=True)
            if not title:
                continue
            container = a.find_parent("li") or a.find_parent("article") or a.parent
            block = container.get_text(" ", strip=True) if container else ""
            salary_m = re.search(r"([A-Z]{3}\s?\d[\d,]*K(?:\s?-\s?\d[\d,]*K)?)", block)
            # Strip the title and salary back out of the block so the description isn't
            # just a redundant restatement of fields already shown elsewhere in the embed.
            clean = block
            if clean.startswith(title):
                clean = clean[len(title):].strip()
            if salary_m:
                clean = clean.replace(salary_m.group(0), "", 1).strip()
            jobs.append(Job(
                source="isecjobs",
                title=title,
                company="",  # genuinely not on the listing page, see docstring
                url=full,
                remote=_is_remote(block),
                salary=salary_m.group(1) if salary_m else "",
                description=clean[:500],
                posted_at=_parse_relative_age(block),
                native_id=full,
            ))
        time.sleep(1.0)
    return jobs

def src_foorilla():
    """jobdataapi.com — structured JSON API by foorilla (the successor to isecjobs.com).
    isecjobs.com was sunset mid-2026; this is the same team's official replacement and
    a major upgrade: company name, salary, experience level, and full description are all
    structured fields, not scraped HTML guesses.

    Free tier (no API key): ~10 req/hour, SHARED across every anonymous caller hitting
    jobdataapi.com from GitHub Actions' runner IP pool (not just this workflow) — first-page
    results only, no date slicing. We combine all SEARCH_TERMS into a single request using
    jobdataapi's `|OR|` multi-value title syntax instead of one request per term, cutting our
    exposure to that shared cap 4x (from ~2 req/hour down to ~0.5 req/hour).

    Set the JOBDATA_API_KEY env var / GitHub secret to a paid key to unlock pagination,
    date-range slicing, and remove the hourly throttle entirely."""

    headers = {"Authorization": f"Api-Key {JOBDATA_API_KEY}"} if JOBDATA_API_KEY else {}
    EXP_MAP = {"EN": "Entry-level", "MI": "Mid-level", "SE": "Senior-level", "EX": "Executive-level"}

    jobs, seen_ids = [], set()

    # Combine every search term into ONE request instead of one request per term.
    title_query = "|OR|".join(SEARCH_TERMS)

    params = {
        "title": title_query,
        "has_remote": "true",
        "country_code": "US",
        "language": "en",
        "description_str": "true",   # stripped plain-text description, no HTML
    }
    # date slicing / bigger pages only work with a paid key — avoids "parameter blocked" 400 errors
    if JOBDATA_API_KEY:
        params["max_age"] = "3"      # only pull the last 3 days per run
        params["page_size"] = "100"

    r = _get("https://jobdataapi.com/api/jobs/", params=params, headers=headers)
    if not r.ok:
        raise RuntimeError(f"jobdataapi.com returned {r.status_code}: {r.text[:200]}")

    data = r.json()
    for item in data.get("results", []):
        jid = str(item.get("id", ""))
        if not jid or jid in seen_ids:
            continue
        seen_ids.add(jid)

        title   = item.get("title", "").strip()
        company = (item.get("company") or {}).get("name", "").strip()
        url     = item.get("application_url") or ""
        loc     = item.get("location", "") or ""
        desc    = (item.get("description_string") or item.get("description") or "")[:500]

        # Salary — comes as yearly FTE numbers with a currency code
        sal_min = item.get("salary_min")
        sal_max = item.get("salary_max")
        sal_cur = item.get("salary_currency") or "USD"
        if sal_min and sal_max:
            lo = int(float(sal_min)) // 1000
            hi = int(float(sal_max)) // 1000
            salary = f"{sal_cur} {lo}K-{hi}K"
        elif sal_min:
            salary = f"{sal_cur} {int(float(sal_min))//1000}K+"
        else:
            salary = ""

        # Experience level — API uses "EN"/"MI"/"SE"/"EX" codes
        exp_code = item.get("experience_level") or ""
        exp_str  = EXP_MAP.get(exp_code, "")

        published = (item.get("published") or "")[:10]

        jobs.append(Job(
            source="foorilla",
            title=title,
            company=company,
            url=url,
            location=loc,
            remote=bool(item.get("has_remote")),
            posted_at=published,
            salary=salary,
            experience=exp_str,
            description=desc,
            native_id=jid,
        ))
    return jobs

def src_remotive():
    """remotive.com/api/remote-jobs (moved from remotive.io). Remotive's own docs
    ask for max ~4 requests/day and will block >2/min — gated to fire only at
    00/06/12/18 UTC so the 2-hour cron doesn't quietly violate that. No OR-query
    syntax exists here (unlike foorilla), so we pull unfiltered and let
    INCLUDE_KEYWORDS narrow it down, same pattern as weworkremotely."""
    r = _get("https://remotive.com/api/remote-jobs")
    if not r.ok:
        raise RuntimeError(f"remotive.com returned {r.status_code}")
    jobs = []
    for item in r.json().get("jobs", []):
        jobs.append(Job(
            source="remotive",
            title=item.get("title", ""),
            company=item.get("company_name", ""),
            url=item.get("url", ""),
            location=item.get("candidate_required_location", "") or "Remote",
            remote=True,
            posted_at=_parse_date(item.get("publication_date")),
            salary=item.get("salary", "") or "",
            description=re.sub(r"<[^>]+>", " ", item.get("description", "") or "")[:12000],
            native_id=str(item.get("id", "")),
        ))
    return jobs

def src_jobicy():
    """jobicy.com/api/v2/remote-jobs. Same 'a few times a day' guidance as Remotive
    (6hr publish delay on their end anyway) — gated to 00/06/12/18 UTC. No OR syntax
    for tags, so one request per core term, capped small to stay polite."""
    jobs, seen_ids = [], set()
    for term in ("healthcare", "human resources", "administrative"):
        r = _get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": term})
        if not r.ok:
            r.raise_for_status()
        for item in r.json().get("jobs", []):
            jid = str(item.get("id", ""))
            if not jid or jid in seen_ids:
                continue
            seen_ids.add(jid)
            sal_min, sal_max, cur = item.get("annualSalaryMin"), item.get("annualSalaryMax"), item.get("salaryCurrency", "USD")
            salary = f"{cur} {sal_min}-{sal_max}" if sal_min and sal_max else ""
            jobs.append(Job(
                source="jobicy",
                title=item.get("jobTitle", ""),
                company=item.get("companyName", ""),
                url=item.get("url", ""),
                location=item.get("jobGeo", "") or "Remote",
                remote=True,
                posted_at=_parse_date(item.get("pubDate")),
                salary=salary,
                experience=item.get("jobLevel", "") or "",
                description=(item.get("jobExcerpt", "") or "")[:12000],
                native_id=jid,
            ))
        time.sleep(1.0)
    return jobs

def src_himalayas():
    """himalayas.app/jobs/api/search. Data only refreshes every 24h on their end,
    so this fires once/day (00:00 UTC) regardless of the 2-hour cron — polling
    more often literally returns identical data per their own docs."""
    jobs = []
    for term in ("healthcare coordinator", "human resources coordinator"):
        r = _get("https://himalayas.app/jobs/api/search", params={"q": term, "country": "US", "sort": "recent"})
        if not r.ok:
            r.raise_for_status()
        for item in r.json().get("jobs", []):
            sal_min, sal_max, cur = item.get("minSalary"), item.get("maxSalary"), item.get("currency", "USD")
            salary = f"{cur} {sal_min}-{sal_max}" if sal_min and sal_max else ""
            seniority = item.get("seniority") or []
            jobs.append(Job(
                source="himalayas",
                title=item.get("title", ""),
                company=item.get("companyName", ""),
                url=item.get("applicationLink", ""),
                location=", ".join(item.get("locationRestrictions") or []) or "Remote (restrictions not listed)",
                remote=True,
                posted_at=_parse_date(item.get("pubDate")),
                salary=salary,
                experience=", ".join(seniority) if seniority else "",
                description=re.sub(r"<[^>]+>", " ", item.get("description", "") or "")[:12000],
                native_id=item.get("guid", ""),
            ))
        time.sleep(1.0)
    return jobs

def src_adzuna():
    """api.adzuna.com. Requires a free app_id/app_key from developer.adzuna.com —
    the only one of these four that needs credentials. Aggregates across many
    boards, so expect the HIGHEST overlap/dedup collision rate with your other
    sources of any adapter in this file."""
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        raise RuntimeError("ADZUNA_APP_ID/ADZUNA_APP_KEY not set — sign up free at developer.adzuna.com")
    jobs = []
    params = {
        "app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY,
        "what": "healthcare administrative human resources coordinator", "where": "remote",
        "max_days_old": 2, "results_per_page": 50, "content-type": "application/json",
    }
    r = _get("https://api.adzuna.com/v1/api/jobs/us/search/1", params=params)
    if not r.ok:
        raise RuntimeError(f"adzuna returned {r.status_code}: {r.text[:200]}")
    for item in r.json().get("results", []):
        loc = (item.get("location") or {}).get("display_name", "") or "Remote"
        sal_min, sal_max = item.get("salary_min"), item.get("salary_max")
        salary = f"${int(sal_min):,}-${int(sal_max):,}" if sal_min and sal_max else ""
        jobs.append(Job(
            source="adzuna",
            title=item.get("title", ""),
            company=(item.get("company") or {}).get("display_name", ""),
            url=item.get("redirect_url", ""),
            location=loc,
            remote=_is_remote(loc + " " + item.get("title", "")),
            posted_at=_parse_date(item.get("created")),
            salary=salary,
            description=(item.get("description", "") or "")[:12000],
            native_id=str(item.get("id", "")),
        ))
    return jobs

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
const origQuery = window.navigator.permissions ? window.navigator.permissions.query : null;
if (origQuery) {
    window.navigator.permissions.query = (params) => (
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(params)
    );
}
"""

def _new_stealth_page(browser):
    """Fresh browser context + page with a few of the well-known automation tells patched
    over (navigator.webdriver, plugins, languages). Doesn't guarantee passing Cloudflare's
    JS challenge - there is no free guarantee of that - but it's the standard free-tier
    mitigation and costs nothing to include."""
    context = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    page = context.new_page()
    page.add_init_script(_STEALTH_JS)
    return context, page

def src_indeed():
    """Indeed via Playwright (real Chromium, full JS execution) — the upgrade from the old
    curl_cffi version, which confirmed Indeed now needs actual JS rendering, not just a
    Cloudflare-edge bypass. Tries the legacy window.mosaic JS variable first (read directly
    out of the live page via page.evaluate, so it works even though that variable isn't in
    the raw HTML anymore); falls back to scraping rendered job cards via the data-jk
    attribute, which is Indeed's stable internal job-ID attribute and survives UI rebuilds
    better than CSS class names do.
    HONEST CAVEAT: full JS rendering solves the rendering problem, but Cloudflare bot
    management increasingly also scores IP reputation — and GitHub Actions runs from shared
    cloud IPs, which are treated with more suspicion than a home connection. This may still
    return 0 some or most of the time when run from GitHub Actions specifically."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("pip install playwright && playwright install chromium")
    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                     args=["--disable-blink-features=AutomationControlled"])
        try:
            for term in SEARCH_TERMS:
                context, page = _new_stealth_page(browser)
                url = "https://www.indeed.com/jobs?" + urlencode({
                    "q": term, "l": "Remote", "fromage": "1", "sort": "date",
                })
                try:
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)  # let any client-side fetch/render settle

                    blob = page.evaluate(
                        "() => (window.mosaic && window.mosaic.providerData && "
                        "window.mosaic.providerData['mosaic-provider-jobcards']) || null"
                    )
                    if blob:
                        results = (blob.get("metaData", {})
                                       .get("mosaicProviderJobCardsModel", {})
                                       .get("results", []))
                        for j in results:
                            jobkey = j.get("jobkey", "")
                            loc = j.get("formattedLocation", "")
                            jobs.append(Job(
                                source="indeed",
                                title=j.get("title", "") or j.get("displayTitle", ""),
                                company=j.get("company", ""),
                                url=f"https://www.indeed.com/viewjob?jk={jobkey}" if jobkey else "",
                                location=loc,
                                remote=_is_remote(loc + " " + j.get("title", "")),
                                salary=(j.get("salarySnippet") or {}).get("text", ""),
                                description=(j.get("snippet", "") or "")[:12000],
                                native_id=jobkey,
                            ))
                    else:
                        # Fallback: scrape rendered DOM via the data-jk job-id attribute.
                        cards = page.query_selector_all("[data-jk]")
                        seen_keys = set()
                        for el in cards:
                            jk = el.get_attribute("data-jk")
                            if not jk or jk in seen_keys:
                                continue
                            seen_keys.add(jk)
                            text = (el.inner_text() or "").strip()
                            if not text:
                                continue
                            first_line = text.split("\n")[0][:200]
                            jobs.append(Job(
                                source="indeed",
                                title=first_line,
                                company="",  # not reliably separable without verified selectors
                                url=f"https://www.indeed.com/viewjob?jk={jk}",
                                remote=_is_remote(text),
                                description=text[:1000],
                                native_id=jk,
                            ))
                except Exception as e:
                    raise RuntimeError(f"indeed.com via Playwright failed: {e}")
                finally:
                    context.close()
                time.sleep(INDEED_GLASSDOOR_DELAY)
        finally:
            browser.close()
    return jobs

def src_glassdoor():
    """Glassdoor via Playwright. Waits for [data-test="jobListing"], which current (2026)
    scraping write-ups specifically call out as Glassdoor's stable selector for job cards
    (more durable than their hashed class names). Per-card field extraction is best-effort —
    Glassdoor's internal markup inside each card isn't publicly documented, so this pulls
    the card's link+text generically rather than betting on exact sub-selectors. Same
    Cloudflare/IP-reputation caveat as Indeed applies — this is the least reliable adapter
    in the whole script even after this Playwright upgrade."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("pip install playwright && playwright install chromium")
    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
                                     args=["--disable-blink-features=AutomationControlled"])
        try:
            for term in SEARCH_TERMS:
                context, page = _new_stealth_page(browser)
                url = "https://www.glassdoor.com/Job/jobs.htm?" + urlencode({
                    "sc.keyword": term + " remote", "fromAge": "1",
                })
                try:
                    page.goto(url, timeout=30000, wait_until="domcontentloaded")
                    try:
                        page.wait_for_selector('[data-test="jobListing"]', timeout=15000)
                    except Exception:
                        pass  # proceed anyway; query_selector_all below just returns []
                    cards = page.query_selector_all('[data-test="jobListing"]')
                    for el in cards:
                        link_el = el.query_selector("a")
                        href = link_el.get_attribute("href") if link_el else ""
                        if href and not href.startswith("http"):
                            href = "https://www.glassdoor.com" + href
                        text = (el.inner_text() or "").strip()
                        if not text:
                            continue
                        lines = [l for l in text.split("\n") if l.strip()]
                        title = lines[0][:200] if lines else ""
                        jobs.append(Job(
                            source="glassdoor",
                            title=title,
                            company="",  # not reliably separable without verified selectors
                            url=href,
                            remote=_is_remote(text),
                            description=text[:1000],
                            native_id=href or title,
                        ))
                except Exception as e:
                    raise RuntimeError(f"glassdoor.com via Playwright failed: {e}")
                finally:
                    context.close()
                time.sleep(INDEED_GLASSDOOR_DELAY)
        finally:
            browser.close()
    return jobs

# ───────────────────────────── EMAIL ADAPTERS (Gmail IMAP) ─────────────────────────────
# Indeed and Glassdoor both get blocked by Cloudflare's IP-reputation scoring when scraped
# from GitHub Actions' shared cloud IPs (see src_indeed / src_glassdoor docstrings above).
# The workaround: instead of scraping the sites, read the job-alert emails they already
# send you. Set up saved-search alerts on each site with your target keywords + Remote
# filter, pointed at the Gmail account below, and these adapters parse those emails
# straight out of your inbox over IMAP — read-only, nothing is sent, deleted, or moved.
#
# One-time setup:
#   1. Turn on 2-Step Verification on the Gmail account: myaccount.google.com/security
#   2. Generate an App Password: myaccount.google.com/apppasswords (16 chars, no spaces)
#   3. Set GMAIL_USER / GMAIL_APP_PASSWORD as env vars locally, or as GitHub repo secrets
#      for Actions (see jobwatch.yml)
#   4. On Indeed: save a search (e.g. "detection engineer", Remote) and turn on email alerts
#   5. Test locally with test_indeed_email.py BEFORE enabling live Discord posting

IMAP_HOST = "imap.gmail.com"
_EMAIL_LOOKBACK_DAYS = 3      # only fetch alert emails from the last N days each run —
                              # keeps IMAP search fast and avoids re-parsing your whole inbox

def _imap_connect():
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD not set — email adapters disabled")
    con = imaplib.IMAP4_SSL(IMAP_HOST)
    con.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    con.select("INBOX", readonly=True)   # readonly=True: belt-and-suspenders, we never write
    return con

def _imap_search_from(con, sender: str):
    since = (datetime.now(timezone.utc) - timedelta(days=_EMAIL_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    typ, data = con.search(None, f'(FROM "{sender}" SINCE "{since}")')
    if typ != "OK":
        return []
    return data[0].split()

def _get_email_bodies(msg) -> tuple[str, str]:
    """Return (html_body, text_body) — either may be '' if that MIME part is absent."""
    html_body, text_body = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/html" and not html_body:
                html_body = text
            elif ctype == "text/plain" and not text_body:
                text_body = text
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace") if payload else ""
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            html_body = text
        else:
            text_body = text
    return html_body, text_body

def _decode_indeed_cts_link(href: str) -> str:
    """Indeed wraps links in individual 'job match' emails behind a cts.indeed.com click-
    tracking redirect. The real destination (containing the jk= job key) is embedded in a
    query parameter as gzip-compressed, base64-encoded bytes rather than a plain URL.
    Best-effort: tries the known param names, falls back to returning href unchanged if
    the structure doesn't match (so callers should treat the result as 'best guess', not
    guaranteed) — Indeed can and does change this wrapper without notice."""
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        for key in ("jns", "vjs", "d", "u"):
            if key in qs:
                raw = base64.urlsafe_b64decode(qs[key][0] + "=" * (-len(qs[key][0]) % 4))
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass  # wasn't actually gzipped, use raw bytes as-is
                decoded = raw.decode("utf-8", errors="ignore")
                m = re.search(r"jk=([a-f0-9]+)", decoded)
                if m:
                    return f"https://www.indeed.com/viewjob?jk={m.group(1)}"
                url_m = re.search(r"https?://[^\s\"'<>]+", decoded)
                if url_m:
                    return url_m.group(0)
    except Exception:
        pass
    return href  # couldn't decode — fall back to the wrapper link itself, still clickable

def _parse_indeed_individual_match(msg) -> "Job | None":
    """One-job-per-email format from match.indeed.com. Subject is '<Title> @ <Company>'."""
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    m = re.match(r"^\s*(.+?)\s+@\s+(.+?)\s*$", subject)
    if not m:
        return None
    title, company = m.group(1).strip(), m.group(2).strip()
    html_body, text_body = _get_email_bodies(msg)
    url, desc, loc, salary = "", "", "", ""
    if html_body and BeautifulSoup is not None:
        soup = BeautifulSoup(html_body, "html.parser")
        link = soup.find("a", href=re.compile(r"cts\.indeed\.com|indeed\.com/(rc|viewjob)"))
        if link:
            url = _decode_indeed_cts_link(link.get("href", ""))
        body_text = soup.get_text(" ", strip=True)
        desc = body_text[:12000]
        loc_m = re.search(r"(Remote|[A-Za-z .]+,\s*[A-Z]{2})\b", body_text)
        if loc_m:
            loc = loc_m.group(1)
        salary = display.extract_salary(body_text)
    elif text_body:
        desc = text_body[:12000]
        salary = display.extract_salary(text_body)
        loc = "Remote" if _is_remote(text_body) else ""
        link_m = re.search(r"https?://[^\s]+cts\.indeed\.com[^\s]+", text_body)
        if link_m:
            url = _decode_indeed_cts_link(link_m.group(0))
    salary = display.extract_salary(body_text if html_body and BeautifulSoup is not None else text_body) or salary
    jk_m = re.search(r"jk=([a-f0-9]+)", url)
    native_id = jk_m.group(1) if jk_m else url
    return Job(
        source="indeed_email", title=title, company=company, url=url,
        location=loc, remote=_is_remote(loc + " " + desc), salary=salary,
        description=desc, native_id=native_id,
        posted_at=_parse_date(msg.get("Date")),
    )

def _parse_indeed_digest(msg) -> list:
    """Multi-job saved-search digest from jobalert.indeed.com. Job keys appear in cleartext
    in href="/rc/clk/dl?jk=..." links, no cts-wrapper decoding needed here."""
    html_body, text_body = _get_email_bodies(msg)
    jobs = []
    posted = _parse_date(msg.get("Date"))
    if html_body and BeautifulSoup is not None:
        soup = BeautifulSoup(html_body, "html.parser")
        for a in soup.find_all("a", href=re.compile(r"/rc/clk/dl\?jk=|[?&]jk=")):
            href = a.get("href", "")
            jk_m = re.search(r"jk=([a-f0-9]+)", href)
            if not jk_m:
                continue
            jk = jk_m.group(1)
            title = a.get_text(strip=True)
            if not title:
                continue
            # Company/location/salary typically sit in sibling/parent text near the link —
            # best-effort, since digest HTML structure isn't publicly documented.
            container = a.find_parent(["td", "div", "li"]) or a.parent
            block = container.get_text(" ", strip=True) if container else ""
            company, loc, salary = "", "", ""
            comp_m = re.search(r"^" + re.escape(title) + r"\s*[-–|]?\s*([A-Za-z0-9&.,' ]+?)(?:\s{2,}|$)", block)
            if comp_m:
                company = comp_m.group(1).strip()
            loc_m = re.search(r"(Remote|[A-Za-z .]+,\s*[A-Z]{2})\b", block)
            if loc_m:
                loc = loc_m.group(1)
            salary = display.extract_salary(block)
            jobs.append(Job(
                source="indeed_email", title=title, company=company,
                url=f"https://www.indeed.com/viewjob?jk={jk}",
                location=loc, remote=_is_remote(loc + " " + block), salary=salary,
                description=block[:1500], native_id=jk, posted_at=posted,
            ))
    elif text_body:
        # Plain-text fallback: much rougher, one line per job at best.
        for line in text_body.splitlines():
            jk_m = re.search(r"jk=([a-f0-9]+)", line)
            if not jk_m:
                continue
            jobs.append(Job(
                source="indeed_email", title=line.strip()[:200], company="",
                url=f"https://www.indeed.com/viewjob?jk={jk_m.group(1)}",
                remote=_is_remote(line), description=line.strip()[:500],
                native_id=jk_m.group(1), posted_at=posted,
            ))
    return jobs

def src_indeed_email():
    """Gmail IMAP adapter covering both Indeed email formats:
      - match.indeed.com          -> one job per email ('<Title> @ <Company>' subject)
      - jobalert.indeed.com       -> digest emails, multiple jobs, jk= in cleartext links
    Read-only IMAP; never sends/deletes/moves mail. See test_indeed_email.py to dry-run
    this against your real inbox before it's wired into live Discord posting."""
    con = _imap_connect()
    jobs = []
    try:
        for msg_id in _imap_search_from(con, "match.indeed.com"):
            typ, data = con.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            job = _parse_indeed_individual_match(msg)
            if job:
                jobs.append(job)
        for msg_id in _imap_search_from(con, "jobalert.indeed.com"):
            typ, data = con.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not data or not data[0]:
                continue
            msg = email.message_from_bytes(data[0][1])
            jobs.extend(_parse_indeed_digest(msg))
    finally:
        try:
            con.close()
        except Exception:
            pass
        con.logout()
    return jobs

def src_glassdoor_email():
    """STUB. Same IMAP pattern as src_indeed_email(), but Glassdoor's digest HTML structure
    isn't built out yet — need one real 'Show original' HTML export of a Glassdoor job-alert
    digest email to model the parser against (sender address, subject format, and where the
    job link/company/location/salary actually live in their markup all still unknown here).
    Left disabled in ENABLED_SOURCES until that sample is provided and this is filled in."""
    raise RuntimeError(
        "glassdoor_email not implemented yet — needs a raw Glassdoor digest HTML sample first."
    )

ADAPTERS = {
    "nodesk": src_nodesk,
    "careerhound": src_careerhound,
    "cybersecurityroles": src_cybersecurityroles,
    "hiringcafe": src_hiringcafe,
    "linkedin": src_linkedin,
    "wellfound": src_wellfound,
    "builtin": src_builtin,
    "remoteok": src_remoteok,
    "weworkremotely": src_weworkremotely,
    "isecjobs": src_isecjobs,
    "foorilla": src_foorilla,
    "indeed": src_indeed,
    "glassdoor": src_glassdoor,
    "indeed_email": src_indeed_email,
    "glassdoor_email": src_glassdoor_email,
    "remotive": src_remotive,
    "jobicy": src_jobicy,
    "himalayas": src_himalayas,
    "adzuna": src_adzuna,
}

# ───────────────────────────── STORAGE / DEDUP ─────────────────────────────

def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            fingerprint TEXT PRIMARY KEY,
            native_id   TEXT,
            title       TEXT,
            company     TEXT,
            url         TEXT,
            first_seen  TEXT,
            last_seen   TEXT,
            last_posted TEXT,
            repost_count INTEGER DEFAULT 0,
            sent        INTEGER DEFAULT 0
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_native ON seen(native_id)")
    con.commit()
    return con

def is_known(con, job):
    cur = con.execute("SELECT fingerprint, last_posted, repost_count, sent FROM seen WHERE fingerprint=? OR (native_id!='' AND native_id=?)",
                      (job.fingerprint(), job.native_id))
    return cur.fetchone()

def record(con, job, now_iso, repost_count, sent):
    con.execute("""
        INSERT INTO seen (fingerprint, native_id, title, company, url, first_seen, last_seen, last_posted, repost_count, sent)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            last_seen=excluded.last_seen,
            last_posted=excluded.last_posted,
            repost_count=excluded.repost_count,
            sent=MAX(seen.sent, excluded.sent)
    """, (job.fingerprint(), job.native_id, job.title, job.company, job.url,
          now_iso, now_iso, job.posted_at, repost_count, int(sent)))
    con.commit()

# ───────────────────────────── GHOST SCORING ─────────────────────────────

VAGUE = re.compile(r"\b(fast[- ]paced|dynamic|self[- ]starter|wear many hats|rockstar|ninja|hit the ground running)\b", re.I)

def ghost_score(job, prior_row):
    """Return (score, flags). Higher = more likely a ghost / repost / placeholder."""
    score, flags = 0, []
    age = _age_days(job.posted_at)
    if age is not None and age > MAX_AGE_DAYS:
        score += 2; flags.append(f"stale {age}d")
    # Repost detection: same role seen before but with a *newer* posted date.
    if prior_row:
        _, last_posted, repost_count, _sent = prior_row
        if job.posted_at and last_posted and job.posted_at > last_posted:
            repost_count = (repost_count or 0) + 1
            if repost_count >= REPOST_GHOST_THRESHOLD:
                score += 2; flags.append(f"reposted x{repost_count}")
            else:
                flags.append(f"reposted x{repost_count}")
        return score, flags, repost_count
    # Vague / placeholder description
    if job.description and len(job.description) < MIN_DESC_CHARS:
        score += 1; flags.append("thin desc")
    if VAGUE.search(job.description or ""):
        score += 1; flags.append("buzzwords")
    return score, flags, 0

def ats_alive(url):
    if not VERIFY_ON_ATS or not url:
        return True
    try:
        r = SESSION.head(url, allow_redirects=True, timeout=12)
        return r.status_code < 400
    except requests.RequestException:
        return True  # don't punish on network hiccups

# ───────────────────────────── DISCORD ─────────────────────────────

SOURCE_COLOR = {
    "nodesk": 0x2ecc71, "careerhound": 0x9b59b6, "cybersecurityroles": 0xe74c3c,
    "hiringcafe": 0xf1c40f, "linkedin": 0x0a66c2, "builtin": 0x1abc9c, "wellfound": 0x34495e,
    "remoteok": 0xff5722, "weworkremotely": 0x00bcd4, "isecjobs": 0x607d8b,
    "foorilla": 0x1a1a2e,   # foorilla dark navy
    "indeed": 0x2557a7, "glassdoor": 0x0caa41,
    "indeed_email": 0x003a9b, "glassdoor_email": 0x0caa41,
    "remotive": 0x00d2b4, "jobicy": 0xff8a00, "himalayas": 0x6c5ce7, "adzuna": 0x0d6efd,
}

DISCORD_MAX_EMBEDS = 10
# Discord allows 6,000 embed characters across the entire message. Leave headroom
# for formatting changes and any counting differences on Discord's side.
DISCORD_MAX_EMBED_CHARS = 5500

def to_embed(job):
    fields = matching.fit_fields(job.fit) if job.fit else []
    if job.location:
        fields.append({"name": "Location", "value": display.clip(job.location, 150), "inline": True})
    fields.append({"name": "Work arrangement", "value": workplace.label(display.work_mode(job)) + ("\n" + job.work_mode_source if job.work_mode_source else "\n" + job.work_mode_note if job.work_mode_note else ""), "inline": True})
    fields.append({"name": "Compensation", "value": display.clip(display.compensation(job.salary), 150), "inline": True})
    experience = _extract_experience(job.title, job.description, job.experience)
    if experience:
        fields.append({"name": "Experience", "value": display.clip(experience, 100), "inline": True})
    if job.posted_at:
        fields.append({"name": "Posted", "value": job.posted_at[:10], "inline": True})
    restrictions = display.remote_details(job.description)
    if restrictions:
        fields.append({"name": "Location / timezone details", "value": restrictions, "inline": False})
    warning_flags = [flag for flag in job.ghost_flags if flag != "no salary"]
    if warning_flags:
        fields.append({"name": "⚠ Listing signals", "value": display.clip(", ".join(warning_flags), 200), "inline": False})
    company_line = f"**{display.clip(job.company, 150)}**\n" if job.company else ""
    embed = {
        "title": display.clip(job.title, 240) + ("  ⚠" if warning_flags else ""),
        "description": company_line + display.role_summary(job.description),
        "color": SOURCE_COLOR.get(job.source, 0x95a5a6),
        "footer": {"text": f"via {job.source} · Cleo Jobs · cards v4 / experience v1"},
        "fields": fields,
    }
    if re.match(r"^https?://[^\s]+$", job.url or "", re.IGNORECASE):
        embed["url"] = job.url
    return embed

def _embed_char_count(embed):
    """Count the strings Discord includes in its 6,000-character embed limit."""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    total += len(embed.get("author", {}).get("name", ""))
    for field in embed.get("fields", []):
        total += len(field.get("name", "")) + len(field.get("value", ""))
    return total

def _discord_embed_batches(jobs):
    """Yield payload-safe batches by both embed count and aggregate characters."""
    batch, char_count = [], 0
    for job in jobs:
        embed = to_embed(job)
        embed_chars = _embed_char_count(embed)
        if batch and (
            len(batch) >= DISCORD_MAX_EMBEDS
            or char_count + embed_chars > DISCORD_MAX_EMBED_CHARS
        ):
            yield batch
            batch, char_count = [], 0
        batch.append(embed)
        char_count += embed_chars
    if batch:
        yield batch

def post_discord(jobs, dry=False, on_delivered=None):
    if not jobs:
        return
    if not dry and not DISCORD_WEBHOOK_URL.startswith("http"):
        raise RuntimeError("DISCORD_WEBHOOK_URL is missing")
    if dry:
        for j in jobs:
            print(f"  [WOULD SEND] {j.source:18} {j.company[:25]:25} | {j.title[:60]}"
                  + (f"  ⚠{j.ghost_flags}" if j.ghost_flags else ""))
        return
    delivered_count = 0
    for embeds in _discord_embed_batches(jobs):
        payload = {"username": "JobWatch", "embeds": embeds}
        while True:
            r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
            if r.status_code == 429:
                wait = r.json().get("retry_after", 2)
                time.sleep(float(wait) + 0.3)
                continue
            if not r.ok:
                print(f"  [DISCORD] webhook error {r.status_code}: {r.text[:500]}", file=sys.stderr)
            r.raise_for_status()
            if on_delivered:
                on_delivered(jobs[delivered_count:delivered_count + len(embeds)])
            delivered_count += len(embeds)
            break
        time.sleep(1.2)

# ───────────────────────────── ORCHESTRATION ─────────────────────────────

SOURCE_STATUS = {}
SOURCE_INTERVALS = {"remotive": 6, "jobicy": 6, "himalayas": 24}

def run_source(name, dry=False):
    # Elapsed intervals tolerate delayed Actions schedules and manual runs.
    con = None
    SOURCE_STATUS.pop(name, None)
    try:
        hours = SOURCE_INTERVALS.get(name)
        if hours:
            con = sqlite3.connect(DB_PATH)
            con.execute("CREATE TABLE IF NOT EXISTS source_poll (name TEXT PRIMARY KEY, last_success REAL)")
            row = con.execute("SELECT last_success FROM source_poll WHERE name=?", (name,)).fetchone()
            if row and time.time() - row[0] < hours * 3600:
                SOURCE_STATUS[name] = f"scheduled skip: within {hours}h polling interval"
                print(f"  {name}: {SOURCE_STATUS[name]}")
                return []
        jobs = ADAPTERS[name]()
        SOURCE_STATUS.setdefault(name, f"ok: fetched {len(jobs)}")
        if con and not dry:
            con.execute("INSERT OR REPLACE INTO source_poll VALUES (?,?)", (name, time.time()))
            con.commit()
        print(f"  {name}: {SOURCE_STATUS[name]}")
        return jobs
    except Exception as e:
        SOURCE_STATUS[name] = f"failed: {e}"
        print(f"  {name}: SKIPPED ({e})")
        return []
    finally:
        if con:
            con.close()

# The rolling #job-map trend chart lives in jobwatch_trends.py.

def enrich_job_details(job):
    """Read public JobPosting metadata when a feed/email lacks usable requirements."""
    if BeautifulSoup is None:
        return False
    if not re.match(r"^https?://[^\s]+$", job.url or "", re.I):
        return False
    metadata_updated = False
    try:
        response = _get(job.url, timeout=12)
        if not response.ok:
            return False  # Respect 403/429 responses; no bypass or retry storm.
        if len(response.content) > 2_000_000:
            return False
        soup = BeautifulSoup(response.text, "html.parser")
        if job.source == "linkedin" and not job.work_mode_source:
            mode, source = workplace.from_soup(soup, job.title, job.company)
            metadata_updated = workplace.apply(job, mode, source)
            if not metadata_updated:
                job.work_mode_note = source
        def postings(value):
            if isinstance(value, list):
                for item in value:
                    yield from postings(item)
            elif isinstance(value, dict):
                kind = value.get("@type", "")
                if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                    yield value
                if "@graph" in value:
                    yield from postings(value["@graph"])
        for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                records = list(postings(json.loads(node.string or node.get_text())))
            except (ValueError, TypeError):
                continue
            for record in records:
                # An aggregator may embed several other jobs: require the same title.
                if _norm(record.get("title", "")) != _norm(job.title):
                    continue
                organization = record.get("hiringOrganization")
                if isinstance(organization, dict) and organization.get("name") and job.company:
                    if _norm(organization["name"]) != _norm(job.company):
                        continue
                body = record.get("description", "")
                if not isinstance(body, str) or not body.strip():
                    continue
                job.description = BeautifulSoup(body, "html.parser").get_text("\n", strip=True)[:20000]
                if workplace.normalize(record.get("jobLocationType")):
                    workplace.apply(job, record["jobLocationType"], "Posting structured metadata")
                base = record.get("baseSalary")
                if isinstance(base, dict):
                    value = base.get("value")
                    if isinstance(value, dict) and base.get("currency") == "USD":
                        low, high = value.get("minValue", value.get("value")), value.get("maxValue", value.get("value"))
                        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                            unit = str(value.get("unitText", "")).upper()
                            suffix = " a year" if unit == "YEAR" else " an hour" if unit == "HOUR" else ""
                            job.salary = f"USD {low}-{high}{suffix}"
                return True
    except (requests.RequestException, ValueError, TypeError):
        pass
    return metadata_updated


def pass_once(dry=False):
    con = db_init()
    try:
        return _pass_once(con, dry)
    finally:
        con.close()

def _pass_once(con, dry=False):
    now_iso = datetime.now(timezone.utc).isoformat()
    raw = []
    SOURCE_STATUS.clear()
    profile = matching.load_profile()
    for name, on in ENABLED_SOURCES.items():
        if on:
            raw.extend(run_source(name, dry=dry))

    # Pull the application ledger (Google Sheet) first so the readiness octagram
    # can overlay real recruiter/interview traction on top of the market-fit
    # polygon. Non-fatal: no ledger config / a failed fetch just omits the
    # overlay.
    try:
        jp.run_pass(DB_PATH, dry=dry)
    except Exception as e:
        print(f"  [PIPELINE] pass error (non-fatal): {e}")

    # Feed the WHOLE pre-filter scraped market into the readiness octagram —
    # not just what survives keyword/remote/ghost filtering below — so each
    # category's averages reflect real market demand, not your narrowed feed.
    try:
        jr.run_pass(raw, DB_PATH, JOB_READINESS_WEBHOOK_URL, dry=dry, force=FORCE_READINESS_POST)
    except Exception as e:
        print(f"  [READINESS] pass error (non-fatal): {e}")

    try:
        jse.run_pass(raw, DB_PATH, NEEDED_SKILLS_WEBHOOK_URL, dry=dry, force=FORCE_SKILLS_EXPORT)
    except Exception as e:
        print(f"  [SKILLS-EXPORT] pass error (non-fatal): {e}")

    to_send, seen_fp_this_pass = [], set()
    detail_requests = 0
    excluded_companies_norm = {_norm(c) for c in EXCLUDE_COMPANIES}
    drop_reasons = defaultdict(lambda: defaultdict(int))   # source -> reason -> count

    def drop(job, reason):
        drop_reasons[job.source][reason] += 1

    for job in raw:
        if not job.url or not job.title:
            drop(job, "missing url/title")
            continue
        if not _matches_wanted(job.title):
            drop(job, "title didn't match keywords")
            continue
        if job.company and _norm(job.company) in excluded_companies_norm:
            drop(job, "excluded company")
            continue
        # Search filters are not evidence that an individual listing is remote.
        workplace.apply_override(job)
        known = is_known(con, job)
        if job.source == "linkedin" and LINKEDIN_FETCH_FULL_DESC and not (known and known[3]):
            if not job.description or not job.work_mode_source:
                _fetch_linkedin_details(job)
                workplace.apply_override(job)  # User-confirmed badge has explicit provenance.
                time.sleep(LINKEDIN_DETAIL_DELAY)
        job.work_mode = display.work_mode(job)
        job.remote = job.work_mode == "Remote"
        if REMOTE_ONLY and not job.remote:
            drop(job, "work arrangement: " + job.work_mode)
            continue

        fp = job.fingerprint()
        if fp in seen_fp_this_pass:            # collapse cross-source dupes within the pass
            drop(job, "duplicate within this pass")
            continue
        seen_fp_this_pass.add(fp)

        prior = is_known(con, job)
        # prior tuple: (fingerprint, last_posted, repost_count, sent)
        prior_for_score = (prior[0], prior[1], prior[2], prior[3]) if prior else None
        score, flags, repost_count = ghost_score(job, prior_for_score)
        job.ghost_flags = flags

        already_sent = bool(prior and prior[3])
        is_repost = bool(prior and "reposted" in " ".join(flags))

        # Skip if we've already sent it and it's not a fresh repost worth re-flagging.
        if already_sent and not is_repost:
            record(con, job, now_iso, repost_count, sent=True)
            drop(job, "already sent in a prior run")
            continue

        ghost = score >= GHOST_SCORE_SUPPRESS
        alive = ats_alive(job.url)
        if (ghost and SUPPRESS_GHOSTS) or not alive:
            record(con, job, now_iso, repost_count, sent=already_sent)
            drop(job, "ghost-suppressed" if ghost else "dead link (ATS check failed)")
            continue

        job.experience = _extract_experience(job.title, job.description, job.experience)

        job.fit = matching.score_job(job, profile)
        if (job.fit["score"] is None or display.work_mode(job) == "Not specified") and detail_requests < 5 and not dry:
            detail_requests += 1
            if enrich_job_details(job):
                job.experience = _extract_experience(job.title, job.description, job.experience)
                job.work_mode = display.work_mode(job)
                job.fit = matching.score_job(job, profile)
        # Only candidates that cleared the initial 70% gate spend requests on
        # a verified public copy. The alert always remains visible; a new
        # blocker changes the card to BLOCKED AFTER ENRICHMENT and prevents
        # jobwatch_apply from forwarding it to the private application queue.
        if applications.eligible(job) and not dry:
            enriched = online_enrichment.enrich_job_posting(asdict(job))
            job.online_posting = enriched.get("online_posting", {})
            if job.online_posting.get("status") == "retrieved":
                job.fit = matching.rescore_after_online_enrichment(
                    job, job.online_posting.get("text", ""), profile)
                signals = job.fit["post_enrichment"]["signals"]
                if signals.get("work_mode") not in ("", "Not specified"):
                    job.work_mode = signals["work_mode"]
                    job.work_mode_source = signals.get("work_mode_source", "")
                    job.remote = job.work_mode == "Remote"
                if signals.get("salary_evidence"):
                    job.salary = signals["salary_evidence"]
        to_send.append(job)
        # Mark delivered only after Discord accepts this job's batch.
        if not dry:
            record(con, job, now_iso, repost_count, sent=False)

    print(f"  -> {len(to_send)} new job(s) to deliver")
    if drop_reasons:
        print("  -- why jobs got dropped, by source:")
        for source, reasons in drop_reasons.items():
            breakdown = ", ".join(f"{count} {reason}" for reason, count in
                                   sorted(reasons.items(), key=lambda kv: -kv[1]))
            print(f"     {source}: {breakdown}")
    to_send.sort(key=lambda j: (not any("BLOCKER:" in x for x in j.fit.get("eligibility", []) + j.fit.get("preferences", [])), j.fit.get("score") is not None, j.fit.get("score") or 0), reverse=True)
    def delivered(batch):
        for job in batch:
            con.execute("UPDATE seen SET sent=1 WHERE fingerprint=?", (job.fingerprint(),))
        con.commit()
    summary = ["## JobWatch source summary", "", f"Fetched {len(raw)} listings; new alert candidates: {len(to_send)}.", ""]
    summary.extend(f"- {name}: {status}" for name, status in SOURCE_STATUS.items())
    summary.extend(f"- {source} filters: " + ", ".join(f"{reason}={count}" for reason,count in reasons.items()) for source,reasons in drop_reasons.items())
    summary_text = "\n".join(summary) + "\n"
    print(summary_text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as report:
            report.write(summary_text)
    try:
        jt.run_pass(to_send, DB_PATH, JOB_MAP_WEBHOOK_URL, dry=dry,
                    force=FORCE_JOB_MAP_POST)
    except Exception as e:
        print(f"  [JOB-TREND] pass error (non-fatal): {e}")
    try:
        applications.run_pass(to_send, DB_PATH, dry=dry)
    except Exception as e:
        print(f"  [JOB-APPLY] intake error ({type(e).__name__}); job alerts continue")
    post_discord(to_send, dry=dry, on_delivered=delivered)
    con.close()
    return len(to_send)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="run forever, sleeping POLL_MINUTES between passes")
    ap.add_argument("--dry", action="store_true", help="print instead of posting to Discord")
    args = ap.parse_args()
    if args.loop:
        while True:
            print(f"\n=== pass @ {datetime.now().isoformat(timespec='seconds')} ===")
            try:
                pass_once(dry=args.dry)
            except Exception as e:
                print(f"pass error: {e}", file=sys.stderr)
            time.sleep(POLL_MINUTES * 60)
    else:
        pass_once(dry=args.dry)

if __name__ == "__main__":
    main()

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE NOTES (read these — they save you hours)
#
# nodesk             RSS at https://nodesk.co/remote-jobs/index.xml — reliable, verified.
# careerhound        DISABLED. Confirmed via live check: signup-gated paid product, zero
#                    public job data. Not automatable without violating their ToS.
# cybersecurityroles DISABLED. That exact domain doesn't resolve to anything. The closest
#                    name match is secroles.com, but it's also gated (3 free jobs, sign up
#                    for the rest). Confirm the real site you meant and tell Claude the URL.
# hiringcafe         Confirmed broken at the architecture level, not just Cloudflare: their
#                    old /api/search-jobs endpoint is dead, and the live site only filters
#                    results client-side via JS after page load — a plain script can't
#                    replicate that. Real fix: the Apify HiringCafe actor (~$1.15-1.25/1000
#                    jobs, run-sync-get-dataset-items endpoint) or a Playwright-driven
#                    scraper since you already use Playwright for your flight-deal finder.
# linkedin           Guest endpoint works without login but is against ToS and rate-limited.
#                    Kept tiny (4 terms x 2 pages, 2.5s sleep, last-24h filter). If you get a
#                    429, the adapter backs off for the pass. Don't run this from your work IP.
# wellfound          Disabled. Needs Playwright (which you already use). When you wire it,
#                    reuse your existing Chromium session and parse the role cards.
# builtin            No stable public feed. Capture the XHR its search page fires (DevTools →
#                    Network → Fetch/XHR) and model it like src_hiringcafe.
# remoteok           Free official JSON API (remoteok.com/api), no login, no proxy. Reliable.
# weworkremotely     Free RSS, no dedicated security category so we pull the all-jobs feed
#                    and rely on INCLUDE_KEYWORDS to narrow it down.
# isecjobs           Cybersecurity-specific, fully public, no login wall observed — but no
#                    stable feed, so this is a best-effort HTML scrape built from a page
#                    snapshot. If company/location come through blank in your dry run, send
#                    the output back and the selectors in src_isecjobs() need tightening.
#
# indeed / glassdoor DISABLED as of this version. Both were upgraded from curl_cffi (TLS-
#                    spoof) to Playwright (real headless Chromium) since both sites render
#                    job cards via client-side JS — but Cloudflare's bot management also
#                    scores IP reputation separately, and GitHub Actions' shared cloud IPs
#                    get treated with more suspicion than a home connection regardless of
#                    JS rendering. Left in the file (ADAPTERS still has them) in case you
#                    ever want to run just these two from a home network instead of Actions,
#                    but ENABLED_SOURCES now points to the email adapters below instead.
#
# indeed_email       Gmail IMAP adapter — reads your Indeed job-alert emails instead of
#                    scraping indeed.com, sidestepping the Cloudflare IP problem entirely.
#                    Handles two formats: match.indeed.com (one job per email, subject
#                    "<Title> @ <Company>", job link wrapped behind a cts.indeed.com
#                    redirect that gets gzip+base64 decoded to recover the real jk= job
#                    key) and jobalert.indeed.com digests (multiple jobs per email, jk=
#                    already in cleartext in the href). Requires GMAIL_USER and
#                    GMAIL_APP_PASSWORD (a Gmail App Password, not your real password —
#                    needs 2-Step Verification turned on first). Read-only IMAP: never
#                    sends, deletes, or moves mail. Set up an Indeed saved search with
#                    email alerts turned on before this will return anything. Test with
#                    test_indeed_email.py against your real inbox before trusting it live —
#                    Indeed's email HTML structure isn't publicly documented and can drift.
# glassdoor_email    STUB, disabled. Same IMAP pattern as indeed_email but not built yet —
#                    needs one real "Show original" HTML export of a Glassdoor job-alert
#                    digest email before the parser can be written against real structure.
#
# remotive           remotive.com/api/remote-jobs (moved from remotive.io). Free, no key.
#                    Gated via _hourly_gate(6) to fire only at 00/06/12/18 UTC — their own
#                    docs ask for max ~4 req/day, and the 2-hour cron would otherwise hit
#                    it 12x/day. Highest overlap with remoteok/weworkremotely of the four
#                    new sources — expect real dedup collisions there.
# jobicy             jobicy.com/api/v2/remote-jobs. Free, no key. Same 4x/day gate as
#                    Remotive (their docs: 6hr publish delay, "a few times a day" is enough).
# himalayas          himalayas.app/jobs/api/search. Free, no key. Data only refreshes every
#                    24h server-side, so gated to fire once/day (00:00 UTC) — polling more
#                    often would just return identical results per their own docs.
# adzuna             api.adzuna.com. Needs a free ADZUNA_APP_ID/ADZUNA_APP_KEY from
#                    developer.adzuna.com — disabled by default until those secrets are set.
#                    Aggregates across many boards itself, so expect the highest cross-
#                    source dedup collision rate of any adapter here.
# ─────────────────────────────────────────────────────────────────────────────

