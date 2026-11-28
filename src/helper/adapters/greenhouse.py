"""Greenhouse (spec 10.2): intake from the public job board API, and filling job-boards.greenhouse.io forms.

Every form field on job-boards.greenhouse.io carries the API field name as its id (first_name, question_123...),
and every dropdown is a combobox whose options have ids react-select-<field id>-option-<n>. Options are always
looked up per field: the phone widget's hidden country list also uses role=option.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Union
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


# A branded domain around the company's name: careers.withwaymo.com is board "waymo" (Waymo, 2026-11-19).
_BRAND_AFFIXES = (re.compile(r"^(?:with|join|get|go|try|use|hello|meet)(?=[a-z0-9]{4})"),
                  re.compile(r"(?<=[a-z0-9]{4})(?:hq|app|inc|careers|jobs|team)$"))


def _board_candidates(url: str) -> list[str]:
    """For a company careers page with ?gh_jid=, the board token is usually the company's domain name, or that name
    without a brand prefix or suffix. Job ids are unique across Greenhouse, so a wrong guess only costs a 404."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    labels = [p for p in host.split(".") if p not in ("com", "io", "co", "org", "net", "ai", "careers", "jobs", "www")]
    out: list[str] = []
    for label in labels:
        plain = label.replace("-", "")
        for candidate in (label, plain, *(affix.sub("", plain) for affix in _BRAND_AFFIXES)):
            if candidate and candidate not in out:
                out.append(candidate)
    return out


_PAGE_BOARD_RES = (
    re.compile(r"greenhouse\.io/embed/job_(?:board|app)[^\"'\s<>]*?[?&](?:amp;)?for=([A-Za-z0-9_-]+)", re.I),
    re.compile(r"greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I),
    re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/", re.I),
)


def boards_on_page(html: str) -> list[str]:
    """Board names a company careers page gives in its Greenhouse embed. They needn't match the domain: current.com
    loads board "current81"."""
    out: list[str] = []
    for pattern in _PAGE_BOARD_RES:
        for token in pattern.findall(html):
            token = token.lower()
            if token != "embed" and token not in out:
                out.append(token)
    return out


def _page_boards(session: Any, url: str) -> list[str]:
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException:
        return []
    return boards_on_page(r.text or "") if r.status_code == 200 else []


def _get(session: Any, url: str, **params: str) -> Any:
    try:
        r = session.get(url, params=params or None, timeout=30)
    except requests.RequestException as exc:
        raise HelperError(f"Couldn't reach Greenhouse ({type(exc).__name__}). Check your internet connection and "
                          "try again.") from None
    return r


def fetch_job(url: str, session: Optional[Any] = None) -> Job:
    session = session or requests.Session()
    ref = parse_ref(url)
    if ref is None:
        raise HelperError(f"{url} isn't a Greenhouse posting link")
    boards = [ref.board] if ref.board else _board_candidates(url)
    data, board = None, None

    def look(candidates: list[str]) -> None:
        nonlocal data, board
        for candidate in candidates:
            r = _get(session, f"{API}/{candidate}/jobs/{ref.job_id}", questions="true")
            if r.status_code == 200:
                data, board = r.json(), candidate
                return

    look(boards)
    if data is None and ref.board is None:  # the company page names its board in its Greenhouse embed
        on_page = [b for b in _page_boards(session, url) if b not in boards]
        boards += on_page
        look(on_page)
    if data is None or board is None:
        tried = ", ".join(boards) or "none"
        raise HelperError(f"Greenhouse has no open job {ref.job_id} (boards tried: {tried}). The posting may be closed, or "
                          "the link is a company page: open it, copy the job-boards.greenhouse.io link, and try again.")
    return job_from_api(data, url, board)


def board_company(name: str) -> str:
    """The company in a board's name: "Rubrik Job Board" is Rubrik (2026-11-24)."""
    return re.sub(r"(?i)[\s,|:-]+(?:job board|jobs? board|careers?(?: site| page)?|jobs)$", "", name.strip()).strip()


def job_from_api(data: dict[str, Any], url: str, board: str) -> Job:
    content = data.get("content") or ""
    location = (data.get("location") or {}).get("name")
    return Job(
        url=url,
        ats="greenhouse",
        company=board_company(data.get("company_name") or "") or board.replace("-", " ").title(),
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
    """The DOM id of a field. The location question renders as #candidate-location, and a demographic question as
    its bare number: demographic_4000319003 is id="4000319003" (Soros, 2026-11-17)."""
    if field_name == "location":
        return "candidate-location"
    if field_name.startswith("demographic_") and field_name[len("demographic_"):].isdigit():
        return field_name[len("demographic_"):]
    return field_name


def css_id(dom: str) -> str:
    if dom[:1].isdigit():  # "#4000319003" isn't a valid CSS selector: an id can't start with a digit there
        return f"[id='{dom}']"
    return "#" + re.sub(r"([^A-Za-z0-9_-])", r"\\\1", dom)


_keyboard = common.keyboard
_FORM_ANCHORS = ("#first_name", "#email", "#application-form", "#application_form")
EMBED_FORM = f"{APPLY_BASE}/embed/job_app"


def own_form_url(url: str, landed: str) -> Optional[str]:
    """Where the form is when the posting link left Greenhouse. A board with its own careers site sends job-boards
    links there: Lucid's went to lucidmotors.com/careers/search/<id>?gh_jid=<id>, whose form sits behind an Apply
    button, and helper typed only the email, into the site's own email box (2026-11-19). Greenhouse's embed page is
    the same application form a careers site loads, on Greenhouse itself."""
    if not landed or "greenhouse.io" in (urlparse(landed).hostname or ""):
        return None
    ref = parse_ref(url)
    if ref is None or not ref.board:
        return None
    return f"{EMBED_FORM}?for={ref.board}&token={ref.job_id}"


def find_form(page: Any, timeout_ms: int = 20000) -> Optional[Any]:
    """The frame holding the application form: the page itself, or the Greenhouse frame a company careers page
    embeds (job-boards links can forward there, like nuro.ai/careersitem?gh_jid=...)."""
    form = common.find_form(page, _FORM_ANCHORS, hosts=("greenhouse.io",), timeout_ms=timeout_ms)
    if form is None or form is page.main_frame or "greenhouse.io" not in (form.url or ""):
        return form
    # The careers page around an embedded form can cover it: Akuna's cookie banner made every dropdown click time out
    # (2026-11-18), so the window opens the embedded form's own page, the same Greenhouse form without the site around it
    try:
        page.goto(form.url, wait_until="domcontentloaded")
    except Exception:
        return form
    return common.find_form(page, _FORM_ANCHORS, hosts=("greenhouse.io",), timeout_ms=timeout_ms) or form


def posting_closed(page: Any) -> Optional[str]:
    """Why the posting is gone, else None. Asked only when the form didn't come up. A taken-down posting's link sends
    the window to the company's job board with "?error=true" and no message: Appodeal's came down between the fill and
    the reopen (2026-11-27), and helper told Vatsal to fill a form that wasn't there and waited a day."""
    for url in (getattr(page, "url", "") or "", getattr(getattr(page, "main_frame", None), "url", "") or ""):
        parsed = urlparse(url)
        if (parsed.hostname or "").endswith("greenhouse.io") and "/jobs/" not in parsed.path \
                and parse_qs(parsed.query).get("error") == ["true"]:
            return "the posting link now opens the company's Greenhouse job board (error=true) instead of the job"
    return None


def choose_option(root: Any, dom: str, wanted: str, *, search: Optional[str] = None, timeout_ms: int = 6000,
                  score: Optional[Callable[[str], float]] = None, threshold: float = 0.8) -> tuple[bool, str]:
    """Open the dropdown for one field, optionally type to search, and click the best matching option. `score` rates
    one option's text (default: how closely it says `wanted`)."""
    from ..answers import match_option

    box = root.locator(css_id(dom))
    options = root.locator(f"[id^='react-select-{dom}-option-']")
    for attempt in (1, 2):  # right after the page loads, a dropdown can ignore the first try (Lyft's phone country)
        box.click()
        if search:
            box.fill("")
            box.type(search, delay=40)
        try:
            options.first.wait_for(state="visible", timeout=min(3000, timeout_ms) if attempt == 1 else timeout_ms)
            break
        except Exception:
            _keyboard(root).press("Escape")
            if attempt == 2:
                return False, "no options appeared"
            root.wait_for_timeout(800)
    texts = [t.strip() for t in options.all_inner_texts()]
    if score is None:
        best, fit = match_option(wanted, texts)
    else:
        rated = sorted(((score(t), -len(t), t) for t in texts if t), reverse=True)
        best, fit = (rated[0][2], rated[0][0]) if rated else (None, 0.0)
    if best is None or fit < threshold:
        _keyboard(root).press("Escape")
        return False, f"no option matches '{wanted}' (options: {', '.join(texts[:12])})"
    options.nth(texts.index(best)).click()
    return True, best


_CHOICE_INPUTS = "input[type='checkbox'], input[type='radio']"


def is_choice_group(target: Any) -> bool:
    """A question the form shows as a group of checkboxes or radio buttons: Greenhouse's new forms render "Select
    all that apply" questions as <fieldset id="question_N[]"> with one checkbox per option (Databricks, Twitch,
    Cloudflare, 2026-11-18)."""
    return target.locator(_CHOICE_INPUTS).count() > 0


def check_options(root: Any, dom: str, wanted: list[str]) -> tuple[bool, str]:
    """Tick the box (or radio button) of each wanted option in a choice group, matched on its label."""
    from ..answers import match_option

    labels = root.locator(css_id(dom)).locator("label")
    texts = [t.strip() for t in labels.all_inner_texts()]
    chosen: list[str] = []
    for pick in wanted:
        best, score = match_option(pick, texts)
        if best is None or score < 0.8:
            return False, f"no option matches '{pick}' (options: {', '.join(texts[:12])})"
        label = labels.nth(texts.index(best))
        box_id = label.get_attribute("for")
        box = root.locator(f"[id='{box_id}']") if box_id else label.locator(_CHOICE_INPUTS)
        if not box.is_checked():
            label.click()
        if not box.is_checked():  # the input hides behind a drawn box; a label click can miss it
            box.check(force=True)
        chosen.append(best)
    return True, "; ".join(chosen)


# A follow-up that only appears after a certain answer ("If you selected other, please specify"; SharkNinja): when it
# isn't shown, it isn't needed, even though the form marks it required.
FOLLOW_UP_RE = re.compile(r"(?i)^\W*if (you (selected|answered|chose|checked)|yes|no|other|so|applicable)\b")


def fill_form(page: Any, answers: list[Any], resume_pdf: Path, *, country: Optional[str], city: Optional[str],
              pause: Callable[[], None]) -> list[FieldResult]:
    """Fill every planned answer in `page` (a Page or the Frame find_form returned). Never touches Submit."""
    results: list[FieldResult] = []
    if country and page.locator("#country").count():
        try:  # a slow phone-country box crashed the whole fill and closed the window (Airbnb, 2026-11-18)
            ok, chosen = choose_option(page, "country", country, search=country[:9])
        except Exception as exc:
            ok, chosen = False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:100]}"
        results.append(FieldResult("country", "Country (phone)", ok, chosen if ok else None, "" if ok else chosen))
        pause()
    for a in answers:
        if a.kind in ("skip", "unknown") or (a.value is None and a.type != "file"):
            continue
        dom = dom_id(a.field)
        target = page.locator(css_id(dom))
        if not target.count():  # optional fields can be hidden (Race shows only after some Hispanic/Latino answers)
            hidden_ok = not a.required or bool(FOLLOW_UP_RE.match(a.label))  # "If you selected other, please specify"
            results.append(FieldResult(a.field, a.label, hidden_ok, None,
                                       "not shown on this form" if hidden_ok else "field not found on the page"))
            continue
        try:
            if a.type == "file":
                upload = Path(a.value) if a.key in ("transcript", "cover_letter") and a.value else resume_pdf
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
            elif is_choice_group(target):
                picks = [v.strip() for v in a.value.split(";") if v.strip()] if a.type == "multi_select" else [a.value]
                ok, chosen = check_options(page, dom, picks)
                results.append(FieldResult(a.field, a.label, ok, chosen if ok else a.value, "" if ok else chosen))
            else:
                target.fill(a.value)
                results.append(FieldResult(a.field, a.label, True, a.value))
        except Exception as exc:  # a field that won't take input is reported, not fatal
            results.append(FieldResult(a.field, a.label, False, a.value, f"{type(exc).__name__}: {str(exc)[:120]}"))
        pause()
    return results


_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")


def fill_employment(root: Any, work: Union[list[Any], Callable[[], list[Any]]],
                    pause: Callable[[], None]) -> list[FieldResult]:
    """The form's Employment section: one entry per job on the tailored resume (company, title, start and end month
    and year, current role), the same entries Workday's My Experience page gets. "Add another" opens each next entry.
    Forms without the section are left alone. `work` can be a function, called only when the section is there."""
    if not root.locator("#company-name-0").count():
        return []
    try:
        work = work() if callable(work) else work
    except HelperError as exc:
        return [FieldResult("employment-0", "Employment", False, None, f"couldn't list the resume's jobs: {exc}")]
    if not work:
        return []
    section = root.locator("#company-name-0").locator("xpath=ancestor::*[.//button[normalize-space()='Add another']][1]")
    results: list[FieldResult] = []
    for i, job in enumerate(work):
        field, label = f"employment-{i}", f"Employment {i + 1}: {job.title}, {job.company}"
        if not root.locator(f"#company-name-{i}").count():
            add = section.locator("button", has_text="Add another")
            if not add.count():
                results.append(FieldResult(field, label, False, None, "the form has no 'Add another' for more jobs"))
                break
            add.last.click()
            try:
                root.locator(f"#company-name-{i}").wait_for(timeout=5000)
            except Exception:
                results.append(FieldResult(field, label, False, None, "the next employment entry didn't appear"))
                break
        problems: list[str] = []
        try:
            root.locator(f"#company-name-{i}").fill(job.company)
            if root.locator(f"#title-{i}").count():
                root.locator(f"#title-{i}").fill(job.title)
            for which, when in (("start", job.start), ("end", None if job.current else job.end)):
                if when is None:
                    continue
                if root.locator(f"#{which}-date-month-{i}").count():
                    ok, chosen = choose_option(root, f"{which}-date-month-{i}", _MONTHS[when.month - 1])
                    if not ok:
                        problems.append(f"{which} month: {chosen}")
                if root.locator(f"#{which}-date-year-{i}").count():
                    root.locator(f"#{which}-date-year-{i}").fill(str(when.year))
            if job.current and root.locator(f"#current-role-{i}_1").count():
                if not common.tick(root.locator(f"#current-role-{i}_1")):
                    problems.append("couldn't tick Current role")
        except Exception as exc:
            problems.append(f"{type(exc).__name__}: {str(exc)[:120]}")
        span = " to ".join(w.display() for w in (job.start, None if job.current else job.end) if w) + (
            " to present" if job.current else "")
        results.append(FieldResult(field, label, not problems, span, "; ".join(problems)))
        pause()
    for i, job in enumerate(work[:len(results)]):  # text typed while the form was still loading can be wiped
        for box_id, text in ((f"company-name-{i}", job.company), (f"title-{i}", job.title)):
            box = root.locator(f"#{box_id}")
            if box.count() and box.input_value() != text:
                box.fill(text)
                if box.input_value() != text and results[i].ok:
                    results[i] = FieldResult(results[i].field, results[i].label, False, results[i].value,
                                             f"{box_id} didn't keep its text")
    return results


def fill_education(root: Any, schools: Union[list[Any], Callable[[], list[Any]]],
                   pause: Callable[[], None]) -> list[FieldResult]:
    """The form's Education section (the job's data says "education_required" or "education_optional"): one entry per
    school in the profile, with school, degree, discipline and start and end month and year. Lucid's five forms had a
    required one (2026-11-19); helper left it empty and every submit stopped there. Forms without it are left alone."""
    from .workday_page import degree_score, school_search_terms, study_score, study_search_terms
    from ..answers import match_option

    if not root.locator("#school--0").count():
        return []
    try:
        schools = schools() if callable(schools) else schools
    except HelperError as exc:
        return [FieldResult("education-0", "Education", False, None, f"couldn't list your schools: {exc}")]
    section = root.locator("#school--0").locator("xpath=ancestor::*[.//button[normalize-space()='Add another']][1]")
    results: list[FieldResult] = []
    for i, s in enumerate(schools):
        field, label = f"education-{i}", f"Education {i + 1}: {s.degree}, {s.school}"
        if not root.locator(f"#school--{i}").count():
            add = section.locator("button", has_text="Add another")
            if not add.count():
                break  # the form takes one school: the first (current) one is in
            add.last.click()
            try:
                root.locator(f"#school--{i}").wait_for(timeout=5000)
            except Exception:
                results.append(FieldResult(field, label, False, None, "the next education entry didn't appear"))
                break
        problems: list[str] = []
        try:
            ok, chosen = False, "not tried"
            for term in school_search_terms(s.school):
                ok, chosen = choose_option(root, f"school--{i}", s.school, search=term, timeout_ms=8000,
                                           score=lambda t: match_option(s.school, [t])[1], threshold=0.85)
                if ok:
                    break
            if not ok:
                problems.append(f"school: {chosen}")
            if root.locator(f"#degree--{i}").count():
                ok, chosen = choose_option(root, f"degree--{i}", s.degree, score=lambda t: degree_score(s.degree, t),
                                           threshold=0.85)
                if not ok:
                    problems.append(f"degree: {chosen}")
            if s.field_of_study and root.locator(f"#discipline--{i}").count():
                # the field's own name, else its broader field: Greenhouse's list has no "Computer Engineering", only
                # "Engineering" (Lucid, 2026-11-19), which is still true of his Bachelor of Engineering
                head = s.field_of_study.split()[-1].lower()

                def fits(t: str) -> float:
                    return max(study_score(s.field_of_study, t), 0.86 if t.strip().lower() == head else 0.0)
                for term in study_search_terms(s.field_of_study):
                    ok, chosen = choose_option(root, f"discipline--{i}", s.field_of_study, search=term, score=fits,
                                               threshold=0.85)
                    if ok:
                        break
                if not ok:
                    problems.append(f"discipline: {chosen}")
            for which, when in (("start", s.start), ("end", s.end)):
                if when is None:
                    continue
                if root.locator(f"#{which}-month--{i}").count():
                    ok, chosen = choose_option(root, f"{which}-month--{i}", _MONTHS[when.month - 1])
                    if not ok:
                        problems.append(f"{which} month: {chosen}")
                if root.locator(f"#{which}-year--{i}").count():
                    root.locator(f"#{which}-year--{i}").fill(str(when.year))
        except Exception as exc:
            problems.append(f"{type(exc).__name__}: {str(exc)[:120]}")
        span = " to ".join(w.display() for w in (s.start, s.end) if w)
        results.append(FieldResult(field, label, not problems, span, "; ".join(problems)))
        pause()
    return results


def is_confirmation(page: Any, form: Optional[Any] = None) -> bool:
    return common.is_confirmation(page, form, hosts=("greenhouse.io",))


SUBMIT_BUTTONS = ("button[type=submit]:has-text('Submit')", "#submit_app", "input[type=submit][value*='Submit' i]")


def submit(form: Any) -> bool:
    """Auto mode only: click Submit once."""
    return common.click_submit(form, SUBMIT_BUTTONS)


def submit_buttons(form: Any) -> list[Any]:
    """The Submit button while the form is still there, so a submit the form's own checks stopped counts as not sent."""
    return common.visible_buttons(form, SUBMIT_BUTTONS)


def is_submit_request(url: str, method: str) -> bool:
    return method == "POST" and bool(re.search(r"greenhouse\.io/.*(?:applications|application_submit|/apply)", url))
