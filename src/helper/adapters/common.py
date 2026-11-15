"""Pieces every adapter shares: field results, finding the form (also inside an embedding frame), confirmations."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Optional

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


def is_confirmation(page: Any, form: Optional[Any] = None, hosts: tuple[str, ...] = ()) -> bool:
    """The page, the frame that held the form, or an embedded ATS frame shows a confirmation."""
    frames = [page.main_frame] + ([form] if form is not None and form is not page.main_frame else [])
    frames += [f for f in page.frames if f not in frames and any(h in f.url for h in hosts)]
    for frame in frames:
        try:
            if re.search(r"/confirmation|/thanks\b|/thank-you\b", frame.url):
                return True
            if CONFIRMATION_RE.search(frame.locator("body").inner_text(timeout=2000)):
                return True
        except Exception:
            continue
    return False
