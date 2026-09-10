"""Workplace labels from scoped posting metadata, never recommendations or search filters."""
import json
import re
from datetime import date
from pathlib import Path

OVERRIDES_PATH = Path(__file__).parent / "profiles" / "workplace_overrides.json"

def normalize(value):
    text = re.sub(r"[\s_–—-]+", " ", str(value or "").strip().lower())
    text = re.sub(r"^[✓✔]\s*", "", text)
    return {"on site":"Onsite", "onsite":"Onsite", "in person":"Onsite",
            "in office":"Onsite", "remote":"Remote", "fully remote":"Remote",
            "telecommute":"Remote", "hybrid":"Hybrid"}.get(text, "")

def label(mode):
    return "On-site (in person)" if mode == "Onsite" else mode

def linkedin_id(url):
    match = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)(?:[/?]|$)", url or "")
    return match.group(1) if match else ""

def from_soup(soup, title="", company=""):
    """Only inspect a posting's header, labeled criteria, and same-title JobPosting."""
    def norm(value):
        return re.sub(r"\W+", " ", str(value or "").lower()).strip()
    candidates = []
    for scope in soup.select(".top-card-layout, .job-details-jobs-unified-top-card__container--two-pane"):
        heading = scope.select_one("h1, h2.topcard__title")
        if title and (not heading or norm(heading.get_text(" ", strip=True)) != norm(title)):
            continue
        for node in scope.select("button, [class*='workplace'], [class*='job-insight'], [class*='flavor']"):
            text = node.get_text(" ", strip=True)
            mode = normalize(text)
            if not mode and "workplace type is" in text.lower():
                match = re.search(r"workplace type is\s+(on[- ]site|in[- ]person|hybrid|remote)\b", text, re.I)
                mode = normalize(match.group(1)) if match else ""
            if mode:
                candidates.append((mode, "LinkedIn posting badge"))
    for item in soup.select(".description__job-criteria-item"):
        heading = item.select_one(".description__job-criteria-subheader")
        value = item.select_one(".description__job-criteria-text")
        if heading and value and heading.get_text(" ",strip=True).lower() in ("workplace type","workplace","work arrangement","location type"):
            mode = normalize(value.get_text(" ",strip=True))
            if mode:
                candidates.append((mode,"LinkedIn workplace field"))
    def records(value):
        if isinstance(value,list):
            for item in value:
                yield from records(item)
        elif isinstance(value,dict):
            kind=value.get("@type")
            if kind == "JobPosting" or isinstance(kind,list) and "JobPosting" in kind:
                yield value
            yield from records(value.get("@graph",[]))
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data=json.loads(script.string or script.get_text())
        except (ValueError,TypeError):
            continue
        for record in records(data):
            if title and norm(record.get("title")) != norm(title):
                continue
            organization=record.get("hiringOrganization") or {}
            if company and isinstance(organization,dict) and organization.get("name") and norm(organization["name"]) != norm(company):
                continue
            mode=normalize(record.get("jobLocationType"))
            if mode:
                candidates.append((mode,"Posting structured metadata"))
    modes={mode for mode,_ in candidates}
    if len(modes)==1:
        return candidates[0]
    return "", "Conflicting workplace metadata" if modes else "Badge not exposed in public metadata"

def apply(job, mode, source):
    if normalize(mode):
        job.work_mode=normalize(mode)
        job.remote=job.work_mode=="Remote"
        job.work_mode_source=source
        return True
    return False

def apply_override(job, path=OVERRIDES_PATH, today=None):
    """User-confirmed overrides are tied to immutable source job IDs and expire."""
    key=linkedin_id(job.url) if job.source=="linkedin" else ""
    if not key:
        return False
    try:
        with open(path,encoding="utf-8") as stream:
            data=json.load(stream)
        row=data.get("linkedin",{}).get(key,{})
        if not row or date.fromisoformat(row["expires"]) < (today or date.today()):
            return False
        return apply(job,row.get("mode"),"User-confirmed LinkedIn badge")
    except (OSError,ValueError,KeyError,TypeError):
        return False

