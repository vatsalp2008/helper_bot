"""Ashby (spec 10.2): the job description from the public job board API, the questions from the application page."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests

from ..dates import parse_month_year
from ..errors import HelperError
from ..htmltext import html_to_text
from ..models import FormQuestion, Job
from ..tracker.db import now_iso
from . import common, dom

API = "https://api.ashbyhq.com/posting-api/job-board"
ANCHORS = ("[data-field-path='_systemfield_email']", "#_systemfield_email", ".ashby-application-form-container")
HOSTS = ("ashbyhq.com",)


def parse_ref(url: str) -> Optional[tuple[Optional[str], str]]:
    """(organization, job id) for jobs.ashbyhq.com/<org>/<id>[/application] links; a company page with
    ?ashby_jid=<id> gives (None, id), and the organization comes from the company's domain."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("ashbyhq.com"):
        m = re.match(r"^/([^/]+)/([0-9a-f-]{36})", parsed.path, re.I)
        return (m.group(1), m.group(2).lower()) if m else None
    jid = parse_qs(parsed.query).get("ashby_jid")
    return (None, jid[0].lower()) if jid else None


def _org_candidates(url: str) -> list[str]:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    return [p for p in host.split(".") if p not in ("com", "io", "co", "org", "net", "ai", "careers", "jobs")]


def fetch_job(url: str, session: Optional[Any] = None,
              scan: Callable[..., tuple[list[FormQuestion], str]] = dom.scan_form) -> Job:
    ref = parse_ref(url)
    if ref is None:
        raise HelperError(f"{url} isn't an Ashby posting link (jobs.ashbyhq.com/<org>/<id>)")
    org, job_id = ref
    session = session or requests.Session()
    for candidate in [org] if org else _org_candidates(url):
        try:
            r = session.get(f"{API}/{candidate}", params={"includeCompensation": "true"}, timeout=30)
        except requests.RequestException as exc:
            raise HelperError(f"Couldn't reach Ashby ({type(exc).__name__}). Check your internet connection and "
                          "try again.") from None
        if r.status_code != 200:
            continue
        job = next((j for j in r.json().get("jobs", []) if str(j.get("id", "")).lower() == job_id), None)
        if job:
            apply_url = f"https://jobs.ashbyhq.com/{candidate}/{job_id}/application"
            questions, title = scan(apply_url, ANCHORS, HOSTS)
            return job_from_api(job, url, candidate, questions, title, apply_url)
    raise HelperError(f"Ashby has no open job {job_id}; it may be closed")


def job_from_api(data: dict[str, Any], url: str, org: str, questions: list[FormQuestion], page_title: str,
                 apply_url: str) -> Job:
    name = page_title.rsplit(" @ ", 1)[1].strip() if " @ " in page_title else org.replace("-", " ").title()
    return Job(
        url=url, ats="ashby", company=name, role_title=data["title"], location=data.get("location"),
        job_id=str(data["id"]), description_text=data.get("descriptionPlain") or html_to_text(data.get("descriptionHtml") or ""),
        description_html=data.get("descriptionHtml"), questions=questions, source="ashby_api", fetched_at=now_iso(),
        apply_url=apply_url,
    )


def find_form(page: Any, timeout_ms: int = 20000) -> Optional[Any]:
    return common.find_form(page, ANCHORS, hosts=HOSTS, timeout_ms=timeout_ms)


def date_text(value: str) -> str:
    """Ashby's date picker takes MM/DD/YYYY; a month-only date (a graduation date) uses the 1st."""
    my = parse_month_year(value)
    return f"{my.month:02d}/01/{my.year}" if my else value


def fill_form(page: Any, answers: list[Any], resume_pdf: Path, *, country: Optional[str], city: Optional[str],
              pause: Callable[[], None]) -> list[common.FieldResult]:
    return dom.fill_dom_form(page, answers, resume_pdf, city=city, pause=pause, date_text=date_text)


def is_confirmation(page: Any, form: Optional[Any] = None) -> bool:
    return common.is_confirmation(page, form, hosts=HOSTS)


SUBMIT_BUTTONS = ("button.ashby-application-form-submit-button", "button:has-text('Submit Application')")


def submit(form: Any) -> bool:
    """Auto mode only: click Submit once."""
    return common.click_submit(form, SUBMIT_BUTTONS)


def submit_buttons(form: Any) -> list[Any]:
    """The Submit button while the form is still there. With it, a submit the form refused is seen as not sent:
    Snowflake's "Your form needs corrections" was logged as submitted_unverified and put in Notion (2026-11-19)."""
    return common.visible_buttons(form, SUBMIT_BUTTONS)


def is_submit_request(url: str, method: str) -> bool:
    return method == "POST" and bool(re.search(r"ashbyhq\.com/api/non-user-graphql\?op=\w*Submit", url))
