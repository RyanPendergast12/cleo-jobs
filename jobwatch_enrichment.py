"""Bounded public-posting retrieval shared by the scanner and apply worker."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import requests

MAX_ONLINE_POSTING_BYTES = 512 * 1024
MAX_ONLINE_POSTING_CHARS = 24000
MAX_ONLINE_REDIRECTS = 4


class _PostingTextParser(HTMLParser):
    """Extract visible page text while excluding executable and decorative data."""

    BLOCK_TAGS = {
        "article", "br", "dd", "div", "dl", "dt", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "li", "main", "p", "section", "table", "td",
        "th", "tr", "ul",
    }
    SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


class _PostingLinkParser(HTMLParser):
    """Collect result links from a bounded public search response."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href", "")
        if href:
            self.links.append(href)


def _is_public_http_url(url: str) -> bool:
    """Reject local, credentialed, and non-HTTP targets before any request."""

    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM
        )
        if not addresses:
            return False
        return all(ipaddress.ip_address(item[4][0]).is_global
                   for item in addresses)
    except (OSError, TypeError, ValueError):
        return False


def _visible_posting_text(raw: str, content_type: str) -> str:
    if "html" not in content_type.lower() and "<html" not in raw[:1000].lower():
        return re.sub(r"\s+", " ", raw).strip()[:MAX_ONLINE_POSTING_CHARS]
    parser = _PostingTextParser()
    try:
        parser.feed(raw)
        parser.close()
    except (ValueError, TypeError):
        return ""
    lines = []
    for line in "".join(parser.parts).splitlines():
        clean = re.sub(r"[ \t\f\v]+", " ", line).strip()
        if clean and (not lines or clean != lines[-1]):
            lines.append(clean)
    return "\n".join(lines)[:MAX_ONLINE_POSTING_CHARS]


def fetch_online_posting(url: str) -> dict:
    """Retrieve bounded visible text from a public job URL.

    Redirect targets are validated individually to prevent the worker from being
    used to access private network resources. Errors are intentionally reduced
    to a stable status so credentials and network details never reach Discord.
    """

    source_url = str(url or "").strip()
    if not _is_public_http_url(source_url):
        return {"status": "unavailable", "source_url": source_url}

    current = source_url
    for _ in range(MAX_ONLINE_REDIRECTS + 1):
        if not _is_public_http_url(current):
            return {"status": "unavailable", "source_url": source_url}
        response = None
        try:
            response = requests.get(
                current,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; JobWatch/1.0; "
                        "+https://github.com/RyanPendergast12/cleo-jobs)"
                    ),
                    "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                },
                timeout=(5, 15),
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location", "")
                current = urljoin(current, location)
                if not location:
                    return {"status": "unavailable", "source_url": source_url}
                continue
            if response.status_code != 200:
                return {"status": "unavailable", "source_url": source_url}

            content_type = response.headers.get("Content-Type", "")
            if content_type and not any(
                    kind in content_type.lower() for kind in ("html", "text")):
                return {"status": "unavailable", "source_url": source_url}

            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                remaining = MAX_ONLINE_POSTING_BYTES - len(body)
                if remaining <= 0:
                    break
                body.extend(chunk[:remaining])
            encoding = response.encoding or "utf-8"
            raw = bytes(body).decode(encoding, errors="replace")
            text = _visible_posting_text(raw, content_type)
            if len(text) < 200:
                return {
                    "status": "insufficient",
                    "source_url": source_url,
                    "final_url": current,
                }
            return {
                "status": "retrieved",
                "source_url": source_url,
                "final_url": current,
                "text": text,
                "retrieved_chars": len(text),
            }
        except (requests.RequestException, OSError, UnicodeError, ValueError):
            return {"status": "unavailable", "source_url": source_url}
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
    return {"status": "unavailable", "source_url": source_url}


def _linkedin_job_id(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if not parsed.hostname or not parsed.hostname.lower().endswith("linkedin.com"):
        return ""
    match = re.search(
        r"/jobs/view/(?:[^/?#]*-)?(\d{6,})(?:[/?#]|$)",
        parsed.path + "/",
        flags=re.I,
    )
    return match.group(1) if match else ""


def _search_result_links(raw: str) -> list[str]:
    parser = _PostingLinkParser()
    try:
        parser.feed(raw)
        parser.close()
    except (TypeError, ValueError):
        return []
    links = []
    for href in parser.links:
        absolute = urljoin("https://html.duckduckgo.com/", href)
        parsed = urlparse(absolute)
        if parsed.hostname and parsed.hostname.lower().endswith("duckduckgo.com"):
            target = parse_qs(parsed.query).get("uddg", [""])[0]
        else:
            target = absolute
        target_host = (urlparse(target).hostname or "").lower()
        if (not target.startswith(("http://", "https://")) or
                target_host.endswith(("duckduckgo.com", "google.com", "bing.com"))):
            continue
        if target not in links:
            links.append(target)
    return links[:5]


def _posting_match_score(job: dict, result: dict) -> float:
    if result.get("status") != "retrieved":
        return 0
    text = str(result.get("text", "")).lower()
    final_url = str(result.get("final_url", "")).lower()
    job_id = _linkedin_job_id(job.get("url", ""))
    if job_id and (job_id in text or job_id in final_url):
        return 20

    def tokens(value):
        ignored = {
            "and", "for", "the", "with", "job", "role", "inc", "llc",
            "corporation", "company",
        }
        return {
            token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(token) >= 3 and token not in ignored
        }

    title_tokens = tokens(job.get("title"))
    company_tokens = tokens(job.get("company"))
    title_hits = sum(token in text for token in title_tokens)
    company_hits = sum(token in text for token in company_tokens)
    if title_tokens and title_hits < max(1, (len(title_tokens) + 1) // 2):
        return 0
    if company_tokens and not company_hits:
        return 0
    if not title_tokens and not company_tokens:
        return 1
    return (
        (title_hits / max(1, len(title_tokens))) * 6 +
        (company_hits / max(1, len(company_tokens))) * 4 +
        min(len(text) / 8000, 2)
    )


def _fetch_search_links(job: dict) -> list[str]:
    title = str(job.get("title", "")).strip()
    company = str(job.get("company", "")).strip()
    if not title or not company:
        return []
    job_id = _linkedin_job_id(job.get("url", ""))
    query = " ".join(
        value for value in (f'"{title}"', f'"{company}"',
                            f'"{job_id}"' if job_id else "") if value
    )
    search_url = (
        "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
    )
    if not _is_public_http_url(search_url):
        return []
    response = None
    try:
        response = requests.get(
            search_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; JobWatch/1.0; "
                    "+https://github.com/RyanPendergast12/cleo-jobs)"
                ),
                "Accept": "text/html",
            },
            timeout=(5, 12),
            allow_redirects=False,
            stream=True,
        )
        if response.status_code != 200:
            return []
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            remaining = MAX_ONLINE_POSTING_BYTES - len(body)
            if remaining <= 0:
                break
            body.extend(chunk[:remaining])
        raw = bytes(body).decode(response.encoding or "utf-8", errors="replace")
        return _search_result_links(raw)
    except (requests.RequestException, OSError, UnicodeError, ValueError):
        return []
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if callable(close):
                close()


def _search_posting_candidates(job: dict) -> dict:
    best = {"status": "unavailable", "source_url": str(job.get("url", "") or "")}
    best_score = 0
    for link in _fetch_search_links(job):
        candidate_url = link
        linked_in_id = _linkedin_job_id(link)
        if linked_in_id:
            candidate_url = (
                "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/" +
                linked_in_id
            )
        candidate = fetch_online_posting(candidate_url)
        score = _posting_match_score(job, candidate)
        if score > best_score:
            best, best_score = candidate, score
            best["matched_url"] = link
    if best_score:
        best["method"] = "web_search"
        best["requested_url"] = str(job.get("url", "") or "")
    return best


def enrich_job_posting(job: dict) -> dict:
    """Add the best verified public copy without replacing saved job data."""

    enriched = dict(job)
    url = str(job.get("url", "") or "").strip()
    existing = job.get("online_posting")
    if isinstance(existing, dict) and _posting_match_score(job, existing):
        return enriched
    enabled = os.environ.get(
        "JOBWATCH_ONLINE_ENRICHMENT_ENABLED",
        os.environ.get("JOB_APPLY_ONLINE_ENRICHMENT_ENABLED", "true"),
    )
    if enabled.lower() in {
            "0", "false", "no", "off"}:
        enriched["online_posting"] = {
            "status": "disabled",
            "source_url": url,
        }
        return enriched
    try:
        candidates = []
        linked_in_id = _linkedin_job_id(url)
        if linked_in_id:
            guest_url = (
                "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/" +
                linked_in_id
            )
            guest = fetch_online_posting(guest_url)
            guest["method"] = "linkedin_guest"
            guest["requested_url"] = url
            candidates.append(guest)

        original = fetch_online_posting(url)
        original["method"] = "original_url"
        original["requested_url"] = url
        candidates.append(original)

        best = max(candidates, key=lambda item: _posting_match_score(job, item))
        if _posting_match_score(job, best):
            enriched["online_posting"] = best
            return enriched

        search_enabled = os.environ.get(
            "JOBWATCH_SEARCH_FALLBACK_ENABLED",
            os.environ.get("JOB_APPLY_SEARCH_FALLBACK_ENABLED", "true"),
        )
        if search_enabled.lower() not in {
                    "0", "false", "no", "off"}:
            searched = _search_posting_candidates(job)
            if _posting_match_score(job, searched):
                enriched["online_posting"] = searched
                return enriched

        enriched["online_posting"] = {
            "status": "unavailable",
            "source_url": url,
            "method": "fallback_exhausted",
        }
    except Exception:
        # Online enrichment is a precaution, never a reason to lose a valid
        # captured posting or prevent the owner from requesting a draft.
        enriched["online_posting"] = {
            "status": "unavailable",
            "source_url": url,
            "method": "fallback_exhausted",
        }
    return enriched

