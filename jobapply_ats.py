"""Central, fail-closed registry for employer application destinations.

Source boards and direct ATS forms are deliberately different.  A source URL
may be inspected only to discover the employer's direct application URL; it is
never eligible for transmission.  New ATS families can be recognized for
read-only inspection without silently becoming transmission-capable.
"""
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Destination:
    platform: str
    kind: str  # ``direct`` or ``source``
    host: str


DIRECT_EXACT_HOSTS = {
    'us-3.fountain.com': 'fountain',
    'careers.subway.com': 'subway',
    'job-boards.greenhouse.io': 'greenhouse',
    'boards.greenhouse.io': 'greenhouse',
    'jobs.ashbyhq.com': 'ashby',
    'jobs.lever.co': 'lever',
    'jobs.smartrecruiters.com': 'smartrecruiters',
}

# Tenant ATS hosts must end on a dot boundary.  ``evilmyworkdayjobs.com`` and
# ``tenant.myworkdayjobs.com.evil.test`` therefore never match.
DIRECT_SUFFIX_HOSTS = (
    ('.fountain.com', 'fountain'),
    ('.myworkdayjobs.com', 'workday'),
    ('.myworkdaysite.com', 'workday'),
    ('.icims.com', 'icims'),
    ('.bamboohr.com', 'bamboohr'),
)

SOURCE_EXACT_HOSTS = {
    'linkedin.com': 'linkedin',
    'www.linkedin.com': 'linkedin',
}

SOURCE_SUFFIX_HOSTS = (
    ('.linkedin.com', 'linkedin'),
)

# Only adapters that have already passed their platform-specific dry-run suite
# may ever be named in JOB_APPLY_TRANSMIT_ATS.  Recognition alone is not trust.
TRANSMIT_READY = frozenset({'fountain', 'subway', 'greenhouse', 'ashby'})

# Additional origins a page may use for scripts/assets/uploads.  The browser
# also permits the exact page host and tightly scoped source-to-ATS redirects.
REQUESTABLE = {
    'fountain': ('.fountain.com',),
    'subway': ('.fountain.com',),
    'greenhouse': ('.greenhouse.io', '.s3.amazonaws.com'),
    'ashby': ('.ashbyhq.com',),
    'lever': ('.lever.co',),
    'smartrecruiters': ('.smartrecruiters.com',),
    'workday': ('.myworkdayjobs.com', '.myworkdaysite.com'),
    'icims': ('.icims.com',),
    'bamboohr': ('.bamboohr.com',),
    'linkedin': ('.linkedin.com', '.licdn.com'),
}


def _platform_for_host(host, exact, suffixes):
    if host in exact:
        return exact[host]
    for suffix, platform in suffixes:
        if host.endswith(suffix):
            return platform
    return None


def destination(url, *, allow_source=True):
    """Validate and classify an HTTPS application URL."""
    try:
        parsed = urlsplit(url or '')
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError('Invalid employer application URL') from None
    host = (parsed.hostname or '').lower().rstrip('.')
    if (parsed.scheme.lower() != 'https' or not host or parsed.username or
            parsed.password or port not in (None, 443)):
        raise ValueError('Use an HTTPS employer application URL without credentials or a custom port')
    direct = _platform_for_host(host, DIRECT_EXACT_HOSTS, DIRECT_SUFFIX_HOSTS)
    if direct:
        return Destination(direct, 'direct', host)
    source = _platform_for_host(host, SOURCE_EXACT_HOSTS, SOURCE_SUFFIX_HOSTS)
    if source and allow_source:
        return Destination(source, 'source', host)
    if source:
        raise ValueError('Use the direct employer ATS URL, not a source-board page')
    raise ValueError(
        'Unsupported application host; use LinkedIn or a direct Greenhouse, Ashby, '
        'Lever, Workday, SmartRecruiters, iCIMS, BambooHR, Subway or Fountain URL'
    )


def platform_id(url, *, allow_source=False):
    try:
        return destination(url, allow_source=allow_source).platform
    except ValueError:
        return None


def request_allowed(host, page_platform, page_host):
    """Return whether a browser request may leave the current page origin."""
    host = (host or '').lower().rstrip('.')
    page_host = (page_host or '').lower().rstrip('.')
    direct = _platform_for_host(host, DIRECT_EXACT_HOSTS, DIRECT_SUFFIX_HOSTS)
    source_redirect = page_platform == 'linkedin' and bool(direct)
    subway_redirect = page_platform == 'subway' and direct == 'fountain'
    return bool(host and (host == page_host or source_redirect or subway_redirect
                          or host == 'challenges.cloudflare.com'
                          or any(host.endswith(suffix)
                                 for suffix in REQUESTABLE.get(page_platform, ()))))

