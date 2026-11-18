"""Pieces every adapter shares: field results, finding the form (also inside an embedding frame), confirmations."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

# Text that means the application went through (Greenhouse, Lever, Ashby).
CONFIRMATION_RE = re.compile(
    r"(?i)thank you for (?:applying|your application)|thanks for applying"  # not "your interest": job posts end with it
    r"|(?:your )?application (?:has been |was )?(?:successfully )?(?:submitted|received)"
    r"|we(?:'|’)ve received your application|successfully submitted"
)


@dataclass
class FieldResult:
    field: str
    label: str
    ok: bool
    value: Optional[str]
    detail: str = ""


def keyboard(root: Any) -> Any:
    """The keyboard of a Page, or of the page a Frame belongs to."""
    return root.keyboard if hasattr(root, "keyboard") else root.page.keyboard


def find_form(page: Any, anchors: tuple[str, ...], hosts: tuple[str, ...] = (), timeout_ms: int = 20000) -> Optional[Any]:
    """The frame holding the application form: the page itself, or a frame a company careers page embeds.
    Frames on `hosts` are searched first."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        frames = [page.main_frame] + sorted((f for f in page.frames if f is not page.main_frame),
                                            key=lambda f: not any(h in f.url for h in hosts))
        for frame in frames:
            try:
                if any(frame.locator(a).count() for a in anchors):
                    return frame
            except Exception:
                continue
        if time.monotonic() > deadline:
            return None
        page.wait_for_timeout(500)


_CHECKED_JS = ("i => i.checked || ((i.closest('[role=radio],[role=checkbox],[role=switch]') || "
               "{getAttribute: () => null}).getAttribute('aria-checked') === 'true')")


def tick(el: Any) -> bool:
    """Tick a radio button or checkbox. Sites often hide the real box behind a styled wrapper (role=radio with
    aria-checked) or a label, and update a moment after the click: try the wrapper, the box, then the label, and wait
    for the tick each time. Each try only happens while the box is still unticked, so a checkbox never flips back."""
    def ticked() -> bool:
        try:
            return bool(el.evaluate(_CHECKED_JS))
        except Exception:
            return False

    if ticked():
        return True
    page = el.page
    wrapper = el.locator("xpath=ancestor::*[@role='radio' or @role='checkbox' or @role='switch'][1]")
    label = el.locator("xpath=ancestor::label[1]")
    attempts = []
    if wrapper.count():
        attempts.append(lambda: wrapper.first.click())
    attempts.append(lambda: el.click(force=True))
    if label.count():
        attempts.append(lambda: label.first.click())
    attempts.append(lambda: el.evaluate("i => { const l = i.labels && i.labels[0]; if (l) l.click(); }"))
    for attempt in attempts:
        try:
            attempt()
        except Exception:
            continue
        for _ in range(8):
            if ticked():
                return True
            page.wait_for_timeout(200)
    return ticked()


def click_submit(form: Any, selectors: tuple[str, ...]) -> bool:
    """Click the form's Submit button once (auto mode only). Only when exactly one visible, enabled button matches."""
    for selector in selectors:
        buttons = form.locator(selector)
        ready = [buttons.nth(i) for i in range(buttons.count())
                 if buttons.nth(i).is_visible() and buttons.nth(i).is_enabled()]
        if len(ready) == 1:
            ready[0].click()
            return True
        if len(ready) > 1:
            return False
    return False


VISIBLE_ERRORS_JS = r"""
() => {
  const visible = e => !!(e.offsetParent || e.getClientRects().length);
  const texts = [...document.querySelectorAll('[role=alert], .error, .error-message, .field-error, .errors, '
                                               + '[class*="error" i]:not(svg):not(path)')]
    .filter(e => visible(e) && (e.innerText || '').trim())
    .map(e => e.innerText.replace(/\s+/g, ' ').trim().slice(0, 120));
  const invalid = [...document.querySelectorAll('[aria-invalid=true]')].filter(visible).length;
  return [...new Set(texts)].concat(invalid ? [invalid + ' field(s) marked invalid'] : []);
}
"""


_CODE_PROMPT_RE = re.compile(r"(?i)\b(verification|security|confirmation) code\b|\benter the code\b|\bcode (was|we) sent\b")


def asks_for_emailed_code(page: Any, form: Optional[Any] = None) -> bool:
    """The site wants a code it emailed (Greenhouse does this after Submit sometimes). Only Vatsal can enter it."""
    for frame in [page.main_frame] + ([form] if form is not None and form is not page.main_frame else []):
        try:
            if _CODE_PROMPT_RE.search(frame.locator("body").inner_text(timeout=2000)):
                return True
        except Exception:
            continue
    return False


def visible_errors(form: Any) -> list[str]:
    """Error messages the form shows right now (checked before an auto-submit)."""
    try:
        return list(form.evaluate(VISIBLE_ERRORS_JS))
    except Exception:
        return ["helper couldn't read the form to check it for errors"]


def is_confirmation(page: Any, form: Optional[Any] = None, hosts: tuple[str, ...] = ()) -> bool:
    """The page, the frame that held the form, or an embedded ATS frame shows a confirmation."""
    frames = [page.main_frame] + ([form] if form is not None and form is not page.main_frame else [])
    frames += [f for f in page.frames if f not in frames and any(h in f.url for h in hosts)]
    for frame in frames:
        try:
            # Phenom careers sites (Disney, 2026-11-18) send a finished application to their "apply-thankyou" page
            # ("applythankyou" too), which /thank-you didn't match, so helper waited on after Vatsal submitted
            if re.search(r"/confirmation|/thanks\b|/thank-you\b", frame.url) or \
                    re.search(r"(?i)/(?:apply-?)?thank-?you/?$", urlparse(frame.url).path):
                return True
            if CONFIRMATION_RE.search(frame.locator("body").inner_text(timeout=2000)):
                return True
        except Exception:
            continue
    return False
