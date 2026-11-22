"""Ashby (spec 10.2): the job description from the public job board API, the questions from the application page."""

from __future__ import annotations

import re
from datetime import date
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


EDUCATION = "[data-field-path='_systemfield_education_history']"
_EDUCATION_ENTRY = ".ashby-application-form-input-education-entry"
_EDUCATION_ADD = "button.ashby-application-form-input-education-add, button:has-text('Add Education')"


def fill_education(root: Any, schools: Any, pause: Callable[[], None]) -> list[common.FieldResult]:
    """Ashby's Education History (a system field): one entry per school in the profile, with the school picked from
    Ashby's school search, degree, field of study, start and end month and year, and "Still Student?" for the school he
    is at now. Secureframe's was required (2026-11-21); helper had read the whole field as one checkbox and left it to
    Vatsal. Forms without it are left alone."""
    widget = root.locator(EDUCATION)
    if not widget.count():
        return []
    try:
        schools = schools() if callable(schools) else schools
    except HelperError as exc:
        return [common.FieldResult("education-0", "Education", False, None, f"couldn't list your schools: {exc}")]
    entries = widget.locator(_EDUCATION_ENTRY)
    today = date.today()
    results: list[common.FieldResult] = []
    for i, s in enumerate(schools):
        field, label = f"education-{i}", f"Education {i + 1}: {s.degree}, {s.school}"
        if entries.count() <= i:
            add = widget.locator(_EDUCATION_ADD)
            if not add.count():
                break  # the form takes one school: the first (current) one is in
            add.first.click()
            try:
                entries.nth(i).wait_for(timeout=5000)
            except Exception:
                results.append(common.FieldResult(field, label, False, None, "the next education entry didn't appear"))
                break
        entry = entries.nth(i)
        current = s.end is not None and (s.end.year, s.end.month) >= (today.year, today.month)
        problems: list[str] = []
        try:
            ok, chosen = _choose_school(root, entry, s.school)
            if not ok:
                problems.append(f"school: {chosen}")
            for suffix, text in (("degree", s.degree), ("major", s.field_of_study)):
                box = entry.locator(f"input[id$='-{suffix}']")
                if text and box.count():
                    box.first.fill(text)
                    if box.first.input_value() != text:
                        problems.append(f"{suffix} didn't keep its text")
            for which, when in (("startDate", s.start), ("endDate", s.end)):
                selects = entry.locator(f"[id$='-{which}'] select")
                if when is None or selects.count() < 2:
                    continue
                offered = selects.nth(1).evaluate("s => [...s.options].map(o => o.value)")
                if str(when.year) not in offered:
                    if which == "endDate" and current:
                        continue  # a graduation year the list doesn't offer yet: "Still Student?" says it
                    problems.append(f"{which}: {when.year} isn't one of the years")
                    continue
                selects.nth(0).select_option(value=str(when.month))
                selects.nth(1).select_option(value=str(when.year))
            if current:
                still = entry.locator("input[type=checkbox][id$='-isCurrent'], input[type=checkbox]")
                if still.count() and not common.tick(still.first):
                    problems.append("'Still Student?' didn't tick")
        except Exception as exc:  # one entry's trouble is reported; the rest of the form still gets filled
            problems.append(f"{type(exc).__name__}: {str(exc)[:120]}")
        span = " to ".join(w.display() for w in (s.start, None if current else s.end) if w) + (
            " to present" if current else "")
        results.append(common.FieldResult(field, label, not problems, span, "; ".join(problems)))
        pause()
    return results


def _choose_school(root: Any, entry: Any, school: str) -> tuple[bool, str]:
    """Search Ashby's school list and pick the entry that names the school (not the first one it lists: a search for
    'Northeastern' also lists Northeastern Illinois University). Nothing chosen leaves the box empty."""
    from ..answers import match_option
    from .workday_page import school_search_terms

    box = entry.locator("input[role=combobox]").first
    listed: list[str] = []
    for term in school_search_terms(school):
        box.click()
        box.fill("")
        box.type(term, delay=40)
        options = root.locator("[role=listbox] [role=option]")
        try:
            options.first.wait_for(timeout=8000)
            box.page.wait_for_timeout(400)  # the list settles
        except Exception:
            continue
        names = [" ".join((o.locator("span").first.inner_text() if o.locator("span").count() else o.inner_text()).split())
                 for o in options.all()]
        listed = names
        scores = [match_option(school, [n])[1] for n in names]
        best = max(range(len(names)), key=lambda k: scores[k]) if names else -1
        if best >= 0 and scores[best] >= 0.85:
            options.nth(best).click()
            shown = ""
            for _ in range(15):  # the box shows the pick a moment after the click (Secureframe, 2026-11-21)
                shown = box.input_value()
                if school.lower() in shown.lower() or names[best].lower() in shown.lower():
                    return True, names[best]
                box.page.wait_for_timeout(200)
            return False, f"picked '{names[best]}' but the box shows '{shown}'"
    box.fill("")
    try:
        box.press("Escape")
    except Exception:
        pass
    seen = f" (it listed: {', '.join(listed[:4])})" if listed else " (the search listed nothing)"
    return False, f"'{school}' isn't in Ashby's school list{seen}"


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
