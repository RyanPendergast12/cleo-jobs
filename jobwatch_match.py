"""Explainable experience matching. No model calls, scraped instructions, or fabricated credentials."""
import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import jobwatch_display as display

PROFILE_PATH = Path(__file__).parent / "profiles" / "experience.json"
# Evidence strength measures the kind of supporting experience, not hiring probability.
STRENGTH = {"production": 1.0, "diagnosis": .85, "design": .6, "lab": .5, "reported": .35}
CATALOG = {
    "Patient scheduling": ["patient scheduling", "appointment scheduling", "schedule patients"],
    "Patient intake": ["patient intake", "patient registration", "register patients"],
    "Check-in and check-out": ["check-in", "check in", "check-out", "check out"],
    "Insurance verification": ["insurance verification", "verify insurance", "benefits verification"],
    "Referral verification": ["referral verification", "verify referrals", "referral authorization"],
    "Patient communication": ["patient communication", "communicate with patients", "patient-facing"],
    "Provider communication": ["provider communication", "communicate with providers", "physician communication"],
    "Medical office operations": ["medical office", "clinic operations", "front office"],
    "Phreesia": ["phreesia"],
    "Copayments": ["copayments", "co-payments", "copays"],
    "Billing statements": ["billing statements", "patient billing", "billing inquiries"],
    "Vitals": ["patient vitals", "vital signs", "take vitals"],
    "Specimen handling": ["specimen handling", "specimen collection", "blood samples", "laboratory samples"],
    "Resource coordination": ["resource coordination", "resource referrals", "community resources"],
    "Onboarding": ["employee onboarding", "new hire onboarding", "onboarding"],
    "Offboarding": ["employee offboarding", "offboarding"],
    "HR records": ["hr records", "personnel records", "employee records", "recordkeeping"],
    "Compliance documentation": ["compliance documentation", "compliance records", "federal compliance"],
    "Records auditing": ["records auditing", "record audits", "data audits", "audit records"],
    "Data entry": ["data entry"],
    "Workday": ["workday"],
    "Oracle": ["oracle hcm", "oracle hr", "oracle"],
    "ADP": ["adp"],
    "Employee relations": ["employee relations"],
    "Benefits": ["employee benefits", "benefits administration", "benefits"],
    "Employee wellness": ["employee wellness", "wellness programs"],
    "Expense management": ["expense management", "expense reimbursement", "expense reports"],
    "Travel coordination": ["travel coordination", "corporate travel", "travel arrangements"],
    "Customer service": ["customer service", "client service", "guest service"],
    "POS": ["point of sale", "pos system", "pos systems"],
    "Payment processing": ["payment processing", "process payments", "card transactions", "cash handling"],
    "Membership sales": ["membership sales", "sell memberships"],
    "Social media": ["social media", "content creation"],
    "Email marketing": ["email marketing", "email campaigns"],
    "Community outreach": ["community outreach", "community engagement"],
    "Crisis hotline support": ["crisis hotline", "crisis support", "care advocate"],
    "Certified Medical Assistant": ["certified medical assistant", "cma certification", "cma required"],
    "CPR/BLS": ["cpr certification", "bls certification", "basic life support"],
    "Medical Coding Certification": ["cpc certification", "certified professional coder", "medical coding certification"],
}
CERTS = {"Certified Medical Assistant", "CPR/BLS", "Medical Coding Certification"}
PATTERNS = {name: re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(a) for a in aliases) + r")(?![a-z0-9])", re.I)
            for name, aliases in CATALOG.items()}

# Cleo-specific classification tables used by both alerts and resume preparation.
CORE_DOMAIN = {
    "Patient scheduling", "Patient intake", "Check-in and check-out", "Insurance verification",
    "Referral verification", "Patient communication", "Provider communication",
    "Medical office operations", "Copayments", "Billing statements", "Vitals",
    "Specimen handling", "Resource coordination", "Onboarding", "Offboarding",
    "HR records", "Compliance documentation", "Records auditing", "Data entry",
    "Employee relations", "Benefits", "Employee wellness", "Expense management",
    "Travel coordination", "Customer service", "Payment processing",
    "Social media", "Email marketing", "Community outreach", "Crisis hotline support",
}
NAMED_PLATFORMS = {
    "Phreesia", "Workday", "Oracle", "ADP", "POS",
}
AUTOMATION_SKILLS = {"Records auditing", "Data entry", "Compliance documentation"}
_KNOCKOUT_NAMES = re.compile(
    r"^\d+\+ years experience$|^Bachelor's degree$|^Completed master's degree$|"
    r"^Certified Medical Assistant$|^CPR/BLS$|^Medical Coding Certification$"
)
DOMAIN_MISMATCH_PATTERN = re.compile(
    r"\bregistered nurse\b|\blicensed practical nurse\b|\blicensed vocational nurse\b|"
    r"\bnurse practitioner\b|\bphysician assistant\b|\bdental hygienist\b|"
    r"\bpharmacist\b|\blicensed (?:clinical )?(?:social worker|therapist)\b", re.I)
BELOW_LEVEL_PATTERN = re.compile(
    r"\bserver\b|\bcashier\b|\bretail sales associate\b", re.I)
SENIOR_TITLE_PATTERN = re.compile(
    r"\bsenior\b|\bsr\.?\b|\bmanager\b|\bsupervisor\b|\bdirector\b|\blead\b|\bhead of\b",
    re.I,
)
TARGET_ROLE_PATTERN = re.compile(
    r"\bpatient (?:access|services|care)\b|\bmedical (?:office|receptionist|records)\b|"
    r"\b(?:clinic|healthcare|health services|referral|scheduling|intake|care) coordinator\b|"
    r"\b(?:human resources|hr|people operations|benefits|recruiting|talent|onboarding) (?:assistant|coordinator)\b|"
    r"\b(?:administrative|office|operations|program|public health|community health|health education|outreach) coordinator\b|"
    r"\bexecutive assistant\b", re.I)
SIDEWAYS_ROLE_PATTERN = re.compile(
    r"\bserver\b|\bretail\b|\bcashier\b|\bsales associate\b", re.I)
EQUIVALENT_EXPERIENCE_PATTERN = re.compile(r"or equivalent experience", re.I)
PROGRAM_OWNER_PATTERN = re.compile(
    r"\bdepartment head\b|\bprogram owner\b|\bown(?:s|ing)? (?:the|our) (?:hr|clinic|health) program\b",
    re.I,
)
SOLO_HIRE_PATTERN = re.compile(
    r"\bsole hr (?:person|professional)\b|\bone[- ]person (?:office|hr) team\b", re.I)
_SENIORITY_BANDS = [(2, 95), (3, 78), (5, 55), (7, 33)]
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


def load_profile(path=PROFILE_PATH):
    with open(path, encoding="utf-8") as stream:
        profile = json.load(stream)
    if profile.get("schema_version") != 1 or not isinstance(profile.get("skills"), dict):
        raise ValueError("Invalid experience profile")
    return profile


def professional_years(profile, today=None):
    """Count non-internship paid-role months once, without double-counting overlaps."""
    today = today or date.today()
    months = set()
    for role in profile.get("employment", []):
        if role.get("type") not in {"employment", "full_time"}:
            continue
        start = date.fromisoformat(role["start"] + "-01")
        end = date.fromisoformat(role["end"] + "-01") if role.get("end") else today.replace(day=1)
        cursor = start.year * 12 + start.month
        stop = end.year * 12 + end.month
        months.update(range(cursor, stop))
    return len(months) / 12


def _sentences(text):
    return [s.strip() for s in re.split(r"\n+|(?<=[.!?;])\s+", text) if s.strip()]


def required_experience_years(text):
    """Return the largest explicit minimum, excluding preferred or upper-bound prose."""
    floors = []
    for sentence in _sentences(display.job_text(text)):
        lower = sentence.lower()
        if re.search(r"\b(?:preferred|desired|nice to have|bonus|a plus|up to)\b", lower):
            continue
        normalized = re.sub(
            r"\b(" + "|".join(_NUMBER_WORDS) + r")\b",
            lambda match: _NUMBER_WORDS[match.group(1).lower()],
            lower,
        )
        for match in re.finditer(
                r"\b(\d+)(?:\s*(?:[-–]|to)\s*\d+)?(?:\s*\+|\s+plus)?\s+"
                r"years?\s+(?:of\s+)?"
                r"(?:(?:relevant|professional|related|healthcare|medical|clinical|administrative|"
                r"human resources|hr|customer service|office)\s+){0,2}"
                r"experience\b", normalized, re.I):
            floors.append(int(match.group(1)))
    return max(floors) if floors else None


def _platform_tier(criterion):
    """Role match: full credit only for production/day-to-day evidence."""
    if criterion.get("kind") == "production":
        return 1.0
    return 0.5 if criterion["value"] > 0 else 0.0


def _ats_match(criteria):
    """Mechanical, binary supported/not-supported keyword coverage."""
    required = [c for c in criteria if c["required"] and not _KNOCKOUT_NAMES.match(c["name"])]
    preferred = [c for c in criteria if not c["required"]]
    if not required and not preferred:
        return None, dict(required_terms=0, supported_required=0, preferred_terms=0, supported_preferred=0)
    supported_required = sum(1 for c in required if c["value"] >= .5)
    supported_preferred = sum(1 for c in preferred if c["value"] >= .5)
    if required and preferred:
        score = supported_required / len(required) * 70 + supported_preferred / len(preferred) * 30
    elif required:
        score = supported_required / len(required) * 100
    else:
        score = supported_preferred / len(preferred) * 100
    return round(score), dict(required_terms=len(required), supported_required=supported_required,
                              preferred_terms=len(preferred), supported_preferred=supported_preferred)


def _transferability_score(profile):
    """Role match, adjacent transferability: breadth of demonstrated evidence
    across the whole catalog, as a proxy for learning this class of tool quickly."""
    skills = profile.get("skills", {})
    demonstrated = sum(1 for name in CATALOG if skills.get(name, {}).get("kind") in
                       ("production", "diagnosis", "design", "lab"))
    return round(15 * demonstrated / len(CATALOG)), demonstrated, len(CATALOG)


def _technical_match(criteria, text, title, profile):
    catalog_terms = [c for c in criteria if c["name"] in CATALOG]
    # Treat licensed-clinical language in the title as authoritative. Body text
    # is used only when the posting otherwise lacks a supported domain signal.
    domain_mismatch = bool(DOMAIN_MISMATCH_PATTERN.search(title or ""))
    if not domain_mismatch and not any(c["name"] in CORE_DOMAIN for c in catalog_terms):
        domain_mismatch = bool(DOMAIN_MISMATCH_PATTERN.search(text))
    if catalog_terms:
        core_terms = [c for c in catalog_terms if c["name"] in CORE_DOMAIN]
        core_score = round(40 * len(core_terms) / len(catalog_terms))
    else:
        core_score = 20  # No catalog terms at all: no domain signal either way.
    if domain_mismatch:
        core_score = min(core_score, 15)
    platform_terms = [c for c in catalog_terms if c["name"] in NAMED_PLATFORMS]
    platform_score = (round(30 * sum(_platform_tier(c) for c in platform_terms) / len(platform_terms))
                      if platform_terms else 0)
    transferability_score, demonstrated, catalog_size = _transferability_score(profile)
    automation_terms = [c for c in catalog_terms if c["name"] in AUTOMATION_SKILLS]
    automation_score = (round(15 * sum(_platform_tier(c) for c in automation_terms) / len(automation_terms))
                        if automation_terms else 15)  # Not a stated requirement: no penalty.
    total = max(0, min(100, core_score + platform_score + transferability_score + automation_score))
    inputs = dict(core_domain_alignment=core_score, named_platform_depth=platform_score,
                  adjacent_transferability=transferability_score, automation_scripting_iac=automation_score,
                  platforms_named=[c["name"] for c in platform_terms],
                  demonstrated_breadth=f"{demonstrated}/{catalog_size} cataloged skills with hands-on evidence")
    return total, inputs, domain_mismatch


def _seniority_fit(text, title, profile, today):
    years_required = required_experience_years(text)
    if years_required is not None:
        base = next((points for ceiling, points in _SENIORITY_BANDS if years_required <= ceiling), 10)
        basis = f"{years_required}+ years stated"
    elif SENIOR_TITLE_PATTERN.search(title or ""):
        base, basis = 10, "staff/principal/lead/director title, no explicit years stated"
    else:
        return None, dict(basis="no experience floor stated")
    observed = professional_years(profile, today)
    modifiers = []
    total = base
    production_core = sum(
        1 for name in CORE_DOMAIN
        if profile.get("skills", {}).get(name, {}).get("kind") == "production"
    )
    substitutes = bool(TARGET_ROLE_PATTERN.search(title or "")) and production_core >= 5
    if years_required is not None and years_required > observed and substitutes:
        total += 10
        modifiers.append("+10 directly relevant healthcare/HR operations breadth offsets part of the tenure gap")
    if EQUIVALENT_EXPERIENCE_PATTERN.search(text):
        total += 5
        modifiers.append("+5 posting allows equivalent experience")
    if PROGRAM_OWNER_PATTERN.search(text):
        total -= 10
        modifiers.append("-10 role owns a department or program rather than coordinating it")
    if SOLO_HIRE_PATTERN.search(text):
        total -= 15
        modifiers.append("-15 sole-owner role with no team support")
    return max(0, min(100, total)), dict(basis=basis, base=base, modifiers=modifiers,
                                         observed_years=round(observed, 1))


def _trajectory_value(title, text):
    if TARGET_ROLE_PATTERN.search(title or ""):
        return 90, "role title matches Cleo's healthcare, HR, administrative or public-health target tracks"
    if SIDEWAYS_ROLE_PATTERN.search(title or ""):
        return 25, "role reads as food service, retail or unrelated sales rather than the target path"
    if not title and TARGET_ROLE_PATTERN.search(text):
        return 90, "role text matches Cleo's target tracks"
    if not title and SIDEWAYS_ROLE_PATTERN.search(text):
        return 25, "role text reads as unrelated service, retail or sales work"
    return 55, "role track unclear from title/description; neutral default"


def _weighted_overall(technical, seniority, ats, trajectory):
    parts = [(technical, .45), (seniority, .30), (ats, .15), (trajectory, .10)]
    valid = [(v, w) for v, w in parts if v is not None]
    return sum(v * w for v, w in valid) / sum(w for _, w in valid) if valid else None


def eligibility(job, profile):
    text = display.job_text(job.description)
    results = []
    p = profile.get("preferences", {})
    certs = profile.get("certifications", {})
    for sentence in _sentences(text):
        lower = sentence.lower()
        preferred = bool(re.search(r"preferred|desired|a plus|nice to have", lower))
        required = bool(re.search(r"required|must|active|current|valid|possess|hold", lower))
        if DOMAIN_MISMATCH_PATTERN.search(sentence) and required and not preferred:
            results.append("BLOCKER: posting requires a clinical license not documented in the supplied resume")
        for name, pattern in (
            ("Certified Medical Assistant", r"certified medical assistant|cma certification|\bcma\b"),
            ("CPR/BLS", r"cpr certification|bls certification|basic life support"),
            ("Medical Coding Certification", r"cpc certification|certified professional coder"),
        ):
            if re.search(pattern, lower):
                earned = certs.get(name, {}).get("status") == "earned"
                if required and not preferred and not earned:
                    results.append(f"BLOCKER: {name} is required but not documented")
                elif not earned:
                    results.append(f"{name} mentioned; not documented")
        if re.search(r"valid driver'?s license", lower):
            results.append("Driver's license requirement needs confirmation")
        if re.search(r"legally authorized|authorized to work|work authorization", lower):
            results.append("U.S. work authorization needs confirmation")
    if display.work_mode(job) == "Remote":
        loc = (job.location or "").lower()
        if re.search(r"\b(?:uk|united kingdom|canada|australia|germany|india)\b", loc) and not re.search(r"\b(?:us|usa|united states|worldwide|anywhere)\b", loc):
            results.append("Remote country restriction: verify work eligibility")
    return list(dict.fromkeys(results))


def location_preference(job, profile):
    mode = display.work_mode(job)
    if mode == "Remote":
        return 1.0, "Remote preferred; check any geographic restrictions"
    if mode == "Not specified":
        return None, "NEEDS EVIDENCE: work arrangement is unknown"
    loc = (job.location or "").lower()
    if not loc:
        return None, "NEEDS EVIDENCE: onsite/hybrid location is unknown"
    preferred = profile.get("preferences", {}).get("preferred_metros", [])
    if any(re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", loc) for alias in preferred):
        return .85, "Preferred onsite/hybrid metro"
    return 0.0, "BLOCKER: onsite/hybrid location is outside preferred metros"


def salary_preference(job, profile):
    bounds = display.annual_usd_salary(job.salary)
    minimum = profile.get("preferences", {}).get("minimum_salary_usd", 0)
    if not minimum:
        return None, "No salary floor configured; compensation shown for review"
    if bounds is None:
        return None, "Salary unknown or not explicitly annual USD"
    low, high = bounds
    if high <= minimum:
        return 0.0, "BLOCKER: salary does not exceed $" + format(minimum, ",")
    if low > minimum:
        return 1.0, "Salary meets target"
    return .5, "Range overlaps target; confirm an offer above $" + format(minimum, ",")


def score_job(job, profile, today=None):
    text = display.job_text(job.description)
    # Never turn an email's 'your skills fit' marketing into candidate/job evidence.
    criteria = []
    for name, pattern in PATTERNS.items():
        matches = [sentence for sentence in _sentences(text) if pattern.search(sentence)]
        if not matches:
            continue
        preferred = all(re.search(r"preferred|desired|nice to have|a plus|bonus", s, re.I) for s in matches)
        record = profile.get("skills", {}).get(name, {})
        strength = STRENGTH.get(record.get("kind"), 0)
        if name in CERTS:
            credential = profile.get("certifications", {}).get(name, {})
            earned = credential.get("status") == "earned"
            expiry = credential.get("expires")
            strength = 1.0 if earned and (not expiry or date.fromisoformat(expiry) >= (today or date.today())) else 0.0
            record = dict(record, evidence=credential.get("evidence", []))
        criteria.append(dict(name=name, weight=.5 if preferred else 1.0, value=strength,
                             required=not preferred, evidence=record.get("evidence", []),
                             kind=record.get("kind"), excerpt=display.clip(matches[0], 180)))
    required = required_experience_years(text)
    if required is not None:
        observed = professional_years(profile, today)
        criteria.append(dict(name=f"{required}+ years experience", weight=1.5,
                             value=min(observed / max(required, 1), 1), required=True,
                             evidence=["employment"], kind=None,
                             excerpt=f"{observed:.1f} full-time years; internships retained separately"))
    if re.search(r"\bbachelor['’]?s?\b", text, re.I):
        criteria.append(dict(name="Bachelor's degree", weight=1, value=1 if profile.get("bachelors_completed") else 0,
                             required=True, evidence=["education"], kind=None, excerpt="Completed BS in Public Health"))
    if re.search(r"\bmaster['’]?s?\s+(?:degree|required)", text, re.I) and not re.search(r"master['’]?s?\s+(?:degree\s+)?(?:preferred|desired)", text, re.I):
        criteria.append(dict(name="Completed master's degree", weight=1, value=1 if profile.get("masters_completed") else 0,
                             required=True, evidence=["education"], kind=None, excerpt="In progress is not a completed degree"))
    technical_weight = sum(c["weight"] for c in criteria)
    coverage = sum(c["weight"] * c["value"] for c in criteria) / technical_weight if technical_weight else None
    location_value, location_note = location_preference(job, profile)
    salary_value, salary_note = salary_preference(job, profile)
    # Requirements dominate; preference dimensions only count when observable.
    # The raw weighted score is never allowed to hide a known disqualifier or
    # inflate a thin/ambiguous posting to 100%.
    parts = [(coverage, .8), (location_value, .1), (salary_value, .1)]
    valid = [(v, w) for v, w in parts if v is not None]
    raw_score = round(100 * sum(v*w for v,w in valid) / sum(w for _,w in valid)) if coverage is not None else None
    eligibility_notes = eligibility(job, profile)
    preference_notes = [location_note, salary_note]
    blockers = [note for note in eligibility_notes + preference_notes if "BLOCKER:" in note]
    evidence_gaps = [note for note in preference_notes if note.startswith("NEEDS EVIDENCE:")]
    required_gaps = [c["name"] for c in criteria if c.get("required") and c["value"] == 0]
    severe_shortfalls = [c["name"] for c in criteria if c.get("required") and 0 < c["value"] < .5]
    material_shortfalls = [c["name"] for c in criteria if c.get("required") and .5 <= c["value"] < .75]

    # Dashboard: ATS / Role Match / Seniority / Overall Match. Computed
    # whenever there's evidence to work from, even on a blocked posting, so the
    # inputs stay visible per 15.1 ("show the inputs, not just the number").
    ats_match = technical_match = seniority_fit = overall_match = None
    weighted = None
    match_inputs = {}
    if coverage is not None:
        combined_text = f"{job.title or ''}\n{text}"
        ats_match, ats_inputs = _ats_match(criteria)
        technical_match, technical_inputs, domain_mismatch = _technical_match(
            criteria, combined_text, job.title, profile)
        seniority_fit, seniority_inputs = _seniority_fit(combined_text, job.title, profile, today)
        trajectory_value, trajectory_note = _trajectory_value(job.title, combined_text)
        weighted = _weighted_overall(technical_match, seniority_fit, ats_match, trajectory_value)
        below_current_level = bool(BELOW_LEVEL_PATTERN.search(job.title or ""))
        match_inputs = dict(ats=ats_inputs, technical=technical_inputs, seniority=seniority_inputs,
                            trajectory=dict(value=trajectory_value, note=trajectory_note),
                            domain_mismatch=domain_mismatch, below_current_level=below_current_level)

    cap_reasons = []
    score = raw_score
    decision = "eligible"
    if blockers:
        # Licensure/certification and onsite/hybrid-location blockers stay absolute.
        score = 0
        overall_match = 0
        decision = "disqualified"
        cap_reasons.append("Known eligibility or location blocker")
    elif score is not None:
        caps = []
        if required_gaps or severe_shortfalls:
            caps.append((35, "Hard disqualifier present: a required minimum is unmet"))
        elif material_shortfalls:
            caps.append((59, "Required minimum is only partially met"))
        if match_inputs.get("domain_mismatch"):
            caps.append((40, "Licensed-clinical domain mismatch"))
        if seniority_fit is not None and seniority_fit < 30:
            caps.append((55, "Seniority Fit below 30"))
        if match_inputs.get("below_current_level"):
            caps.append((50, "Role is outside the intended career track"))
        if evidence_gaps:
            caps.append((69, "Location/work-arrangement evidence is missing"))
        if len(criteria) < 3:
            caps.append((69, "Too few requirements were available to support a high-confidence score"))
        elif len(criteria) < 5 or job.source == "indeed_email":
            caps.append((79, "Posting detail is limited"))
        overall_match = round(weighted) if weighted is not None else raw_score
        if caps:
            cap, reason = min(caps, key=lambda item: item[0])
            if overall_match > cap:
                overall_match = cap
            cap_reasons.extend(reason for value, reason in caps if value == cap)
        score = overall_match
        if evidence_gaps:
            decision = "needs_evidence"
        elif score < 70:
            decision = "not_eligible"

    return dict(score=score, raw_score=raw_score,
                requirement_score=round(coverage*100) if coverage is not None else None,
                confidence="limited" if len(criteria) < 5 or job.source == "indeed_email" else "moderate",
                decision=decision, score_cap_reasons=list(dict.fromkeys(cap_reasons)),
                evidence_gaps=evidence_gaps,
                matched=[c["name"] for c in criteria if c["value"] >= .85],
                partial=[c["name"] for c in criteria if 0 < c["value"] < .85],
                gaps=[c["name"] for c in criteria if c["value"] == 0],
                criteria=criteria, eligibility=eligibility_notes,
                preferences=preference_notes, profile_version=profile.get("updated", "unknown"),
                ats_match=ats_match, technical_match=technical_match, seniority_fit=seniority_fit,
                overall_match=overall_match, match_inputs=match_inputs)


def _job_value(job, name, default=""):
    return job.get(name, default) if isinstance(job, dict) else getattr(job, name, default)


def _online_salary(text):
    """Prefer compensation-labelled annual USD ranges over unrelated dollar figures."""
    sentences = _sentences(text)
    labelled = [
        sentence for sentence in sentences
        if re.search(r"\b(?:salary|compensation|base pay|pay range)\b", sentence, re.I)
    ]
    for sentence in labelled + sentences:
        if display.annual_usd_salary(sentence) is not None:
            return display.clip(sentence, 500)
    return ""


def _online_location_evidence(text):
    evidence = []
    for sentence in _sentences(text):
        if re.search(
                r"\b(?:based in|located in|must (?:reside|live|be based)|"
                r"work location|(?:candidate|employee|applicant).{0,50}(?:reside|live|be based)\s+in|"
                r"hybrid(?: role|position)?\s+(?:in|near)|"
                r"on[ -]?site(?: role|position)?\s+(?:in|near))\b|\blocation\s*:",
                sentence, re.I):
            evidence.append(display.clip(sentence, 350))
    return " ".join(evidence[:5])


def rescore_after_online_enrichment(job, online_text, profile=None, today=None):
    """Re-score a verified fuller posting before any application-side action."""
    profile = profile or load_profile()
    saved_description = str(_job_value(job, "description", "") or "")
    online_text = str(online_text or "")
    probe = SimpleNamespace(
        title=_job_value(job, "title", ""),
        description=online_text,
        location="",
        work_mode="",
        work_mode_source="",
        remote=False,
    )
    online_mode = display.work_mode(probe)
    location_evidence = _online_location_evidence(online_text)
    preferred = profile.get("preferences", {}).get("preferred_metros", [])
    preferred_lines = [
        sentence for sentence in _sentences(online_text)
        if any(re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])",
                         sentence, re.I) for alias in preferred)
    ]
    if preferred_lines:
        location_evidence = " ".join(
            list(dict.fromkeys(
                [location_evidence] + [display.clip(line, 350) for line in preferred_lines]
            ))
        ).strip()
    saved_mode = str(_job_value(job, "work_mode", "") or "")
    saved_mode_source = str(_job_value(job, "work_mode_source", "") or "")
    if online_mode != "Not specified":
        work_mode = online_mode
        work_mode_source = "Verified public posting text"
    else:
        work_mode = saved_mode
        work_mode_source = saved_mode_source

    saved_location = str(_job_value(job, "location", "") or "")
    if online_mode in {"Hybrid", "Onsite"} and location_evidence:
        location = location_evidence
    else:
        location = saved_location or location_evidence
    online_salary = _online_salary(online_text)
    salary = online_salary or str(_job_value(job, "salary", "") or "")

    candidate = SimpleNamespace(
        source=_job_value(job, "source", ""),
        title=_job_value(job, "title", ""),
        company=_job_value(job, "company", ""),
        url=_job_value(job, "url", ""),
        description=(saved_description + "\n" + online_text).strip(),
        location=location,
        work_mode=work_mode,
        work_mode_source=work_mode_source,
        remote=work_mode == "Remote",
        salary=salary,
    )
    result = score_job(candidate, profile, today=today)
    notes = result.get("eligibility", []) + result.get("preferences", [])
    blockers = [note for note in notes if "BLOCKER:" in note]
    score = result.get("score")
    blocked = (
        not isinstance(score, (int, float)) or isinstance(score, bool) or score < 70
        or bool(blockers)
    )
    reasons = blockers or result.get("score_cap_reasons", []) or result.get("evidence_gaps", [])
    if blocked and not reasons:
        reasons = ["Enriched score is below the 70% application threshold"]
    before_fit = _job_value(job, "fit", {}) or {}
    result["post_enrichment"] = {
        "status": "blocked" if blocked else "eligible",
        "before_score": before_fit.get("score") if isinstance(before_fit, dict) else None,
        "after_score": score,
        "reasons": list(dict.fromkeys(reasons)),
        "signals": {
            "work_mode": work_mode or "Not specified",
            "work_mode_source": work_mode_source,
            "location_evidence": location,
            "salary_evidence": salary,
            "required_experience_years": required_experience_years(candidate.description),
        },
    }
    return result


def fit_fields(result):
    if result["score"] is None:
        headline = "Pending — insufficient job requirements"
    elif result.get("decision") == "disqualified":
        headline = f'Disqualified · {result["score"]}% fit · hard blocker'
    elif result.get("decision") == "needs_evidence":
        headline = f'{result["score"]}% fit maximum · more evidence required'
    else:
        headline = f'{result["score"]}% fit · {result["confidence"]} detail (assessed criteria)'
    fields = [{"name": "Experience fit", "value": headline, "inline": False}]
    if result.get("overall_match") is not None:
        seniority = result["seniority_fit"]
        dashboard = (f"Overall {result['overall_match']}/100 · ATS {result['ats_match']}/100 · "
                    f"Role Match {result['technical_match']}/100 · Seniority Fit "
                    f"{seniority if seniority is not None else 'n/a'}/100")
        fields.append({"name": "Match score", "value": display.clip(dashboard, 240), "inline": False})
    enrichment = result.get("post_enrichment")
    if enrichment:
        blocked = enrichment.get("status") == "blocked"
        label = "BLOCKED AFTER ENRICHMENT — DO NOT APPLY" if blocked else "Re-scored after online enrichment"
        reasons = "; ".join(enrichment.get("reasons", []))
        movement = f"{enrichment.get('before_score', 'n/a')} → {enrichment.get('after_score', 'n/a')}"
        fields.append({
            "name": label,
            "value": display.clip(f"Score {movement}" + (f" · {reasons}" if reasons else ""), 500),
            "inline": False,
        })
    for name, key in [("Matched", "matched"), ("Partial evidence", "partial"), ("Not documented / unmet", "gaps")]:
        if result[key]:
            fields.append({"name": name, "value": display.clip(", ".join(result[key]), 230), "inline": False})
    details = result["eligibility"] + result["preferences"]
    if result.get("score_cap_reasons"):
        details.append("Score cap: " + "; ".join(result["score_cap_reasons"]))
    fields.append({"name": "Eligibility & preferences", "value": display.clip("; ".join(details), 500), "inline": False})
    return fields
