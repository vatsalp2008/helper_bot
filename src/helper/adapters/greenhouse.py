"""Greenhouse (spec 10.2): intake from the public job board API, and filling job-boards.greenhouse.io forms.

Every form field on job-boards.greenhouse.io carries the API field name as its id (first_name, question_123...),
and every dropdown is a combobox whose options have ids react-select-<field id>-option-<n>. Options are always
looked up per field: the phone widget's hidden country list also uses role=option.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

import requests

from ..errors import HelperError
from ..htmltext import html_to_text
from ..models import FormQuestion, Job
from ..tracker.db import now_iso

API = "https://boards-api.greenhouse.io/v1/boards"
APPLY_BASE = "https://job-boards.greenhouse.io"
_TYPES = {
    "input_text": "text",
    "textarea": "textarea",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multi_select",
    "boolean": "boolean",
}
from .common import CONFIRMATION_RE, FieldResult  # noqa: E402,F401  (re-exported for callers and tests)
from . import common  # noqa: E402


@dataclass(frozen=True)
class GreenhouseRef:
    board: Optional[str]  # None: found from the link's company domain at fetch time
    job_id: str


def parse_ref(url: str) -> Optional[GreenhouseRef]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    if host.endswith("greenhouse.io"):
        m = re.match(r"^/([A-Za-z0-9_-]+)/jobs/(\d+)", parsed.path)
        if m and m.group(1) != "embed":
            return GreenhouseRef(m.group(1).lower(), m.group(2))
        if parsed.path.startswith("/embed/job_app") and query.get("token"):
            return GreenhouseRef((query.get("for") or [None])[0], query["token"][0])
    if query.get("gh_jid"):
        return GreenhouseRef(None, query["gh_jid"][0])
    return None


def _board_candidates(url: str) -> list[str]:
    """For a company careers page with ?gh_jid=, the board token is usually the company's domain name."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    labels = [p for p in host.split(".") if p not in ("com", "io", "co", "org", "net", "ai", "careers", "jobs", "www")]
    out: list[str] = []
    for label in labels:
        for candidate in (label, label.replace("-", "")):
            if candidate and candidate not in out:
                out.append(candidate)
    return out


def _get(session: Any, url: str, **params: str) -> Any:
    try:
        r = session.get(url, params=params or None, timeout=30)
    except requests.RequestException as exc:
        raise HelperError(f"can't reach Greenhouse ({type(exc).__name__})") from None
    return r


def fetch_job(url: str, session: Optional[Any] = None) -> Job:
    session = session or requests.Session()
    ref = parse_ref(url)
    if ref is None:
        raise HelperError(f"{url} isn't a Greenhouse posting link")
    boards = [ref.board] if ref.board else _board_candidates(url)
    data, board = None, None
    for candidate in boards:
        r = _get(session, f"{API}/{candidate}/jobs/{ref.job_id}", questions="true")
        if r.status_code == 200:
            data, board = r.json(), candidate
            break
    if data is None or board is None:
        tried = ", ".join(boards) or "none"
        raise HelperError(f"Greenhouse has no open job {ref.job_id} (boards tried: {tried}). The posting may be closed, or "
                          "the link is a company page: open it, copy the job-boards.greenhouse.io link, and try again.")
    return job_from_api(data, url, board)


def job_from_api(data: dict[str, Any], url: str, board: str) -> Job:
    content = data.get("content") or ""
    location = (data.get("location") or {}).get("name")
    return Job(
        url=url,
        ats="greenhouse",
        company=data.get("company_name") or board.replace("-", " ").title(),
        role_title=data["title"],
        location=location,
        job_id=str(data["id"]),
        description_text=html_to_text(content),
        description_html=content,
        questions=questions_from_api(data),
        source="greenhouse_api",
        fetched_at=now_iso(),
        apply_url=f"{APPLY_BASE}/{board}/jobs/{data['id']}",
    )


def _question(label: str, field: dict[str, Any], required: bool, section: str,
              description: Optional[str] = None) -> Optional[FormQuestion]:
    kind = _TYPES.get(field.get("type", ""))
    if kind is None:  # input_hidden (latitude, longitude) and anything unknown
        return None
    return FormQuestion(
        label=label.strip(),
        field_name=field["name"],
        type=kind,
        required=required,
        options=[v["label"] for v in field.get("values", []) if v.get("label")],
        description=html_to_text(description) if description else None,
        section=section,
    )


def questions_from_api(data: dict[str, Any]) -> list[FormQuestion]:
    out: list[FormQuestion] = []
    for section, questions in (("questions", data.get("questions") or []),
                               ("location", data.get("location_questions") or [])):
        for q in questions:
            for f in q.get("fields", []):
                question = _question(q["label"], f, bool(q.get("required")), section, q.get("description"))
                if question:
                    out.append(question)
    for block in data.get("compliance") or []:
        for q in block.get("questions", []):
            for f in q.get("fields", []):
                if f.get("name") == "race":  # the form asks Hispanic/Latino first; the API leaves that question out
                    out.append(FormQuestion(label=_EEOC_LABELS["hispanic_ethnicity"], field_name="hispanic_ethnicity",
                                            type="select", required=False, section="eeoc",
                                            options=["Yes", "No", "Decline To Self Identify"]))
                question = _question(_EEOC_LABELS.get(f["name"], q["label"]), f, bool(q.get("required")), "eeoc")
                if question:
                    out.append(question)
    demographic = data.get("demographic_questions") or {}
    for q in demographic.get("questions", []) if isinstance(demographic, dict) else []:
        kind = "multi_select" if q.get("type") == "multi_value_multi_select" else "select"
        out.append(FormQuestion(label=q["label"], field_name=f"demographic_{q['id']}", type=kind,
                                required=bool(q.get("required")),
                                options=[o["label"] for o in q.get("answer_options", [])], section="demographic"))
    return out


_EEOC_LABELS = {
    "gender": "Gender",
    "race": "Race",
    "hispanic_ethnicity": "Are you Hispanic/Latino?",
    "veteran_status": "Veteran Status",
    "disability_status": "Disability Status",
}


# --------------------------------------------------------------------------- filling the form


def dom_id(field_name: str) -> str:
    """The DOM id of a field. The location question renders as #candidate-location."""
    return "candidate-location" if field_name == "location" else field_name


def css_id(dom: str) -> str:
    return "#" + re.sub(r"([^A-Za-z0-9_-])", r"\\\1", dom)


_keyboard = common.keyboard
_FORM_ANCHORS = ("#first_name", "#email", "#application-form", "#application_form")


def find_form(page: Any, timeout_ms: int = 20000) -> Optional[Any]:
    """The frame holding the application form: the page itself, or the Greenhouse frame a company careers page
    embeds (job-boards links can forward there, like nuro.ai/careersitem?gh_jid=...)."""
    return common.find_form(page, _FORM_ANCHORS, hosts=("greenhouse.io",), timeout_ms=timeout_ms)


def choose_option(root: Any, dom: str, wanted: str, *, search: Optional[str] = None,
                  timeout_ms: int = 6000) -> tuple[bool, str]:
    """Open the dropdown for one field, optionally type to search, and click the best matching option."""
    from ..answers import match_option

    box = root.locator(css_id(dom))
    box.click()
    if search:
        box.fill("")
        box.type(search, delay=40)
    options = root.locator(f"[id^='react-select-{dom}-option-']")
    try:
        options.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        _keyboard(root).press("Escape")
        return False, "no options appeared"
    texts = [t.strip() for t in options.all_inner_texts()]
    best, score = match_option(wanted, texts)
    if best is None or score < 0.8:
        _keyboard(root).press("Escape")
        return False, f"no option matches '{wanted}' (options: {', '.join(texts[:12])})"
    options.nth(texts.index(best)).click()
    return True, best


def fill_form(page: Any, answers: list[Any], resume_pdf: Path, *, country: Optional[str], city: Optional[str],
              pause: Callable[[], None]) -> list[FieldResult]:
    """Fill every planned answer in `page` (a Page or the Frame find_form returned). Never touches Submit."""
    results: list[FieldResult] = []
    if country and page.locator("#country").count():
        ok, chosen = choose_option(page, "country", country, search=country[:9])
        results.append(FieldResult("country", "Country (phone)", ok, chosen if ok else None, "" if ok else chosen))
        pause()
    for a in answers:
        if a.kind in ("skip", "unknown") or (a.value is None and a.type != "file"):
            continue
        dom = dom_id(a.field)
        target = page.locator(css_id(dom))
        if not target.count():  # optional fields can be hidden (Race shows only after some Hispanic/Latino answers)
            results.append(FieldResult(a.field, a.label, not a.required, None,
                                       "not shown on this form" if not a.required else "field not found on the page"))
            continue
        try:
            if a.type == "file":
                upload = Path(a.value) if a.key == "transcript" and a.value else resume_pdf
                target.set_input_files(str(upload))
                results.append(FieldResult(a.field, a.label, True, upload.name))
            elif (target.get_attribute("role") or "") == "combobox":
                if a.field == "location":
                    ok, chosen = choose_option(page, dom, a.value, search=city or a.value.split(",")[0])
                elif a.type == "multi_select":
                    picks = [v.strip() for v in a.value.split(";") if v.strip()]
                    chosen_all = []
                    ok = True
                    for pick in picks:
                        one_ok, one = choose_option(page, dom, pick)
                        ok = ok and one_ok
                        chosen_all.append(one)
                    chosen = "; ".join(chosen_all)
                else:
                    ok, chosen = choose_option(page, dom, a.value)
                results.append(FieldResult(a.field, a.label, ok, chosen if ok else a.value, "" if ok else chosen))
            else:
                target.fill(a.value)
                results.append(FieldResult(a.field, a.label, True, a.value))
        except Exception as exc:  # a field that won't take input is reported, not fatal
            results.append(FieldResult(a.field, a.label, False, a.value, f"{type(exc).__name__}: {str(exc)[:120]}"))
        pause()
    return results


def is_confirmation(page: Any, form: Optional[Any] = None) -> bool:
    return common.is_confirmation(page, form, hosts=("greenhouse.io",))


def is_submit_request(url: str, method: str) -> bool:
    return method == "POST" and bool(re.search(r"greenhouse\.io/.*(?:applications|application_submit|/apply)", url))
