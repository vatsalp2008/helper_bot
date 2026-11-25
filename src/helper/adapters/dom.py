"""Reading and filling application forms from the page itself (Lever, Ashby; later the generic adapter).

Neither Lever nor Ashby publishes its form questions, so helper scans the rendered form. Every question block becomes
a FormQuestion whose field_name is a stable key (the block's question id or field name). The scan also tags each block
with data-helper-key, so filling finds it again in the live page.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..models import FormQuestion
from .common import FieldResult, keyboard, tick

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
    // Education History is a set of entries, one per school (ashby.fill_education); read as one checkbox, it came
    // to Claude as a question nobody could answer (Secureframe, 2026-11-21)
    if (c.getAttribute('data-field-path') === '_systemfield_education_history') continue;
    const key = 'ashby:' + (c.getAttribute('data-field-path') || clean(text(title)));
    const required = /_required/.test(title.className) || !!c.querySelector('[required],[aria-required=true]');
    const description = text(c.querySelector('.ashby-application-form-question-description'));
    let type = null, options = [];
    // a phone box with a consent choice under the same title ("Phone Number" with "Yes - I consent to receiving text
    // messages", Illumio, 2026-11-23): read as the radio alone, the required number stayed empty and nothing was sent.
    // Two questions: the box, keyed on its own wrapper, and the choice, keyed on the field.
    const box = c.querySelector('input[type=tel], input[type=text], input[type=email], input:not([type])');
    if (box && c.querySelector('input[type=radio]') && !c.querySelector('.ashby-application-form-input-yesno')) {
      let wrap = box;
      while (wrap.parentElement && wrap.parentElement !== c && !wrap.parentElement.querySelector('input[type=radio]'))
        wrap = wrap.parentElement;
      // with no wrapper of its own (the box beside the choices, or right in the field: Illumio's live form, where the
      // field's own key replaced the box's), the box itself carries the key; _inside reads a tagged control
      push(wrap, key + ':box', text(title), 'text', required, [], null);
      const choices = [...c.querySelectorAll('input[type=radio]')].map(optionLabel);
      const sms = choices.some(o => /text messag|\bsms\b/i.test(o || ''));
      push(c, key, sms ? 'Consent to receive text messages (' + clean(text(title)) + ')' : text(title), 'radio',
           required, choices, description);
      continue;
    }
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


_MENU_TOGGLE = "button[aria-haspopup]"


def _choose_from_menu(root: Any, block: Any, wanted: str) -> tuple[bool, Optional[str], str]:
    """A styled dropdown whose real <select> stays empty until something is picked: BambooHR's Fabric select keeps
    its choices in a pop-up menu (All Options' State and Country, 2026-11-18). Open it and click the best item."""
    from ..answers import us_state_name

    toggle = block.locator(_MENU_TOGGLE).first
    toggle.click()
    menu = toggle.get_attribute("data-menu-id") or toggle.get_attribute("aria-controls")
    items = (root.locator(f"[id={json.dumps(menu)}] [role=menuitem], [id={json.dumps(menu)}] [role=option]") if menu
             else root.locator("[role=menuitem]:visible, [role=option]:visible"))
    try:
        items.first.wait_for(state="visible", timeout=5000)
    except Exception:
        keyboard(root).press("Escape")
        return False, wanted, "the dropdown's list didn't open"
    texts = [" ".join(t.split()) for t in items.all_inner_texts()]
    index = _pick(wanted, texts)
    if index is None and us_state_name(wanted):  # "WA" in a list of full names
        index = _pick(us_state_name(wanted) or "", texts)
    if index is None:
        keyboard(root).press("Escape")
        return False, wanted, f"no option matches '{wanted}' (choices: {', '.join(texts[:10])})"
    items.nth(index).click()
    return True, texts[index], ""


_COMBOBOX = "[role=combobox]"
# a dropdown's choices: options, or the cells of the grid Oracle's cx-select opens (Vertiv, 2026-11-19)
_OPTIONS = "[role=option]:visible, [role=gridcell]:visible"
_SHOWN_JS = "e => (e.value !== undefined ? e.value : e.innerText || '').replace(/\\s+/g, ' ').trim()"
_UNPICKED = re.compile(r"(?i)^(select|choose|search|pick)\b.{0,15}$|^$")


def _open_options(root: Any, combo: Any, timeout_ms: int) -> tuple[Any, list[str]]:
    """Click an ARIA combobox and read the options its list shows ([] when none show)."""
    combo.click(timeout=5000)
    items = root.locator(_OPTIONS)
    try:
        items.first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        return items, []
    ref = combo.get_attribute("aria-controls") or combo.get_attribute("aria-owns")
    if ref:  # the list this box opened, when it names it
        own = root.locator(f"[id={json.dumps(ref)}] [role=option], [id={json.dumps(ref)}] [role=gridcell]")
        if own.count():
            items = own
    return items, [" ".join(t.split()) for t in items.all_inner_texts()]


def listbox_options(root: Any, key: str, timeout_ms: int = 2500) -> list[str]:
    """The choices of an ARIA dropdown (Rippling, 2026-11-18), read by opening its list and closing it again; nothing
    is picked. [] for a box that shows nothing until typed into (a place search)."""
    combo = _inside(_block(root, key), _COMBOBOX)
    texts: list[str] = []
    try:
        if combo.count():
            _, texts = _open_options(root, combo, timeout_ms)
    except Exception:
        texts = []
    finally:
        try:
            keyboard(root).press("Escape")
        except Exception:
            pass
    return [t for t in dict.fromkeys(texts) if t]


def _choose_from_listbox(root: Any, combo: Any, wanted: str) -> tuple[bool, Optional[str], str]:
    """An ARIA dropdown (Rippling's role=combobox, 2026-11-18): open its list and click the best option. A dropdown
    with a search box gets the answer's first words typed when the option isn't in view (long lists)."""
    from ..answers import us_state_name

    def best(texts: list[str]) -> Optional[int]:
        index = _pick(wanted, texts) if texts else None
        if index is None and texts and us_state_name(wanted):  # "WA" in a list of full names
            index = _pick(us_state_name(wanted) or "", texts)
        return index

    items, texts = _open_options(root, combo, 5000)
    index = best(texts)
    if index is None and combo.evaluate("e => e.tagName === 'INPUT'"):
        combo.fill("")
        combo.type(re.split(r"[(,]", wanted)[0].strip()[:30], delay=40)
        try:
            items = root.locator(_OPTIONS)
            items.first.wait_for(state="visible", timeout=4000)
            texts = [" ".join(t.split()) for t in items.all_inner_texts()]
        except Exception:
            texts = []
        index = best(texts)
    if index is None:
        keyboard(root).press("Escape")
        if not texts:
            return False, wanted, "the dropdown's list didn't open"
        return False, wanted, f"no option matches '{wanted}' (choices: {', '.join(texts[:10])})"
    items.nth(index).click()
    shown = ""
    for _ in range(10):  # the box shows the pick a moment later
        shown = combo.evaluate(_SHOWN_JS)
        if not _UNPICKED.match(shown):
            break
        time.sleep(0.2)
    if _UNPICKED.match(shown):
        return False, texts[index], f"the dropdown still shows '{shown or 'nothing'}' after picking '{texts[index]}'"
    return True, texts[index], ""


def _choose_aria_radio(block: Any, wanted: str) -> tuple[bool, Optional[str], str]:
    """A radio group of role=radio elements with no input inside (Rippling's Strong No / No / Yes / Strong Yes
    questions, 2026-11-18): click the best option and check that it took."""
    radios = block.locator("[role=radio]")
    labels = [(radios.nth(i).get_attribute("aria-label") or radios.nth(i).inner_text()).strip()
              for i in range(radios.count())]
    index = _pick(wanted, labels)
    if index is None:
        return False, wanted, f"no option matches '{wanted}' (choices: {', '.join(labels[:8])})"
    radio = radios.nth(index)
    for _ in range(2):
        radio.click()
        for _ in range(10):
            if radio.get_attribute("aria-checked") == "true":
                return True, labels[index], ""
            time.sleep(0.15)
    return False, labels[index], f"the site didn't keep the pick on '{labels[index]}'"


def _choose_pills(block: Any, wanted: list[str]) -> tuple[bool, Optional[str], str]:
    """Oracle's multi-select pills (buttons in a ul role=list; "Check all of the following statements that apply to
    you:", Honeywell, 2026-11-20): click each wanted pill once and check Oracle marks it selected."""
    pills = block.locator("button.cx-select-pill-section")
    labels = [" ".join(pills.nth(i).inner_text().split()) for i in range(pills.count())]
    chosen = []
    for want in wanted:
        index = _pick(want, labels)
        if index is None:
            return False, want, f"no option matches '{want}' (choices: {', '.join(labels[:8])})"
        pill = pills.nth(index)
        if "cx-select-pill-section--selected" not in (pill.get_attribute("class") or ""):
            pill.click()
            for _ in range(10):
                if "cx-select-pill-section--selected" in (pill.get_attribute("class") or ""):
                    break
                time.sleep(0.15)
            else:
                return False, labels[index], f"the site didn't keep the pick on '{labels[index]}'"
        chosen.append(labels[index])
    return True, "; ".join(chosen), ""


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


def _inside(block: Any, css: str) -> Any:
    """The control in a question's block, or the block itself when the reader tagged the control directly (fields with
    no wrapper of their own)."""
    try:
        if block.evaluate("(e, css) => e.matches(css)", css):
            return block
    except Exception:
        pass
    return block.locator(css).first


def _fill_one(root: Any, block: Any, a: Any, pdf: Path, city: Optional[str],
              date_text: Callable[[str], str]) -> tuple[bool, Optional[str], str]:
    if a.type == "file":
        upload = Path(a.value) if a.key in ("transcript", "cover_letter") and a.value else pdf
        _inside(block, "input[type=file]").set_input_files(str(upload))
        return True, upload.name, ""
    if a.type == "textarea":
        _inside(block, "textarea").fill(a.value)
        return True, a.value, ""
    if a.type == "select":
        select = _inside(block, "select")
        if not select.count():
            combo = _inside(block, _COMBOBOX)
            if combo.count():  # an ARIA dropdown with no <select> (Rippling, 2026-11-18)
                return _choose_from_listbox(root, combo, a.value)
        options = [o.strip() for o in select.locator("option").all_inner_texts()]
        if not any(options) and block.locator(_MENU_TOGGLE).count():
            return _choose_from_menu(root, block, a.value)
        index = _pick(a.value, options)
        if index is None:
            return False, a.value, f"no option matches '{a.value}'"
        select.select_option(index=index)
        return True, options[index], ""
    if a.type in ("radio", "multi_select", "boolean"):
        kind = "radio" if a.type == "radio" else "checkbox"
        boxes = _labels(block, kind)
        if not boxes and a.type == "radio" and block.locator("[role=radio]").count():
            return _choose_aria_radio(block, a.value)
        if not boxes and a.type == "multi_select" and block.locator(".cx-select-pill-section").count():
            return _choose_pills(block, [v.strip() for v in a.value.split(";") if v.strip()])
        wanted = [v.strip() for v in a.value.split(";")] if a.type == "multi_select" else [a.value]
        if a.type == "boolean":
            same = " ".join(a.value.strip(" *").split()).lower() == " ".join(a.label.strip(" *").split()).lower()
            if a.value.strip().lower() not in ("yes", "true", "i agree", "agree") and not same:  # "…NDA*" (Shopify)
                return True, None, "left unchecked"
            if not tick(boxes[0][0]):
                return False, a.value, "the site didn't keep the tick"
            return True, "checked", ""
        chosen = []
        for want in wanted:
            index = _pick(want, [label for _, label in boxes])
            if index is None:
                return False, a.value, f"no option matches '{want}'"
            if not tick(boxes[index][0]):
                return False, a.value, f"the site didn't keep the tick on '{boxes[index][1]}'"
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
        field = _inside(block, _ANSWER_INPUTS)
        typed = city if (city and a.key == "location_city") else a.value.split(",")[0]
        ok, shown = _choose_suggestion(root, field, typed, a.value)
        return ok, shown if ok else a.value, "" if ok else shown
    if a.type == "date":
        field = _inside(block, _ANSWER_INPUTS)
        field.click()
        field.fill(date_text(a.value))
        keyboard(root).press("Enter")
        return True, date_text(a.value), ""
    field = _inside(block, _ANSWER_INPUTS)
    if a.key == "location_city" and (field.get_attribute("aria-autocomplete") == "list"
                                     or field.get_attribute("role") == "combobox"):
        # a place search that wants one of its suggestions picked (Rippling's Location, 2026-11-18)
        ok, shown = _choose_suggestion(root, field, city or a.value.split(",")[0], a.value)
        return ok, shown if ok else a.value, "" if ok else shown
    if (field.get_attribute("type") or "").lower() == "number":
        # a numbers-only box: "Yes" failed with "Cannot type text into input[type=number]" (Snowflake, 2026-11-19)
        number = re.search(r"\d+(?:\.\d+)?", a.value.replace(",", ""))
        if number is None:
            return False, a.value, f"a numbers-only box can't take '{a.value}'"
        field.fill(number.group(0))
        return True, number.group(0), ""
    field.fill(a.value)
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
