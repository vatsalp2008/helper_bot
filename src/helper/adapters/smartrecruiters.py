"""SmartRecruiters, read only. helper never fills these applications (rule 8: they need an account at each company),
but it reads the posting from SmartRecruiters' public API, so `helper resume <link>` can build the resume and cover
letter. (Its job pages show helper's browser an "Internet Explorer 11 is no longer supported" page instead.)"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from ..errors import HelperError
from ..htmltext import html_to_text
from ..models import Job
from ..tracker.db import now_iso

API = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting}"
_SECTIONS = ("jobDescription", "qualifications", "additionalInformation", "companyDescription")


@dataclass(frozen=True)
class SmartRecruitersRef:
    company: str
    posting: str


def parse_ref(url: str) -> Optional[SmartRecruitersRef]:
    parsed = urlparse(url)
    if not re.search(r"(^|\.)smartrecruiters\.com$", parsed.hostname or ""):
        return None
    m = re.match(r"^/([\w.-]+)/(\d{6,})(?:[-/]|$)", parsed.path)
    return SmartRecruitersRef(m.group(1), m.group(2)) if m else None


def fetch_job(url: str, session: Optional[Any] = None) -> Job:
    import requests

    ref = parse_ref(url)
    if ref is None:
        raise HelperError(f"{url} isn't a SmartRecruiters posting link (jobs.smartrecruiters.com/<company>/<id>)")
    session = session or requests.Session()
    try:
        r = session.get(API.format(company=ref.company, posting=ref.posting), timeout=30)
    except requests.RequestException as exc:
        raise HelperError(f"Couldn't reach SmartRecruiters ({type(exc).__name__}). Check your internet connection and "
                          "try again.") from None
    if r.status_code != 200:
        raise HelperError(f"SmartRecruiters has no open posting {ref.posting} at {ref.company}; it may be closed. If it "
                          "isn't, save the job description as text and use `--jd-file`.")
    data = r.json()
    sections = (data.get("jobAd") or {}).get("sections") or {}
    parts = [html_to_text((sections.get(k) or {}).get("text") or "") for k in _SECTIONS]
    description = "\n\n".join(p for p in parts if p.strip())
    if len(description) < 300:
        raise HelperError(f"couldn't read the job description at {url}. Save it as text and use `--jd-file`.")
    return Job(url=url, ats="smartrecruiters", company=(data.get("company") or {}).get("name") or ref.company,
               role_title=(data.get("name") or "").strip() or "Unknown role",
               location=(data.get("location") or {}).get("fullLocation"), job_id=ref.posting,
               description_text=description, source="smartrecruiters_api", fetched_at=now_iso(),
               apply_url=data.get("applyUrl") or url)
