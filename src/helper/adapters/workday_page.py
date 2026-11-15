"""Reading and filling the pages of Workday's apply flow.

Every Workday question sits in a [data-automation-id="formField-<id>"] block, and required ones carry
abbr[title=required]. A block holds one of these controls:
- a text box or a text area;
- a dropdown: a button that opens a list (ul[role=listbox] > li[role=option]);
- a search box ("prompt"): type, press Enter, then pick from the results. A single result is picked by Workday itself;
- radio buttons or checkboxes, each with its own label;
- a date made of month/day/year boxes; you click the visible box and type digits;
- a file upload.
My Experience repeats blocks per entry, with element ids such as workExperience-4--jobTitle.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..answers import match_option
from ..dates import MonthYear, parse_month_year
from ..models import FormQuestion
from .common import FieldResult

NEXT_BUTTON = "[data-automation-id=pageFooterNextButton], [data-automation-id=bottom-navigation-next-button]"
LISTBOX_OPTIONS = "ul[role=listbox]:not([data-automation-id=selectedItemList]) li[role=option]"
PROMPT_OPTIONS = "[data-automation-id=activeListContainer] [role=option]"
SELECTED_ITEMS = "[data-automation-id=selectedItemList] [data-automation-id=promptOption]"
SPECIAL_SECTIONS = re.compile(r"^(Work-Experience|Education|Certifications|Skills|Languages|Resume/CV|Websites)-")
DATE_SECTIONS = ("Month", "Day", "Year")

# One entry per visible question block: {key, label, type, required, options, section, current, selected, dates}.
DISCOVER_JS = r"""
() => {
  const text = el => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  const optionLabel = i => text(i.labels && i.labels[0]) || i.getAttribute('aria-label') || i.value || '';
  const root = document.querySelector('[data-automation-id=applyFlowPage]') || document.body;
  const out = [], seen = {};
  for (const f of root.querySelectorAll('[data-automation-id^="formField-"]')) {
    if (!visible(f) || f.parentElement.closest('[data-automation-id^="formField-"]')) continue;
    const group = f.closest('[role=group][aria-labelledby]');
    const section = group ? group.getAttribute('aria-labelledby') : '';
    let id = f.getAttribute('data-automation-id').slice('formField-'.length) || f.getAttribute('data-fkit-id') || 'field';
    seen[id] = (seen[id] || 0) + 1;
    if (seen[id] > 1) id += '-' + seen[id];
    const labelEl = f.querySelector('legend') || f.querySelector('label');
    let label = text(labelEl);
    const required = !!f.querySelector('abbr[title=required], .requiredAsterisk') || /\*\s*$/.test(label);
    label = label.replace(/\s*\*+\s*$/, '').trim();
    let type = null, options = [];
    const boxes = [...f.querySelectorAll('input[type=checkbox]')];
    if (f.querySelector('input[type=file]')) type = 'file';
    else if (f.querySelector('button[aria-haspopup=listbox]')) type = 'select';
    else if (f.querySelector('[data-automation-id=multiselectInputContainer], [data-uxi-widget-type=selectinput]')) type = 'prompt';
    else if (f.querySelector('input[type=radio]')) { type = 'radio'; options = [...f.querySelectorAll('input[type=radio]')].map(optionLabel); }
    else if (boxes.length > 1) { type = 'multi_select'; options = boxes.map(optionLabel); }
    else if (boxes.length === 1) { type = 'boolean'; options = [optionLabel(boxes[0]) || label]; }
    else if (f.querySelector('textarea')) type = 'textarea';
    else if (f.querySelector('[data-automation-id=dateInputWrapper]')) type = 'date';
    else if (f.querySelector('input[type=number], input[inputmode=numeric], input[inputmode=decimal], ' +
                             '[data-automation-id*=umeric]')) type = 'number';
    else if (f.querySelector('input:not([type=hidden])')) type = 'text';
    if (!type || !label) continue;
    const key = 'workday:' + id;
    f.setAttribute('data-helper-key', key);
    const input = f.querySelector('input:not([type=checkbox]):not([type=radio]):not([type=file]):not([type=hidden]), textarea');
    out.push({
      key, label, type, required, options, section,
      current: type === 'select' ? text(f.querySelector('button[aria-haspopup=listbox]')) : (input ? input.value || '' : ''),
      selected: [...f.querySelectorAll('[data-automation-id=selectedItemList] [data-automation-id=promptOption]')]
                  .map(p => p.getAttribute('data-automation-label') || text(p)),
      dates: ['Month', 'Day', 'Year'].filter(s => f.querySelector('[data-automation-id=dateSection' + s + '-display]')),
    });
  }
  return out;
}
"""


@dataclass
class Found:
    """What discovery saw in a block besides the question itself."""

    section: str = ""
    current: str = ""
    selected: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)


def block(page: Any, key: str) -> Any:
    return page.locator(f"[data-helper-key={json.dumps(key)}]")


# --------------------------------------------------------------------------- where the flow is


def current_step(page: Any) -> Optional[str]:
    """The name of the active step ('My Information', 'Review', 'Create Account/Sign In'), or None outside the flow."""
    bar = page.locator("[data-automation-id=progressBarActiveStep]")
    try:
        if not bar.count():
            return None
        raw = " ".join(bar.first.inner_text(timeout=2000).split())
    except Exception:
        return None
    name = re.sub(r"(?i)^current step \d+ of \d+\s*", "", raw).strip()
    return name or raw


def step_kind(step: Optional[str]) -> str:
    s = (step or "").lower()
    if re.search(r"create account|sign in", s):
        return "account"
    if re.match(r"review\b", s):
        return "review"
    if "my experience" in s:
        return "experience"
    if "my information" in s:
        return "info"
    if re.search(r"self[- ]?identif|disabilit", s):
        return "self_identify"
    if re.search(r"voluntary|disclosure", s):
        return "disclosures"
    return "questions"


def next_is_submit(page: Any) -> bool:
    button = page.locator(NEXT_BUTTON)
    try:
        return bool(button.count()) and bool(re.search(r"submit", button.first.inner_text(timeout=2000), re.I))
    except Exception:
        return True  # unsure: treat it as Submit, so it is never clicked


def click_next(page: Any) -> None:
    """Go to the next page. Refuses whenever the button is Submit or the page is Review (review mode, rule 10)."""
    if next_is_submit(page) or step_kind(current_step(page)) == "review":
        raise RuntimeError("refusing to click Submit: Vatsal submits")
    page.locator(NEXT_BUTTON).first.click()


def errors_shown(page: Any) -> list[str]:
    texts = []
    for selector in ("[data-automation-id=inputAlert]", "[data-automation-id=errorMessage]", "[data-automation-id=errorHeading]"):
        try:
            texts += [" ".join(t.split()) for t in page.locator(selector).all_inner_texts() if t.strip()]
        except Exception:
            continue
    return list(dict.fromkeys(texts))


SUBMIT_WATCH_JS = r"""
() => {
  if (window.__helperSubmitWatch) return;
  window.__helperSubmitWatch = true;
  document.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('[data-automation-id=pageFooterNextButton], [data-automation-id=bottom-navigation-next-button]');
    if (b && /submit/i.test(b.innerText || '')) window.__helperSubmitClicked = true;
  }, true);
}
"""


def install_submit_watch(page: Any) -> None:
    try:
        page.evaluate(SUBMIT_WATCH_JS)
    except Exception:
        pass


def submit_clicked(page: Any) -> bool:
    try:
        return bool(page.evaluate("() => window.__helperSubmitClicked === true"))
    except Exception:
        return False


# --------------------------------------------------------------------------- reading questions


def discover(page: Any, *, skip_special: bool = False, section: str = "questions") -> tuple[list[FormQuestion], dict[str, Found]]:
    """The page's questions, with dropdown choices read by opening each dropdown."""
    questions, found = [], {}
    for f in page.evaluate(DISCOVER_JS):
        if skip_special and SPECIAL_SECTIONS.match(f["section"] or ""):
            continue
        options = f["options"]
        if f["type"] == "select":
            options = dropdown_options(page, block(page, f["key"]))
            if not options:  # the page wasn't ready for the click yet
                page.wait_for_timeout(1000)
                options = dropdown_options(page, block(page, f["key"]))
        sect = section
        if re.search(r"(?i)^(please select your )?(gender|race|ethnicity|veteran|disabilit|hispanic)", f["label"]):
            sect = "eeoc"
        questions.append(FormQuestion(label=f["label"], field_name=f["key"], type=f["type"], required=f["required"],
                                      options=options, section=sect))
        found[f["key"]] = Found(section=f["section"] or "", current=f["current"], selected=f["selected"], dates=f["dates"])
    return questions, found


def dropdown_options(page: Any, where: Any) -> list[str]:
    try:
        where.locator("button[aria-haspopup=listbox]").first.click()
        page.locator(LISTBOX_OPTIONS).first.wait_for(state="visible", timeout=4000)
        texts = [" ".join(t.split()) for t in page.locator(LISTBOX_OPTIONS).all_inner_texts()]
    except Exception:
        texts = []
    finally:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    return [t for t in texts if t and not re.match(r"(?i)^select one$", t)]


# --------------------------------------------------------------------------- filling one control


def choose_dropdown(page: Any, where: Any, value: str) -> tuple[bool, str, str]:
    button = where.locator("button[aria-haspopup=listbox]").first
    current = " ".join(button.inner_text().split())
    if current and match_option(value, [current])[1] >= 0.95:
        return True, current, "already set"
    options = page.locator(LISTBOX_OPTIONS)
    for attempt in range(2):
        button.click()
        try:
            options.first.wait_for(state="visible", timeout=4000)
            break
        except Exception:
            if attempt:
                return False, value, "the list didn't open"
            page.wait_for_timeout(1000)
    texts = [" ".join(t.split()) for t in options.all_inner_texts()]
    best, score = match_option(value, texts)
    if best is None or score < 0.8:
        page.keyboard.press("Escape")
        return False, value, f"no choice matches '{value}'"
    options.nth(texts.index(best)).click()
    page.wait_for_timeout(300)
    return True, best, ""


def selected_items(where: Any) -> list[str]:
    """What a search box holds, by each chip's label (its text can still be empty right after a pick)."""
    try:
        labels = where.locator(SELECTED_ITEMS).evaluate_all(
            "els => els.map(e => e.getAttribute('data-automation-label') || e.innerText || '')")
        return [" ".join(t.split()) for t in labels if t and t.strip()]
    except Exception:
        return []


def choose_prompt(page: Any, where: Any, terms: list[str], accept: Callable[[str], float], *,
                  threshold: float = 0.9, keep_existing: bool = True) -> tuple[bool, str, str]:
    """Search a prompt box for each term in turn and pick the first result `accept` scores at `threshold` or more.
    Workday picks a lone result by itself; a lone result that isn't acceptable is removed again."""
    existing = selected_items(where)
    if existing and (keep_existing or any(accept(e) >= threshold for e in existing)):
        return True, "; ".join(existing), "already chosen"
    box = where.locator("input:not([type=hidden])").first
    for term in [t for t in dict.fromkeys(terms) if t]:
        box.click()
        box.fill("")
        box.type(term, delay=25)
        shown_before = page.locator(PROMPT_OPTIONS).all_inner_texts()
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        last: Optional[list[str]] = None
        for _ in range(12):  # until the results replace the earlier list and stop changing (about 4 seconds at most)
            if selected_items(where) != existing:
                break
            results = page.locator(PROMPT_OPTIONS).all_inner_texts()
            if results and results != shown_before and results == last:
                break
            last = results
            page.wait_for_timeout(300)
        now = selected_items(where)
        added = [s for s in now if s not in existing]
        if added:
            if accept(added[-1]) >= threshold:
                _close_popup(page)
                return True, added[-1], ""
            _remove_last_selection(where)
            existing = selected_items(where)
            continue
        options = page.locator(PROMPT_OPTIONS)
        texts = [" ".join(t.split()) for t in options.all_inner_texts()]
        scored = sorted(((accept(t), i) for i, t in enumerate(texts) if t), key=lambda x: (-x[0], x[1]))
        if scored and scored[0][0] >= threshold:
            options.nth(scored[0][1]).click()
            page.wait_for_timeout(800)
            _close_popup(page)
            chosen = [s for s in selected_items(where) if s not in existing]
            return True, chosen[-1] if chosen else texts[scored[0][1]], ""
    box.fill("")
    _close_popup(page)
    return False, terms[0] if terms else "", f"no search result matches '{terms[0] if terms else ''}'"


def _close_popup(page: Any) -> None:
    if page.locator("[data-automation-id=activeListContainer]").count():
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)


def _remove_last_selection(where: Any) -> None:
    try:
        where.locator("[data-automation-id=selectedItemList] [data-automation-id=DELETE_charm]").last.click()
    except Exception:
        pass


def date_digits(value: str, sections: list[str]) -> dict[str, str]:
    """'05/01/2027', 'May 1, 2027', 'May 2027' or '2027' as the digits each date box takes."""
    month = day = year = None
    if m := re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", value):
        month, day, year = int(m[1]), int(m[2]), int(m[3])
    elif m := re.match(r"^\s*(\d{1,2})/(\d{4})\s*$", value):
        month, year = int(m[1]), int(m[2])
    elif m := re.match(r"^\s*([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})\s*$", value):
        my = parse_month_year(f"{m[1]} {m[3]}")
        if my:
            month, day, year = my.month, int(m[2]), my.year
    elif my := parse_month_year(value):
        month, year = my.month, my.year
    elif m := re.match(r"^\s*(\d{4})\s*$", value):
        year = int(m[1])
    out = {}
    if "Month" in sections and month:
        out["Month"] = f"{month:02d}"
    if "Day" in sections:
        out["Day"] = f"{day or 1:02d}"
    if "Year" in sections and year:
        out["Year"] = f"{year:04d}"
    return out


def type_date(page: Any, where: Any, digits: dict[str, str]) -> str:
    for section in DATE_SECTIONS:
        if section in digits:
            where.locator(f"[data-automation-id=dateSection{section}-display]").first.click()
            page.keyboard.type(digits[section], delay=40)
    return "/".join(digits[s] for s in DATE_SECTIONS if s in digits)


def _labels(where: Any, kind: str) -> list[tuple[Any, str]]:
    inputs = where.locator(f"input[type={kind}]")
    out = []
    for i in range(inputs.count()):
        el = inputs.nth(i)
        label = el.evaluate("i => ((i.labels && i.labels[0] && i.labels[0].innerText) || i.getAttribute('aria-label') "
                            "|| i.value || '').replace(/\\s+/g, ' ').trim()")
        out.append((el, label))
    return out


def _tick(el: Any) -> bool:
    """Tick a radio button or checkbox. Workday updates the box a moment after the click, so wait for it, and
    click the option's label when the box itself doesn't take the click."""
    if el.is_checked():
        return True
    page = el.page
    for target in ("input", "label"):
        try:
            if target == "input":
                el.click(force=True)
            else:
                el.evaluate("i => { const l = i.labels && i.labels[0]; if (l) l.click(); }")
        except Exception:
            continue
        for _ in range(10):
            if el.is_checked():
                return True
            page.wait_for_timeout(200)
    return el.is_checked()


def check_choices(where: Any, kind: str, wanted: list[str]) -> tuple[bool, str, str]:
    boxes = _labels(where, kind)
    labels = [label for _, label in boxes]
    chosen = []
    for want in wanted:
        best, score = match_option(want, labels)
        if best is None or score < 0.8:
            return False, "; ".join(wanted), f"no choice matches '{want}'"
        if not _tick(boxes[labels.index(best)][0]):
            return False, "; ".join(wanted), f"Workday didn't keep the tick on '{best}'"
        chosen.append(best)
    unticked = [label for (el, label) in boxes if label in chosen and not el.is_checked()]
    if unticked:  # ticking one cleared another: the group takes a single answer
        return False, "; ".join(chosen), f"only one answer stays ticked here; check {', '.join(unticked)}"
    return True, "; ".join(chosen), ""


def upload(page: Any, where: Any, pdf: Path, *, replace: bool = True) -> tuple[bool, str, str]:
    """Upload the resume. Files left from an earlier application are removed first, so only this one is sent."""
    if replace:
        for _ in range(5):
            old = where.locator("[data-automation-id=delete-file]")
            if not old.count():
                break
            old.first.click()
            page.wait_for_timeout(800)
    where.locator("input[type=file]").first.set_input_files(str(pdf))
    try:
        where.locator("[data-automation-id=file-upload-successful]").first.wait_for(state="attached", timeout=30000)
    except Exception:
        return False, pdf.name, "the upload didn't finish"
    return True, pdf.name, ""


# --------------------------------------------------------------------------- My Experience


@dataclass
class WorkEntry:
    title: str
    company: str
    location: Optional[str]
    start: Optional[MonthYear]
    end: Optional[MonthYear]
    current: bool
    description: str


@dataclass
class SchoolEntry:
    school: str
    degree: str
    field_of_study: str
    start: Optional[MonthYear]
    end: Optional[MonthYear]


def entry_ids(page: Any, kind: str) -> list[str]:
    """Entry numbers in page order, e.g. ['4', '48'] for workExperience-4--... and workExperience-48--...."""
    ids = page.evaluate(f"""() => [...document.querySelectorAll("[id^='{kind}-'][id*='--']")]
        .map(e => (e.id.match(/^{kind}-(\\d+)--/) || [])[1]).filter(Boolean)""")
    return list(dict.fromkeys(ids))


def _by_id(page: Any, element_id: str) -> Any:
    return page.locator(f"[id={json.dumps(element_id)}]")


def _field_of(page: Any, element_id: str) -> Any:
    return page.locator(f"[data-automation-id^='formField-']:has([id={json.dumps(element_id)}])").first


def _value(page: Any, element_id: str) -> str:
    el = _by_id(page, element_id)
    try:
        return el.first.input_value() if el.count() else ""
    except Exception:
        return ""


def _add_entry(page: Any, section: str, kind: str) -> Optional[str]:
    before = set(entry_ids(page, kind))
    group = page.locator(f"[role=group][aria-labelledby={json.dumps(section)}]")
    button = group.locator("button[data-automation-id=add-button]")
    if not button.count():
        return None
    button.first.click()
    page.wait_for_timeout(1200)
    new = [i for i in entry_ids(page, kind) if i not in before]
    return new[0] if new else None


def _slot(page: Any, section: str, kind: str, key_field: str, wanted: str, used: set[str]) -> Optional[str]:
    """The entry to fill: one already holding this item, else an empty one, else a new one."""
    ids = [i for i in entry_ids(page, kind) if i not in used]
    for i in ids:
        if match_option(wanted, [_value(page, f"{kind}-{i}--{key_field}")])[1] >= 0.9:
            return i
    for i in ids:
        if not _value(page, f"{kind}-{i}--{key_field}"):
            return i
    return _add_entry(page, section, kind)


def _date_in(page: Any, base_id: str, my: Optional[MonthYear]) -> None:
    if my is None:
        return
    sections = [s for s in DATE_SECTIONS if _by_id(page, f"{base_id}-dateSection{s}-display").count()]
    digits = {"Month": f"{my.month:02d}", "Day": "01", "Year": f"{my.year:04d}"}
    for s in sections:
        _by_id(page, f"{base_id}-dateSection{s}-display").first.click()
        page.keyboard.type(digits[s], delay=40)


def fill_work(page: Any, entries: list[WorkEntry], pause: Callable[[], None]) -> list[FieldResult]:
    results = []
    used: set[str] = set()
    for n, e in enumerate(entries, 1):
        label = f"Work experience {n}: {e.title}, {e.company}"
        try:
            i = _slot(page, "Work-Experience-section", "workExperience", "companyName", e.company, used)
            if i is None:
                results.append(FieldResult(f"workday:work:{n}", label, False, None, "no room to add it"))
                continue
            used.add(i)
            base = f"workExperience-{i}--"
            _by_id(page, base + "jobTitle").first.fill(e.title)
            _by_id(page, base + "companyName").first.fill(e.company)
            if e.location and _by_id(page, base + "location").count():
                _by_id(page, base + "location").first.fill(e.location)
            current = _by_id(page, base + "currentlyWorkHere")
            if current.count() and current.first.is_checked() != e.current:
                current.first.check(force=True) if e.current else current.first.uncheck(force=True)
                page.wait_for_timeout(400)
            _date_in(page, base + "startDate", e.start)
            if not e.current:
                _date_in(page, base + "endDate", e.end)
            if e.description and _by_id(page, base + "roleDescription").count():
                _by_id(page, base + "roleDescription").first.fill(e.description)
            results.append(FieldResult(f"workday:work:{n}", label, True, f"{_span(e.start)} to "
                                       f"{'present' if e.current else _span(e.end)}"))
        except Exception as exc:
            results.append(FieldResult(f"workday:work:{n}", label, False, None, f"{type(exc).__name__}: {str(exc)[:120]}"))
        pause()
    return results


def _span(my: Optional[MonthYear]) -> str:
    return f"{my.month:02d}/{my.year}" if my else "?"


DEGREE_ALIASES = {
    "master": ("masters", "master's", "master's degree", "master", "ms", "m s", "msc", "graduate", "master of science"),
    "bachelor": ("bachelors", "bachelor's", "bachelor's degree", "bachelor", "be", "b e", "bs", "b s", "btech",
                 "b tech", "bachelor of engineering", "undergraduate"),
}


def degree_aliases(degree: str) -> tuple[str, ...]:
    d = degree.lower()
    return DEGREE_ALIASES["master"] if d.startswith("master") else DEGREE_ALIASES["bachelor"] if d.startswith("bachelor") else ()


def fill_education(page: Any, schools: list[SchoolEntry], pause: Callable[[], None]) -> list[FieldResult]:
    results = []
    used: set[str] = set()
    for n, s in enumerate(schools, 1):
        label = f"Education {n}: {s.degree}, {s.school}"
        try:
            i = _slot(page, "Education-section", "education", "schoolName", s.school, used)
            if i is None:
                results.append(FieldResult(f"workday:school:{n}", label, False, None, "no room to add it"))
                continue
            used.add(i)
            base = f"education-{i}--"
            notes = []
            school = _field_of(page, base + "schoolName")
            if school.locator("[data-automation-id=multiselectInputContainer]").count():
                ok, shown, why = choose_prompt(page, school, [s.school], lambda t: match_option(s.school, [t])[1],
                                               threshold=0.85, keep_existing=False)
                if not ok:
                    notes.append(f"school: {why}")
            elif _by_id(page, base + "schoolName").count():
                _by_id(page, base + "schoolName").first.fill(s.school)
            degree = _field_of(page, base + "degree")
            if degree.count() and degree.locator("button[aria-haspopup=listbox]").count():
                options = dropdown_options(page, degree)
                best, score = match_option(s.degree, options, degree_aliases(s.degree))
                if best is not None and score >= 0.85:
                    choose_dropdown(page, degree, best)
                else:
                    notes.append(f"degree: no choice matches '{s.degree}'")
            study = _field_of(page, base + "fieldOfStudy")
            if study.count() and s.field_of_study:
                ok, _, why = choose_prompt(page, study, [s.field_of_study],
                                           lambda t: max(match_option(s.field_of_study, [t])[1],
                                                         0.9 if _norm_words(s.field_of_study) <= _norm_words(t) else 0.0),
                                           threshold=0.85, keep_existing=False)
                if not ok:
                    notes.append(f"field of study: {why}")
            # Overall Result (GPA) is never filled (rule 3).
            _date_in(page, base + "firstYearAttended", s.start)
            _date_in(page, base + "lastYearAttended", s.end)
            results.append(FieldResult(f"workday:school:{n}", label, not notes, f"{_span(s.start)} to {_span(s.end)}",
                                       "; ".join(notes)))
        except Exception as exc:
            results.append(FieldResult(f"workday:school:{n}", label, False, None, f"{type(exc).__name__}: {str(exc)[:120]}"))
        pause()
    return results


def _norm_words(text: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9]+", " ", text.lower()).split()) - {"and", "of", "the", "in"}


# --------------------------------------------------------------------------- Skills and Certifications


def _skill_score(skill: str, option: str) -> float:
    """The same skill, exactly or with a qualifier ("Python (Programming Language)"); "Python Requests" is another."""
    norm = lambda t: " ".join(re.sub(r"[^a-z0-9+#]+", " ", t.lower()).split())  # noqa: E731
    if norm(option) == norm(skill):
        return 1.0
    if re.match(rf"^\s*{re.escape(skill)}\s*\(", option, re.I):
        return 0.97
    return min(match_option(skill, [option])[1], 0.85)


def fill_skills(page: Any, skills: list[str], pause: Callable[[], None]) -> list[FieldResult]:
    """Add each skill from the resume's skills lines that the company's skill list has (Vatsal, 2026-11-14)."""
    where = page.locator("[role=group][aria-labelledby='Skills-section'] [data-automation-id^='formField-']").first
    if not skills or not where.count() or not where.locator("[data-automation-id=multiselectInputContainer]").count():
        return []
    added, missing = [], []
    for skill in skills:
        if any(_skill_score(skill, s) >= 0.9 for s in selected_items(where)):
            continue
        try:
            ok, shown, _ = choose_prompt(page, where, [skill], lambda t, s=skill: _skill_score(s, t), threshold=0.9,
                                         keep_existing=False)
        except Exception:
            ok, shown = False, skill
        (added if ok else missing).append((shown or skill) if ok else skill)
        pause()
    detail = f"not in this company's list: {', '.join(missing)}" if missing else ""
    return [FieldResult("workday:skills", "Skills", True, "; ".join(added) or "none added", detail)]


def _cert_date(text: Optional[str]) -> dict[str, str]:
    if not text:
        return {}
    return date_digits(text, ["Month", "Day", "Year"])


def _delete_entry(page: Any, element_id: str) -> None:
    panel = _field_of(page, element_id).locator("xpath=ancestor::*[@role='group'][1]")
    try:
        panel.get_by_role("button", name=re.compile(r"^Delete", re.I)).first.click()
        page.wait_for_timeout(600)
    except Exception:
        pass


def fill_certifications(page: Any, certs: list[dict[str, Any]], pause: Callable[[], None]) -> list[FieldResult]:
    """Add each certificate the company's certification list has, with its ID and issue date. Workday lists are
    company-specific: a certificate the list doesn't have is left out (its empty entry is removed again)."""
    section = "Certifications-section"
    if not certs or not page.locator(f"[role=group][aria-labelledby={json.dumps(section)}]").count():
        return []
    added, missing = [], []
    for cert in certs:
        name, issuer = cert.get("name", ""), cert.get("issuer") or ""
        i = _add_entry(page, section, "certification")
        if i is None:
            missing.append(name)
            continue
        base = f"certification-{i}--"
        field = _field_of(page, base + "certification")
        try:
            if field.locator("[data-automation-id=multiselectInputContainer], [data-automation-id=searchBox]").count():
                accept = lambda t: max(match_option(name, [t])[1], match_option(f"{name} {issuer}", [t])[1])  # noqa: E731
                ok, shown, _ = choose_prompt(page, field, [name], accept, threshold=0.9, keep_existing=False)
            else:
                _by_id(page, base + "certification").first.fill(name)
                ok, shown = True, name
        except Exception:
            ok, shown = False, name
        if not ok:
            _delete_entry(page, base + "certification")
            missing.append(name)
            continue
        if cert.get("credential_id") and _by_id(page, base + "certificationNumber").count():
            _by_id(page, base + "certificationNumber").first.fill(str(cert["credential_id"]))
        digits = _cert_date(cert.get("issued"))
        for section_name in DATE_SECTIONS:
            box = _by_id(page, f"{base}issuedDate-dateSection{section_name}-display")
            if section_name in digits and box.count():
                box.first.click()
                page.keyboard.type(digits[section_name], delay=40)
        added.append(shown)
        pause()
    detail = f"not in this company's list: {', '.join(missing)}" if missing else ""
    return [FieldResult("workday:certifications", "Certifications", True, "; ".join(added) or "none added", detail)]
