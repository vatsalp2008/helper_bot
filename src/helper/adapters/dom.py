"""Reading and filling application forms from the page itself (Lever, Ashby; later the generic adapter).

Neither Lever nor Ashby publishes its form questions, so helper scans the rendered form. Every question block becomes
a FormQuestion whose field_name is a stable key (the block's question id or field name). The scan also tags each block
with data-helper-key, so filling finds it again in the live page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from ..models import FormQuestion
from .common import FieldResult, keyboard

# Returns one entry per question block: {key, label, type, required, options, description}.
DISCOVER_JS = r"""
() => {
  const out = [];
  const text = el => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const clean = s => s.replace(/\s*[✱*]+\s*$/, '').replace(/^\s*[✱*]+\s*/, '').trim();
  const placeholder = s => /^select\b|^choose\b|^-+$|^$/i.test(s.trim());
  const optionLabel = input => {
    if (input.labels && input.labels.length) return text(input.labels[0]);
    const wrap = input.closest('label');
    return wrap ? text(wrap) : (input.value || '');
  };
  const push = (c, key, label, type, required, options, description) => {
    if (!label || out.some(o => o.key === key)) return;
    c.setAttribute('data-helper-key', key);
    out.push({key, label: clean(label), type, required: !!required, options: options.filter(o => o && !placeholder(o)),
              description: description || null});
  };

  // Ashby: every question sits in an element carrying its id (data-field-path); radio and checkbox groups put
  // a fieldset inside it, other fields are the element itself.
  for (const c of document.querySelectorAll('[data-field-path]')) {
    const title = c.querySelector('.ashby-application-form-question-title');
    if (!title || c.parentElement.closest('[data-field-path]')) continue;
    const key = 'ashby:' + (c.getAttribute('data-field-path') || clean(text(title)));
    const required = /_required/.test(title.className) || !!c.querySelector('[required],[aria-required=true]');
    const description = text(c.querySelector('.ashby-application-form-question-description'));
    let type = null, options = [];
    if (c.querySelector('.ashby-application-form-input-yesno')) { type = 'yesno'; options = ['Yes', 'No']; }
    else if (c.querySelector('input[type=radio]')) {
      type = 'radio';
      options = [...c.querySelectorAll('input[type=radio]')].map(optionLabel);
    } else if (c.querySelector('.ashby-application-form-input-checkbox-group')) {
      type = 'multi_select';
      options = [...c.querySelectorAll('input[type=checkbox]')].map(optionLabel);
    } else if (c.querySelector('input[type=checkbox]')) { type = 'boolean'; options = [clean(text(title))]; }
    else if (c.querySelector('input[type=file]')) type = 'file';
    else if (c.querySelector('textarea')) type = 'textarea';
    else if (c.querySelector('.ashby-application-form-input-date')) type = 'date';
    else if (c.querySelector('[role=combobox], .ashby-application-form-input-autocomplete')) type = 'combobox';
    else if (c.querySelector('select')) { type = 'select'; options = [...c.querySelector('select').options].map(o => o.text); }
    else if (c.querySelector('input')) type = 'text';
    if (type) push(c, key, text(title), type, required, options, description);
  }

  // Lever: one .application-question per question (li in the form, div in the EEO survey), keyed by the control's name.
  for (const c of document.querySelectorAll('.application-question')) {
    const labelEl = c.querySelector('.application-label .text') || c.querySelector('.application-label');
    const control = c.querySelector('input:not([type=hidden]), select, textarea');
    if (!labelEl || !control) continue;
    const label = text(labelEl).split(' ✱')[0];
    const required = !!c.querySelector('.required') || control.required;
    const key = 'lever:' + (control.name || clean(label));
    let type, options = [];
    if (control.type === 'file') type = 'file';
    else if (control.tagName === 'SELECT') { type = 'select'; options = [...control.options].map(o => o.text); }
    else if (control.type === 'radio') { type = 'radio'; options = [...c.querySelectorAll('input[type=radio]')].map(optionLabel); }
    else if (control.type === 'checkbox') {
      const boxes = [...c.querySelectorAll('input[type=checkbox]')];
      if (boxes.length > 1) { type = 'multi_select'; options = boxes.map(optionLabel); }
      else { type = 'boolean'; options = [optionLabel(control)]; }
    } else if (control.tagName === 'TEXTAREA') type = 'textarea';
    else if (control.id === 'location-input' || control.name === 'location') type = 'combobox';
    else type = 'text';
    push(c, key, label, type, required, options, null);
  }
  return out;
}
"""

_ANSWER_INPUTS = "input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=file]):not([type=submit])"


def discover(root: Any) -> list[FormQuestion]:
    """Every question block in the form (a Page or Frame), as FormQuestions keyed for filling."""
    found = root.evaluate(DISCOVER_JS)
    questions = []
    for f in found:
        section = "eeoc" if re.search(r"(?i)^(gender|race|ethnicity|veteran|disability|hispanic)", f["label"]) else "questions"
        questions.append(FormQuestion(label=f["label"], field_name=f["key"], type=f["type"], required=f["required"],
                                      options=f["options"], description=f.get("description"), section=section))
    return questions


def _block(root: Any, key: str) -> Any:
    return root.locator(f"[data-helper-key={json.dumps(key)}]")


def _labels(block: Any, kind: str) -> list[tuple[Any, str]]:
    inputs = block.locator(f"input[type={kind}]")
    out = []
    for i in range(inputs.count()):
        el = inputs.nth(i)
        label = el.evaluate("i => (i.labels && i.labels.length ? i.labels[0].innerText : (i.closest('label') ? "
                            "i.closest('label').innerText : i.value || '')).replace(/\\s+/g, ' ').trim()")
        out.append((el, label))
    return out


def _pick(wanted: str, options: list[str]) -> Optional[int]:
    from ..answers import match_option

    best, score = match_option(wanted, options)
    return options.index(best) if best is not None and score >= 0.8 else None


def _choose_suggestion(root: Any, field: Any, typed: str, wanted: str, timeout_ms: int = 6000) -> tuple[bool, str]:
    """Type into an autocomplete box and click the suggestion that best matches `wanted`."""
    field.click()
    field.fill("")
    field.type(typed, delay=50)
    options = root.locator("[role=option]:visible, .dropdown-results .dropdown-location:visible")
    try:
        options.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        return True, typed  # no suggestions: the typed text stays
    texts = [t.strip() for t in options.all_inner_texts()]
    index = _pick(wanted, texts)
    if index is None:
        index = next((i for i, t in enumerate(texts) if t.lower().startswith(typed.lower())), None)
    if index is None:
        keyboard(root).press("Escape")
        return False, f"no suggestion matches '{wanted}' (saw: {', '.join(texts[:6])})"
    options.nth(index).click()
    return True, texts[index]


def fill_dom_form(root: Any, answers: list[Any], resume_pdf: Path, *, city: Optional[str],
                  pause: Callable[[], None], date_text: Callable[[str], str] = lambda v: v) -> list[FieldResult]:
    """Fill every planned answer whose block is in the form. Never touches the submit button."""
    root.evaluate(DISCOVER_JS)  # tag the blocks in this page load
    results: list[FieldResult] = []
    for a in answers:
        if a.kind in ("skip", "unknown") or (a.value is None and a.type != "file"):
            continue
        block = _block(root, a.field)
        if not block.count():
            results.append(FieldResult(a.field, a.label, not a.required, None,
                                       "not shown on this form" if not a.required else "field not found on the page"))
            continue
        try:
            ok, shown, detail = _fill_one(root, block, a, resume_pdf, city, date_text)
        except Exception as exc:  # a field that won't take input is reported, not fatal
            ok, shown, detail = False, a.value, f"{type(exc).__name__}: {str(exc)[:120]}"
        results.append(FieldResult(a.field, a.label, ok, shown, detail))
        pause()
    return results


def _fill_one(root: Any, block: Any, a: Any, pdf: Path, city: Optional[str],
              date_text: Callable[[str], str]) -> tuple[bool, Optional[str], str]:
    if a.type == "file":
        upload = Path(a.value) if a.key == "transcript" and a.value else pdf
        block.locator("input[type=file]").first.set_input_files(str(upload))
        return True, upload.name, ""
    if a.type == "textarea":
        block.locator("textarea").first.fill(a.value)
        return True, a.value, ""
    if a.type == "select":
        select = block.locator("select").first
        options = [o.strip() for o in select.locator("option").all_inner_texts()]
        index = _pick(a.value, options)
        if index is None:
            return False, a.value, f"no option matches '{a.value}'"
        select.select_option(index=index)
        return True, options[index], ""
    if a.type in ("radio", "multi_select", "boolean"):
        kind = "radio" if a.type == "radio" else "checkbox"
        boxes = _labels(block, kind)
        wanted = [v.strip() for v in a.value.split(";")] if a.type == "multi_select" else [a.value]
        if a.type == "boolean":
            if a.value.strip().lower() not in ("yes", "true", "i agree", "agree") and a.value != a.label:
                return True, None, "left unchecked"
            boxes[0][0].check(force=True)
            return True, "checked", ""
        chosen = []
        for want in wanted:
            index = _pick(want, [label for _, label in boxes])
            if index is None:
                return False, a.value, f"no option matches '{want}'"
            boxes[index][0].check(force=True)
            chosen.append(boxes[index][1])
        return True, "; ".join(chosen), ""
    if a.type == "yesno":
        option = "yes" if a.value.strip().lower().startswith("y") else "no"
        button = block.locator(f"button[data-option='{option}']")
        if not button.count():
            button = block.get_by_role("button", name=a.value.strip(), exact=True)
        button.first.click()
        return True, a.value, ""
    if a.type == "combobox":
        field = block.locator(_ANSWER_INPUTS).first
        typed = city if (city and a.key == "location_city") else a.value.split(",")[0]
        ok, shown = _choose_suggestion(root, field, typed, a.value)
        return ok, shown if ok else a.value, "" if ok else shown
    if a.type == "date":
        field = block.locator(_ANSWER_INPUTS).first
        field.click()
        field.fill(date_text(a.value))
        keyboard(root).press("Enter")
        return True, date_text(a.value), ""
    block.locator(_ANSWER_INPUTS).first.fill(a.value)
    return True, a.value, ""


def scan_form(url: str, anchors: tuple[str, ...], hosts: tuple[str, ...], timeout_ms: int = 45000) -> tuple[list[FormQuestion], str]:
    """Open the application page in a hidden browser (no profile, nothing typed) and read its questions.
    Returns the questions and the page title."""
    from playwright.sync_api import sync_playwright

    from ..errors import HelperError
    from .common import find_form

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            form = find_form(page, anchors, hosts=hosts)
            if form is None:
                raise HelperError(f"couldn't find the application form at {url}")
            return discover(form), page.title()
        finally:
            browser.close()
