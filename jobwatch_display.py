"""Conservative, source-grounded job labels and Discord summaries (stdlib only)."""
import html
import re


def clean_text(value):
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def clip(value, limit):
    text = clean_text(value)
    if len(text) <= limit:
        return text
    prefix = text[:limit - 1]
    return (prefix.rsplit(" ", 1)[0] if " " in prefix else prefix).rstrip(" ,;:") + "…"


def is_remote(text):
    """Require a work arrangement, never remote access or distributed systems."""
    text = clean_text(text)
    if re.search(r"\b(?:not|non)[ -]remote\b|no remote|remote (?:work |working )?(?:is )?not|not (?:a )?remote", text, re.I):
        return False
    return bool(re.search(
        r"^(?:fully |100% )?remote(?:$|\s*[-–—,(|:]\s*|\s+(?:US|USA|United States|worldwide)\b)"
        r"|\b(?:fully remote|100% remote|work(?:ing)? (?:from home|remotely)|wfh)\b"
        r"|\b(?:role|position|job)\s+(?:is|can be)\s+(?:fully )?remote\b"
        r"|\bremote\s+(?:role|position|job|work|working)\b"
        r"|[\[(|,–—-]\s*remote\s*(?:[\])|]|$)", text, re.I))


def work_mode(job):
    """Explicit metadata wins; conflicting descriptions never imply fully remote."""
    # Only provenance-backed values outrank description inference.
    if getattr(job, "work_mode_source", "") and job.work_mode in ("Remote", "Hybrid", "Onsite"):
        return job.work_mode
    metadata = clean_text(f"{job.work_mode} {job.location}")
    description = clean_text(job.description)
    for text in (metadata, description):
        if re.search(r"\bhybrid\b", text, re.I) and (text == metadata or re.search(r"hybrid (?:work|role|position|schedule)|(?:role|position) is hybrid", text, re.I)):
            return "Hybrid"
        if re.search(r"\bon[ -]?site\b|\bin[ -](?:office|person)\b", text, re.I) and (text == metadata or re.search(r"(?:role|position|work) (?:is |requires? )?(?:on[ -]?site|in[ -]person)|must (?:work|be) on[ -]?site|(?:candidate|employee|applicant).{0,100}(?:reside|live|be based).{0,100}\bon[ -]?site work\b|\bon[ -]?site work (?:is )?required\b|\d+ days? (?:a week )?in (?:the )?office", text, re.I)):
            return "Onsite"
        if re.search(r"\b(?:not|non)[ -]remote\b|no remote|remote (?:work |working )?(?:is )?not|not (?:a )?remote", text, re.I):
            return "Onsite"
    if is_remote(job.work_mode) or is_remote(job.location) or is_remote(job.title):
        return "Remote"
    if job.remote or is_remote(description):
        return "Remote"
    return "Not specified"


_YEARS = re.compile(r"\b\d+(?:\s*(?:[-–]|to)\s*\d+)?(?:\s*\+|\s+plus)?\s+years?\s+(?:of\s+)?(?:(?:relevant|professional|related)\s+)?experience\b", re.I)
_LEVEL = re.compile(r"\b(?:Entry-level|Junior|Mid-level|Intermediate|Senior-level|Senior|Executive-level|Director|Lead|Principal|Staff)\b", re.I)


def experience(title, description="", explicit=""):
    if clean_text(explicit):
        return clean_text(explicit)
    match = _YEARS.search(clean_text(description))
    if match:
        return match.group(0)
    match = _LEVEL.search(title or "")
    return match.group(0) if match else ""


def compensation(value):
    """Format a simple USD amount/range; preserve all other currencies and qualifiers."""
    text = clean_text(value)
    if not text:
        return "Not listed"
    match = re.fullmatch(r"USD\s*\$?([\d,]+(?:\.\d+)?)\s*(?:[-–]\s*\$?([\d,]+(?:\.\d+)?))?(.*)", text, re.I)
    if not match:
        return text
    def amount(number):
        whole, dot, fraction = number.replace(",", "").partition(".")
        return "$" + format(int(whole), ",") + (dot + fraction if fraction and int(fraction) else "")
    result = amount(match[1])
    if match[2]:
        result += "–" + amount(match[2])
    return result + " USD" + match[3]


def role_summary(description):
    """Extract at most two relevant passages, without generating qualifications."""
    text = re.sub(r"<\s*(?:br\s*/?|/?(?:p|li|h[1-6]|div))\s*>", "\n", job_text(description), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    # Section boundaries also work for adapters that flattened the source HTML.
    text = re.sub(r"\b(Responsibilities|Requirements|Qualifications|What you(?:'|’)ll do|What you bring|Job Overview)\s*:?", r"\n\1: ", text, flags=re.I)
    passages = [clean_text(s).strip("•-* ") for s in re.split(r"\n+|(?<=[.!?])\s+", text)]
    selected = []
    section = False
    for passage in passages:
        if re.match(r"^(?:Benefits|About us|About the company|Compensation|Equal opportunity)\b", passage, re.I):
            section = False
        if re.match(r"^(?:Responsibilities|Requirements|Qualifications|What you(?:'|’)ll do|What you bring):", passage, re.I):
            section = True
        if section or re.search(r"\b(?:responsibilities|requirements|qualifications|you will|you'll|you’ll|you bring|must have|required|experience (?:with|in)|years? (?:of )?experience|develop|investigate|monitor|triage|implement|maintain|design|analy[sz]e|respond to)\b", passage, re.I):
            if len(passage) > 24 and not re.search(r"\b(?:our company|founded in|we are a|we're a|we’re a|is a (?:leading|leader)|equal opportunity)\b", passage, re.I):
                if passage not in selected:
                    selected.append(passage)
        if len(selected) == 2:
            break
    return "\n".join("• " + clip(p, 230) for p in selected) or "Open the posting for responsibilities and requirements."


def remote_details(description):
    text = clean_text(description)
    for passage in re.split(r"(?<=[.!?;])\s+", text):
        if re.search(r"\b(?:must (?:reside|live|be based)|(?:US|U\.S\.|United States)[ -]only|time ?zones?|remote (?:within|from|in))\b", passage, re.I):
            return clip(passage, 200)
    return ""


def job_text(value):
    """Remove personalized email pitches and boilerplate before summary or matching."""
    text = html.unescape(str(value or "")).replace("U.S.", "US")
    text = re.sub(r"<(?:br|/p|/li|/div|/h[1-6])\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    if re.search(r"your (?:background|skills|experience|profile)|could be a (?:strong|great|good) (?:match|fit)", text, re.I) and not re.search(r"responsibilities|qualifications|requirements|what you.ll do", text, re.I):
        return ""
    parts = re.split(r"\n+|(?<=[.!?])\s+", text)
    return "\n".join(clean_text(p) for p in parts if clean_text(p) and not re.search(
        r"\bhi\s+\w+|your (?:background|skills|experience|profile)|"
        r"could be a (?:strong|great|good) (?:match|fit)|"
        r"unsubscribe|manage (?:your )?(?:email|job) alerts|"
        r"^(?:founded in|about (?:us|the company)|we are a|we're a)|"
        r"\bis a (?:leader|leading provider)\b", p, re.I))


def extract_salary(value):
    """Preserve whole amounts across HTML whitespace and Unicode range separators."""
    text = clean_text(value)
    text = re.sub(r"(?<=\d),\s+(?=\d{3}\b)", ",", text)
    number = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?[kK]?"
    match = re.search(r"(?:(?:USD|US)\s*)?\$\s*" + number +
                      r"(?:\s*[-–—]\s*\$?\s*" + number + r")?"
                      r"(?:\s*(?:a year|per year|annually|/\s*(?:year|yr|hour|hr)|an hour|per hour))?", text, re.I)
    if not match:
        return ""
    # Reject fragments such as '$76,' rather than silently treating them as $76.
    if match.end() < len(text) and text[match.end()] in ",0123456789":
        return ""
    return re.sub(r"\$\s+", "$", match.group(0))


def annual_usd_salary(value):
    text = clean_text(value)
    if not re.search(r"a year|per year|annually|/\s*(?:year|yr)\b", text, re.I):
        return None
    if re.search(r"CAD|AUD|NZD|HKD|SGD|EUR|GBP|CA\$|AU\$", text, re.I):
        return None
    if "$" not in text and "USD" not in text.upper():
        return None
    numbers = re.findall(r"\d[\d,]*(?:\.\d+)?[kK]?", text)
    if not numbers:
        return None
    def number(raw):
        return float(raw.rstrip("kK").replace(",", "")) * (1000 if raw[-1:] in ("k", "K") else 1)
    values = [number(n) for n in numbers[:2]]
    return min(values), max(values)

