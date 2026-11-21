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
from .common import FieldResult, tick

NEXT_BUTTON = ("[data-automation-id=pageFooterNextButton], [data-automation-id=bottom-navigation-next-button], "
               "[data-automation-id=bottomNavigationNextButton]")
# a flag, not an inline (?i): Playwright hands the pattern to the page as a JavaScript regex (Samsung, 2026-11-18)
NEXT_WORDS = re.compile(r"^\s*(next|continue|save and continue)\s*$", re.IGNORECASE)  # never "Submit" (rule 10)
LISTBOX_OPTIONS = "ul[role=listbox]:not([data-automation-id=selectedItemList]) li[role=option]"
PROMPT_OPTIONS = "[data-automation-id=activeListContainer] [role=option]"
SELECTED_ITEMS = "[data-automation-id=selectedItemList] [data-automation-id=promptOption]"
# Jabil (2026-11-20) titles its jobs "Work or Other Experience": its fields came to Claude as loose questions, typed
# over helper's own entry (the home address as the job's location), and Workday refused the page with "Page Error"s.
SPECIAL_SECTIONS = re.compile(r"^(Work(?:-or-Other)?-Experience|Education|Certifications|Skills|Languages|Resume/CV"
                              r"|Websites)-")
WORK_TITLE = re.compile(r"^\s*Work(?: or Other)? Experience\s*$", re.I)
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
    let label = text(labelEl), labelFrom = null;
    if (!label) {
      // an attachment question keeps an empty <label>; its words are a rich-text block the upload names with
      // aria-labelledby ("Please upload a current unofficial transcript.", The Aerospace Corporation, 2026-11-20)
      const named = [f, ...f.querySelectorAll('[aria-labelledby]')]
        .flatMap(e => (e.getAttribute('aria-labelledby') || '').split(/\s+/)).filter(Boolean)
        .map(id => document.getElementById(id)).find(e => e && text(e));
      labelFrom = f.querySelector('[data-automation-id=richText]') || named || null;
      label = text(labelFrom);
    }
    const required = !!f.querySelector('abbr[title=required], .requiredAsterisk') || /\*\s*$/.test(label)
      || !!(labelFrom && labelFrom.querySelector('abbr[title=required], .requiredAsterisk'));
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
    const entry = [...f.querySelectorAll('[id]')].some(e => /^(?:workExperience|education)-\d+--/.test(e.id));
    out.push({
      key, label, type, required, options, section, entry,
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


_WENT_WRONG = re.compile(r"something went wrong", re.IGNORECASE)  # a flag, not "(?i)": Playwright runs it as JS


def went_wrong(page: Any) -> bool:
    """Workday's own error page inside the application: "Something went wrong. Please refresh the page and then try
    again." (HPE and Marvell, reopened after a helper fix, 2026-11-18)."""
    try:
        shown = page.get_by_text(_WENT_WRONG)
        return shown.count() > 0 and shown.first.is_visible()
    except Exception:
        return False


# The posting is gone (HPE, 2026-11-19: "The page you are looking for doesn't exist." where the posting had been).
_GONE = re.compile(r"the page you are looking for (?:doesn.t|does not) exist|(?:job|posting|position)(?: posting)?"
                   r" (?:is )?no longer available|(?:job|position) (?:you are (?:trying to apply|applying) for )?has "
                   r"(?:been filled|been closed|expired)", re.IGNORECASE)


def posting_gone(page: Any) -> Optional[str]:
    """The site's words when the posting has been taken down, else None. Only asked off the application's pages."""
    try:
        shown = page.get_by_text(_GONE)
        if shown.count() and shown.first.is_visible():
            return " ".join(shown.first.inner_text().split())[:160]
    except Exception:
        pass
    return None


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


def click_next(page: Any, wait_seconds: float = 15) -> None:
    """Go to the next page. Refuses whenever the button is Submit or the page is Review (review mode, rule 10).
    Waits for a Next button that arrives late: right after signing in, HPE's My Information page had none yet and
    helper handed the page over (2026-11-18)."""
    if next_is_submit(page) or step_kind(current_step(page)) == "review":
        raise RuntimeError("refusing to click Submit: Vatsal submits")
    tries = max(1, int(wait_seconds))
    for attempt in range(tries):
        button = page.locator(NEXT_BUTTON).first
        if not button.count():  # Samsung's Workday (2026-11-18): none of the usual ids; find the button by its words
            button = page.get_by_role("button", name=NEXT_WORDS).first
        if button.count():
            break
        if attempt == tries - 1:
            raise LookupError("Workday's Next button isn't on this page")
        page.wait_for_timeout(1000)
        if next_is_submit(page) or step_kind(current_step(page)) == "review":
            raise RuntimeError("refusing to click Submit: Vatsal submits")
    button.click(timeout=15000)


SUBMIT_WORDS = re.compile(r"^\s*submit(\s+(my\s+)?application)?\s*$", re.IGNORECASE)


def _shown(buttons: Any) -> list[Any]:
    return [buttons.nth(i) for i in range(buttons.count()) if buttons.nth(i).is_visible()]


def submit_review(page: Any, wait_seconds: float = 15) -> bool:
    """Auto-submit (Vatsal, 2026-11-16): click Submit on the Review page, once. Only on the Review page, only when
    exactly one visible Submit button is there. It waits for a button that arrives late and finds one without
    Workday's usual ids by its words, as click_next does (Bloomberg Industry Group, 2026-11-18: helper reached Review
    and never clicked; nothing was sent)."""
    for attempt in range(max(1, int(wait_seconds))):
        if attempt:
            page.wait_for_timeout(1000)
        if step_kind(current_step(page)) != "review":
            continue
        visible = _shown(page.locator(NEXT_BUTTON)) or _shown(page.get_by_role("button", name=SUBMIT_WORDS))
        if len(visible) == 1 and re.search(r"submit", visible[0].inner_text(timeout=2000), re.I):
            visible[0].click()
            return True
    return False


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
        if skip_special and (SPECIAL_SECTIONS.match(f["section"] or "") or f.get("entry")):
            continue  # fill_work and fill_education fill these
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
                                      options=options, section=sect,
                                      description=(f["section"] or "").replace("-", " ") or None))
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
    seen: list[str] = []  # what the last search listed, for the note when nothing matched
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
            seen = added[-1:]  # Workday picked its lone result by itself
            _remove_last_selection(where)
            existing = selected_items(where)
            continue
        options = page.locator(PROMPT_OPTIONS)
        texts = [" ".join(t.split()) for t in options.all_inner_texts()]
        seen = [t for t in texts if t] or seen  # the note names the last list that showed anything
        scored = sorted(((accept(t), i) for i, t in enumerate(texts) if t), key=lambda x: (-x[0], x[1]))
        if scored and scored[0][0] >= threshold:
            options.nth(scored[0][1]).click()
            page.wait_for_timeout(800)
            # A category ("Job Board ›") opens its own list instead of selecting anything, and the field stayed empty
            # (Bloomberg, Samsung, 2026-11-17): pick inside it, up to two levels down, and only count a pick that stuck.
            for _ in range(2):
                if [s for s in selected_items(where) if s not in existing]:
                    break
                inner = page.locator(PROMPT_OPTIONS)
                inner_texts = [" ".join(t.split()) for t in inner.all_inner_texts()]
                ranked = sorted(((accept(t), i) for i, t in enumerate(inner_texts) if t), key=lambda x: (-x[0], x[1]))
                if not ranked or ranked[0][0] < threshold:
                    break
                inner.nth(ranked[0][1]).click()
                page.wait_for_timeout(800)
            _close_popup(page)
            chosen = [s for s in selected_items(where) if s not in existing]
            if chosen:
                return True, chosen[-1], ""
            existing = selected_items(where)  # nothing stuck: try the next term
    box.fill("")
    _close_popup(page)
    # what the list offered, so a miss can be fixed later (Cadence's Field of Study, 2026-11-19, said only this)
    shown = f" (it listed: {'; '.join(seen[:6])})" if seen else " (the search listed nothing)"
    return False, terms[0] if terms else "", f"no search result matches '{terms[0] if terms else ''}'{shown}"


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


def check_choices(where: Any, kind: str, wanted: list[str]) -> tuple[bool, str, str]:
    boxes = _labels(where, kind)
    labels = [label for _, label in boxes]
    chosen = []
    for want in wanted:
        best, score = match_option(want, labels)
        if best is None or score < 0.8:
            return False, "; ".join(wanted), f"no choice matches '{want}'"
        if not tick(boxes[labels.index(best)][0]):
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
    gpa: Optional[str] = None  # forms only (data/grades.yaml); never on a resume


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


def has_section(page: Any, section: str, kind: str, title: str | re.Pattern[str]) -> bool:
    """Whether the page asks for these entries at all. DraftKings' campus portal (2026-11-19) has no Work Experience
    or Education section on My Experience (it asks for the school among its questions), and helper listed every job
    and school as "NOT filled: no room to add it". A section that is there but has no Add button still says so."""
    if entry_ids(page, kind):
        return True
    if page.locator(f"[id={json.dumps(section)}], [role=group][aria-labelledby={json.dumps(section)}]").count():
        return True
    name = title if isinstance(title, re.Pattern) else re.compile(rf"^\s*{re.escape(title)}\s*$", re.I)
    return bool(page.get_by_role("heading", name=name).count())


SECTIONS_JS = r"""() => [...document.querySelectorAll('[role=group][aria-labelledby$="-section"]')].map(g => {
  const id = g.getAttribute('aria-labelledby'), h = document.getElementById(id);
  return {id, title: h ? (h.innerText || h.textContent || '').replace(/\s+/g, ' ').trim() : '',
          ids: [...g.querySelectorAll('[id*="--"]')].map(e => e.id)};
})"""


def section_id(page: Any, default: str, kind: str, title: re.Pattern[str]) -> str:
    """The id naming the section that holds these entries, which its Add button sits under. It follows the page's own
    title: Work-Experience-section on most sites, Work-or-Other-Experience-section at Jabil (2026-11-20)."""
    try:
        groups = page.evaluate(SECTIONS_JS)
    except Exception:
        return default
    entry = re.compile(rf"^{re.escape(kind)}-\d+--")
    holding = [g for g in groups if any(entry.match(i) for i in g["ids"])]
    if holding:
        return min(holding, key=lambda g: len(g["ids"]))["id"]  # the innermost one
    return next((g["id"] for g in groups if title.match(g["title"])), default)


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
    results: list[FieldResult] = []
    section = section_id(page, "Work-Experience-section", "workExperience", WORK_TITLE)
    if not has_section(page, section, "workExperience", WORK_TITLE):
        return results
    used: set[str] = set()
    for n, e in enumerate(entries, 1):
        label = f"Work experience {n}: {e.title}, {e.company}"
        try:
            i = _slot(page, section, "workExperience", "companyName", e.company, used)
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
    "master": ("masters", "master's", "master's degree", "master", "ms", "m s", "msc", "m sc", "graduate",
               "postgraduate", "post graduate", "master of science"),
    "bachelor": ("bachelors", "bachelor's", "bachelor's degree", "bachelor", "be", "b e", "bs", "b s", "btech",
                 "b tech", "bachelor of engineering", "undergraduate", "4 year degree", "four year degree"),
}


def degree_aliases(degree: str) -> tuple[str, ...]:
    d = degree.lower()
    return DEGREE_ALIASES["master"] if d.startswith("master") else DEGREE_ALIASES["bachelor"] if d.startswith("bachelor") else ()


_DEGREE_OF = re.compile(r"(?i)\b(?:master|bachelor)(?:['’]?s)?\s+of\s+([a-z]+)")
_OTHER_DEGREES = re.compile(r"(?i)\b(?:MBA|MFA|LLM|LLB|JD|MD|MPH|MEd|BBA)\b|\bof (?:business administration|laws?|fine "
                            r"arts|public health|education|medicine)\b")
_DEGREE_ABBREVIATIONS = {"science": ("msc", "ms", "bsc", "bs"), "engineering": ("beng", "meng", "be", "btech", "mtech")}


def degree_score(degree: str, option: str) -> float:
    """How well a Workday degree choice fits. A choice naming another degree never wins: on Visa, "Master of Business
    Administration - MBA (ISCED 7)" tied "Master's or Equivalent (ISCED 7: MA, MSc, ...)" for "Master of Science" and
    won as the shorter one (2026-11-16)."""
    score = match_option(degree, [option], degree_aliases(degree))[1]
    # a note in brackets isn't part of the name: HPE's "Masters (±18 years of education)" scored too low and the
    # required Degree stayed empty (2026-11-18)
    core = " ".join(re.sub(r"\([^()]*\)?", " ", option).split())
    if core and core != option:
        score = max(score, match_option(degree, [core], degree_aliases(degree))[1])
    # "(ISCED 7: MA, MSc, MEng, MBA, LLM, et cetera)" is a list of examples, not the choice's own name
    examples = " ".join(re.findall(r"\(([^()]*,[^()]*)\)?", option))
    name = re.sub(r"\([^()]*,[^()]*\)?", " ", option)
    # the degree's own name, without notes in brackets: "(±18 years of education)" isn't a Master of Education
    mine, theirs = _DEGREE_OF.search(degree), _DEGREE_OF.search(core or name)
    if _OTHER_DEGREES.search(core or name) and not _OTHER_DEGREES.search(degree):
        return min(score, 0.5)  # a professional degree (MBA, law, medicine) is never his
    if theirs and mine and theirs.group(1).lower() != mine.group(1).lower():
        # another field of the same level: only when nothing closer is offered (KLA has no Master of Science, so
        # "Masters of Technology (Masters)"; 2026-11-16)
        return min(score, 0.86)
    words = set(re.findall(r"[a-z]+", f"{name} {examples}".lower()))
    level = "master" if degree.lower().startswith("master") else "bachelor"
    abbreviations = _DEGREE_ABBREVIATIONS.get(mine.group(1).lower(), ()) if mine else ()
    if score >= 0.85 and any(a in words and a[0] == level[0] for a in abbreviations):
        score = max(score, 0.93)  # "(ISCED 7: MA, MSc, MEng ...)" lists his degree
    return score


DEBUG_DIR: Optional[Path] = None  # set per run by apply.py: where field markup goes when something won't fill
SCHOOL_KEYS = ("schoolName", "school")
_SCHOOL_FILLER = {"university", "of", "the", "college", "institute", "school", "state", "and", "at", "technology"}


def school_search_terms(school: str) -> list[str]:
    """Searches to try, shortest first: 'Northeastern' then 'Northeastern University' (Vatsal, 2026-11-16: typing
    'northeastern' and Enter brings up 'Northeastern University' on Visa)."""
    words = [w for w in re.findall(r"[A-Za-z]+", school) if w.lower() not in _SCHOOL_FILLER]
    short = max(words, key=len) if words else ""
    return [t for t in dict.fromkeys([short, school]) if t]


_STUDY_FILLER = {"and", "of", "the", "in", "for", "with"}
_GENERIC_STUDY = {"engineering", "science", "sciences", "studies", "technology", "technologies", "general", "applied",
                  "arts"}


def study_search_terms(field: str) -> list[str]:
    """The field of study as written, then each of its words, distinctive ones first (longest first among them).
    'Computer' lists every computing field to choose from (Cadence, 2026-11-19, found nothing for 'Computer
    Science'), while 'Engineering' lists engineering from Aerospace on, and Workday shows only the start of a long
    list (Signet, 2026-11-19, never reached 'Computer Engineering')."""
    words = [w for w in dict.fromkeys(re.findall(r"[A-Za-z]+", field)) if w.lower() not in _STUDY_FILLER]
    if len(words) < 2:
        return [field] if field else []
    ranked = sorted(words, key=lambda w: (w.lower() in _GENERIC_STUDY, -len(w)))
    return [t for t in dict.fromkeys([field, *ranked]) if t]


def study_score(field: str, option: str) -> float:
    """How well a list entry names his field: the same name, or one holding all its words ('Computer and
    Information Sciences' for Computer Science; plurals count), but never another field ('Computer Engineering')."""
    def stems(text: str) -> set[str]:
        return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in _norm_words(text)}
    return max(match_option(field, [option])[1], 0.9 if stems(field) <= stems(option) else 0.0)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _section_text(page: Any) -> str:
    try:
        group = page.locator("[role=group][aria-labelledby='Education-section']")
        return group.first.inner_text() if group.count() else ""
    except Exception:
        return ""


def fill_education(page: Any, schools: list[SchoolEntry], pause: Callable[[], None]) -> list[FieldResult]:
    results: list[FieldResult] = []
    if not has_section(page, "Education-section", "education", "Education"):
        return results
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
            notes, detail = [], ""
            # the box is education-N--schoolName on some sites and education-N--school on others (Visa, Analog
            # Devices, 2026-11-16: helper looked only for schoolName, typed nothing and Workday said it was empty)
            key = next((k for k in SCHOOL_KEYS if _by_id(page, base + k).count()), None)
            school = _field_of(page, base + (key or SCHOOL_KEYS[0]))
            if key is None:
                notes.append("school: helper couldn't find the School box; choose it in the window")
            elif school.locator("[data-automation-id=multiselectInputContainer]").count():
                # type a short search and press Enter, then pick from the list (Vatsal's way, 2026-11-16)
                ok, shown, why = choose_prompt(page, school, school_search_terms(s.school),
                                               lambda t: match_option(s.school, [t])[1], threshold=0.85,
                                               keep_existing=False)
                if not ok and re.search(r"(?i)\btype in \W?other\b", _section_text(page)):
                    # the site's own instruction: "If your school is not listed, please type in 'Other'" (Visa)
                    ok, shown, why = choose_prompt(page, school, ["Other"], lambda t: 1.0 if _norm(t) == "other" else 0.0,
                                                   threshold=0.9, keep_existing=False)
                    if ok:
                        detail = f"'{s.school}' isn't in this site's list, so Other (as the page asks)"
                if not ok:
                    notes.append(f"school: {why}")
                elif not selected_items(school):  # it clicked something, but nothing was picked (Rocket, Visa)
                    notes.append("school: the pick didn't hold; choose it (or delete the entry) in the window")
            else:
                _by_id(page, base + key).first.fill(s.school)
                # a search box that wants a pick from its list doesn't count typed text (Dexcom, Martin Marietta)
                if school.locator("[data-automation-id=promptIcon], [data-automation-id=searchBox]").count() \
                        and not selected_items(school):
                    notes.append("school: this site's school list didn't offer it; pick it (or delete the entry) in "
                                 "the window")
            if notes and DEBUG_DIR is not None:  # the field's markup, so Claude can see why (run folder, git-ignored)
                try:
                    (DEBUG_DIR / f"education_{n}_school.html").write_text(school.evaluate("e => e.outerHTML")[:60000],
                                                                         encoding="utf-8")
                except Exception:
                    pass
            degree = _field_of(page, base + "degree")
            if degree.count() and degree.locator("button[aria-haspopup=listbox]").count():
                notes += _choose_degree(page, degree, s.degree)
            study = _field_of(page, base + "fieldOfStudy")
            if study.count() and s.field_of_study:
                ok, _, why = choose_prompt(page, study, study_search_terms(s.field_of_study),
                                           lambda t: study_score(s.field_of_study, t), threshold=0.85,
                                           keep_existing=False)
                if not ok:
                    notes.append(f"field of study: {why}")
            if s.gpa and _by_id(page, base + "gradeAverage").count():  # Vatsal, 2026-11-15: forms may get GPA
                _by_id(page, base + "gradeAverage").first.fill(s.gpa)
            _date_in(page, base + "firstYearAttended", s.start)
            _date_in(page, base + "lastYearAttended", s.end)
            results.append(FieldResult(f"workday:school:{n}", label, not notes, f"{_span(s.start)} to {_span(s.end)}",
                                       "; ".join(notes) or detail))
        except Exception as exc:
            results.append(FieldResult(f"workday:school:{n}", label, False, None, f"{type(exc).__name__}: {str(exc)[:120]}"))
        pause()
    if DEBUG_DIR is not None:  # the whole section as filled, to diagnose "School or University is required" later
        try:
            group = page.locator("[role=group][aria-labelledby='Education-section']")
            if group.count():
                (DEBUG_DIR / "education_section.html").write_text(group.first.evaluate("e => e.outerHTML")[:200000],
                                                                  encoding="utf-8")
        except Exception:
            pass
    return results


def _choose_degree(page: Any, degree: Any, wanted: str) -> list[str]:
    """Pick the degree. Returns what went wrong, if anything. A list that opened slowly (an overloaded Mac) left
    HPE's required Degree empty with no word about it (2026-11-18), so a closed list is tried again, a failed pick
    is reported, and a no-match names the choices."""
    options = dropdown_options(page, degree)
    if not options:
        page.wait_for_timeout(1500)
        options = dropdown_options(page, degree)
    if not options:
        return ["degree: the Degree list didn't open; pick it in the window"]
    scored = sorted(((degree_score(wanted, o), -len(o), o) for o in options if o.strip()), reverse=True)
    best, score = scored[0][2], scored[0][0]
    if score < 0.85:
        return [f"degree: no choice matches '{wanted}' (choices: {', '.join(options[:15])})"]
    ok, shown, why = choose_dropdown(page, degree, best)
    if not ok:
        ok, shown, why = choose_dropdown(page, degree, best)
    held = " ".join(degree.locator("button[aria-haspopup=listbox]").first.inner_text().split())
    if not ok or match_option(best, [held])[1] < 0.95:
        return [f"degree: picking '{best}' didn't hold ({why or 'the box still shows ' + repr(held)}); pick it in the "
                "window"]
    return []


def required_gpa_fields(page: Any) -> list[str]:
    """Education entries whose GPA box is required and empty. helper never fills GPA (rule 3): Vatsal decides."""
    found = page.evaluate("""() => [...document.querySelectorAll("[data-automation-id='formField-gradeAverage']")]
        .filter(f => f.querySelector('abbr[title=required], .requiredAsterisk'))
        .filter(f => { const i = f.querySelector('input'); return i && !i.value; })
        .map(f => { const panel = f.closest('[role=group][aria-labelledby]');
                    return panel ? (document.getElementById(panel.getAttribute('aria-labelledby')) || {}).innerText || '' : ''; })""")
    return [f"Overall Result (GPA){' in ' + name.strip() if name and name.strip() else ''} (rule 3: helper never fills GPA)"
            for name in found]


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


def delete_education(page: Any) -> int:
    """Remove every education entry (Vatsal, 2026-11-16: when Workday wants a school it won't let anyone pick, delete
    the section and move on; Dexcom and Rocket accepted the page then). Returns how many went."""
    gone = 0
    for i in entry_ids(page, "education")[::-1]:
        before = len(entry_ids(page, "education"))
        key = next((k for k in SCHOOL_KEYS if _by_id(page, f"education-{i}--{k}").count()), SCHOOL_KEYS[0])
        _delete_entry(page, f"education-{i}--{key}")
        if len(entry_ids(page, "education")) < before:
            gone += 1
    return gone


def _has_any_choices(page: Any, where: Any) -> bool:
    """Whether a search box offers anything at all when opened."""
    try:
        where.locator("input:not([type=hidden])").first.click()
        page.wait_for_timeout(1200)
        return page.locator(PROMPT_OPTIONS).count() > 0
    except Exception:
        return True  # unsure: search as usual
    finally:
        _close_popup(page)


def fill_certifications(page: Any, certs: list[dict[str, Any]], pause: Callable[[], None]) -> list[FieldResult]:
    """Add each certificate the company's certification list has, with its ID and issue date. Workday lists are
    company-specific: a certificate the list doesn't have is left out (its empty entry is removed again)."""
    section = "Certifications-section"
    if not certs or not page.locator(f"[role=group][aria-labelledby={json.dumps(section)}]").count():
        return []
    added, missing, missed_issuers = [], [], set()
    for n, cert in enumerate(certs):
        name, issuer = cert.get("name", ""), cert.get("issuer") or ""
        if issuer and issuer in missed_issuers:  # this issuer isn't on the company's list: skip its other certificates
            missing.append(name)
            continue
        i = _add_entry(page, section, "certification")
        if i is None:
            missing.append(name)
            continue
        base = f"certification-{i}--"
        field = _field_of(page, base + "certification")
        prompt = field.locator("[data-automation-id=multiselectInputContainer], [data-automation-id=searchBox]")
        if n == 0 and prompt.count() and not _has_any_choices(page, field):
            _delete_entry(page, base + "certification")  # no list at all: nothing can match
            return [FieldResult("workday:certifications", "Certifications", True, "none added",
                                "this company's Workday has no certification list to choose from")]
        try:
            if prompt.count():
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
            if issuer:
                missed_issuers.add(issuer)
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


_URL_INPUTS = "input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file])"


def fill_websites(page: Any, urls: list[str], pause: Callable[[], None]) -> list[FieldResult]:
    """Workday's Websites section: one Add per link (LinkedIn, GitHub, Portfolio; Vatsal, 2026-11-15). Links already
    listed are left alone, so a second pass over the page adds nothing twice."""
    group = page.locator("[role=group][aria-labelledby='Websites-section']")
    urls = [u for u in urls if u]
    if not urls or not group.count():
        return []
    norm = lambda u: u.strip().rstrip("/").lower()  # noqa: E731
    values = group.locator(_URL_INPUTS).evaluate_all("els => els.map(e => e.value)")
    present = {norm(v) for v in values if v}
    todo = [u for u in urls if norm(u) not in present]
    added, failed = [], []
    # a row already there but blank takes a link first: a page that opened while the account's rows were still
    # loading kept them empty, and Workday refused it ("The field URL is required"; FreedomRoads, 2026-11-18)
    for i, value in enumerate(values):
        if todo and not value.strip():
            url = todo.pop(0)
            group.locator(_URL_INPUTS).nth(i).fill(url)
            present.add(norm(url))
            added.append(url)
            pause()
    boxes = group.locator(_URL_INPUTS)

    def blank_row() -> Optional[int]:
        return next((i for i, v in enumerate(boxes.evaluate_all("els => els.map(e => e.value)")) if not v.strip()), None)

    for url in todo:
        slot = blank_row()  # a row an earlier Add brought late takes this link (it was deleted as blank before)
        if slot is None:
            button = group.locator("button[data-automation-id=add-button]")
            if not button.count():
                failed.append(url)
                continue
            before = boxes.count()
            # the first Add on a fresh page can do nothing, so LinkedIn, always first, was never added (Zillow, Bose,
            # 2026-11-18): click again when no row comes; a new row can take a few seconds (Bloomberg Industry Group)
            for _attempt in (1, 2):
                button.first.click()
                for _ in range(10):
                    page.wait_for_timeout(500)
                    if boxes.count() > before:
                        break
                if boxes.count() > before:
                    break
            slot = blank_row()
        if slot is None:
            failed.append(url)
            continue
        boxes.nth(slot).fill(url)
        present.add(norm(url))
        added.append(url)
        pause()
    removed = _drop_blank_websites(page, group)
    detail = f"couldn't add: {', '.join(failed)}" if failed else ""
    if removed:
        detail = "; ".join(d for d in (detail, f"deleted {removed} blank row(s)") if d)
    return [FieldResult("workday:websites", "Websites", not failed, "; ".join(added) or "already listed", detail)]


def _drop_blank_websites(page: Any, group: Any) -> int:
    """Delete Websites rows still blank once every link is in (Workday won't take an empty URL). Only a row whose own
    panel holds exactly that one box is deleted, so a filled row never goes with it."""
    removed = 0
    for _ in range(6):
        values = group.locator(_URL_INPUTS).evaluate_all("els => els.map(e => e.value)")
        blank = [i for i, v in enumerate(values) if not v.strip()]
        if not blank:
            break
        panel = group.locator(_URL_INPUTS).nth(blank[-1]).locator("xpath=ancestor::*[@role='group'][1]")
        delete = panel.get_by_role("button", name=re.compile(r"^Delete", re.I))
        if panel.locator(_URL_INPUTS).count() != 1 or not delete.count():
            break
        delete.first.click()
        page.wait_for_timeout(600)
        if group.locator(_URL_INPUTS).count() >= len(values):
            break
        removed += 1
    return removed
