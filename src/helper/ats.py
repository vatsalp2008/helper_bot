"""Which applicant tracking system a posting URL belongs to (spec 9 step 2)."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qsl, urlparse

_HOSTS: list[tuple[str, str]] = [
    ("greenhouse", r"(^|\.)greenhouse\.io$"),
    ("lever", r"(^|\.)lever\.co$"),
    ("ashby", r"(^|\.)ashbyhq\.com$"),
    ("workday", r"(^|\.)myworkdayjobs\.com$|(^|\.)myworkdaysite\.com$|(^|\.)workday\.com$"),
    ("icims", r"(^|\.)icims\.com$"),
    ("smartrecruiters", r"(^|\.)smartrecruiters\.com$"),
    ("taleo", r"(^|\.)taleo\.net$"),
    # SAP's own career site runs on SuccessFactors (its iXp internships, 2026-11-23)
    ("successfactors", r"(^|\.)successfactors\.(com|eu)$|(^|\.)sapsf\.com$|^(careers|jobs)\.sap\.com$"),
    ("workable", r"(^|\.)workable\.com$"),
    ("rippling", r"(^|\.)rippling\.com$"),
    ("jazzhr", r"(^|\.)applytojob\.com$"),
    ("ukg", r"(^|\.)ultipro\.com$|(^|\.)ukg\.net$"),
    ("bamboohr", r"(^|\.)bamboohr\.com$"),
    ("jobvite", r"(^|\.)jobvite\.com$"),
    ("avature", r"(^|\.)avature\.net$"),
    ("oracle", r"(^|\.)oraclecloud\.com$"),
    ("eightfold", r"(^|\.)eightfold\.ai$"),
    ("linkedin", r"(^|\.)linkedin\.com$"),
    ("handshake", r"(^|\.)joinhandshake\.com$"),
]


def detect_ats(url: Optional[str]) -> str:
    if not url:
        return "unknown"
    host = (urlparse(url).hostname or "").lower()
    for name, pattern in _HOSTS:
        if re.search(pattern, host):
            return name
    if re.match(r"(?i)/hcmUI/CandidateExperience/", urlparse(url).path or ""):
        return "oracle"  # Oracle Candidate Experience on a company's own domain (Dell, 2026-11-23)
    return "unknown"


# Job ids by ATS, read from the posting URL (spec 9 step 1: the duplicate check uses them).
_JOB_ID_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"/jobs/(\d+)")),
    ("greenhouse", re.compile(r"[?&](?:token|gh_jid)=(\d+)")),
    ("lever", re.compile(r"lever\.co/[^/]+/([0-9a-f-]{36})", re.I)),
    ("ashby", re.compile(r"ashbyhq\.com/[^/]+/([0-9a-f-]{36})", re.I)),
    ("workday", re.compile(r"/job/(?:[^/]+/)*[^/]*?_([A-Za-z]*-?\d[\w-]*?)(?:-\d+)?(?:/|$|\?)")),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com/[^/]+/(\d{6,})")),
    ("icims", re.compile(r"/jobs/(\d+)/")),
]


def job_id_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    ats = detect_ats(url)
    for name, pattern in _JOB_ID_PATTERNS:
        if name == ats or (name == "greenhouse" and "gh_jid=" in url):
            m = pattern.search(url)
            if m:
                return m.group(1).lower()
    return None


_ID_QUERY_KEYS = {"token", "gh_jid", "jobid", "job_id", "jid", "id", "req", "reqid", "requisitionid"}


def normalize_url(url: Optional[str]) -> Optional[str]:
    """Host, path and job-id query keys only: no scheme, www, tracking query, fragment, trailing slash, or
    apply/application suffix."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"/(?:apply|application)(?:/(?:applyManually|autofillWithResume|useMyLastApplication))?/?$", "",
                  parsed.path.rstrip("/"), flags=re.I)
    keep = sorted(f"{k.lower()}={v}" for k, v in parse_qsl(parsed.query) if k.lower() in _ID_QUERY_KEYS)
    return (f"{host}{path}".lower() + (f"?{'&'.join(keep)}" if keep else "")) or None


_TRACKING_KEYS = re.compile(r"^(?:fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|igshid|ref|referrer|source|gh_src|lever-source"
                            r"|lever-origin|jr_id|utm_[a-z]+)$", re.I)


def clean_url(url: str) -> str:
    """The posting link without tracking parameters (fbclid, utm_*, gh_src...), so it is what gets stored."""
    from urllib.parse import urlencode, urlunparse

    parsed = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not _TRACKING_KEYS.match(k)]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))
