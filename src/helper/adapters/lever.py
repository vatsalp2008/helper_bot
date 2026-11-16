"""Lever (spec 10.2): the job description from the public postings API, the questions from the apply page."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

from ..errors import HelperError
from ..htmltext import html_to_text
from ..models import FormQuestion, Job
from ..tracker.db import now_iso
from . import common, dom

ANCHORS = ("input[name=email]", "input[name=resume]", "#application-form")
HOSTS = ("lever.co",)


def parse_ref(url: str) -> Optional[tuple[str, str, bool]]:
    """(company, posting id, EU) for jobs.lever.co/<company>/<id>[/apply] links."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host.endswith("lever.co"):
        return None
    m = re.match(r"^/([^/]+)/([0-9a-f-]{36})", parsed.path, re.I)
    return (m.group(1), m.group(2).lower(), ".eu." in host) if m else None


def fetch_job(url: str, session: Optional[Any] = None,
              scan: Callable[..., tuple[list[FormQuestion], str]] = dom.scan_form) -> Job:
    ref = parse_ref(url)
    if ref is None:
        raise HelperError(f"{url} isn't a Lever posting link (jobs.lever.co/<company>/<id>)")
    company, posting, eu = ref
    api = f"https://api.{'eu.' if eu else ''}lever.co/v0/postings/{company}/{posting}"
    try:
        r = (session or requests.Session()).get(api, timeout=30)
    except requests.RequestException as exc:
        raise HelperError(f"Couldn't reach Lever ({type(exc).__name__}). Check your internet connection and "
                          "try again.") from None
    if r.status_code != 200:
        raise HelperError(f"Lever has no open posting {posting} at {company}; it may be closed")
    data = r.json()
    apply_url = data.get("applyUrl") or f"{data.get('hostedUrl', url).rstrip('/')}/apply"
    questions, title = scan(apply_url, ANCHORS, HOSTS)
    return job_from_api(data, url, company, questions, title, apply_url)


def job_from_api(data: dict[str, Any], url: str, company: str, questions: list[FormQuestion], page_title: str,
                 apply_url: str) -> Job:
    parts = [data.get("description") or data.get("descriptionBody") or ""]
    for block in data.get("lists") or []:
        parts.append(f"<h3>{block.get('text', '')}</h3><ul>{block.get('content', '')}</ul>")
    parts.append(data.get("additional") or "")
    name = page_title.split(" - ")[0].strip() if " - " in page_title else company.replace("-", " ").title()
    categories = data.get("categories") or {}
    return Job(
        url=url, ats="lever", company=name, role_title=data["text"], location=categories.get("location"),
        job_id=data["id"], description_text=html_to_text("".join(parts)), description_html="".join(parts),
        questions=questions, source="lever_api", fetched_at=now_iso(), apply_url=apply_url,
    )


def find_form(page: Any, timeout_ms: int = 20000) -> Optional[Any]:
    return common.find_form(page, ANCHORS, hosts=HOSTS, timeout_ms=timeout_ms)


def fill_form(page: Any, answers: list[Any], resume_pdf: Path, *, country: Optional[str], city: Optional[str],
              pause: Callable[[], None]) -> list[common.FieldResult]:
    return dom.fill_dom_form(page, answers, resume_pdf, city=city, pause=pause)


def is_confirmation(page: Any, form: Optional[Any] = None) -> bool:
    return common.is_confirmation(page, form, hosts=HOSTS)


def submit(form: Any) -> bool:
    """Auto mode only: click Submit once."""
    return common.click_submit(form, ("#btn-submit", "[data-qa=btn-submit]"))


def is_submit_request(url: str, method: str) -> bool:
    return method == "POST" and bool(re.search(r"lever\.co/[^/]+/[0-9a-f-]{36}/apply", url, re.I))
