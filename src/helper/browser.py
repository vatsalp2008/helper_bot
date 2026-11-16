"""The browser helper fills forms in (spec 10.1): Playwright Chromium with its own persistent profile, headed by
default and separate from your everyday Chrome. Small human-like pauses; no stealth plugins or detection evasion."""

from __future__ import annotations

import json
import random
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from .config import Paths
from .models import Config

# Visible challenge frames. An invisible reCAPTCHA badge is not a challenge.
CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha/api2/bframe']",
    "iframe[src*='recaptcha/enterprise/bframe']",
    "iframe[src*='hcaptcha.com/captcha']",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='challenge' i]",
)


@contextmanager
def browser_session(paths: Paths, cfg: Config, *, headless: Optional[bool] = None) -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    profile = paths.browser_profile(cfg)
    profile.mkdir(parents=True, exist_ok=True)
    no_password_saving(profile)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile), headless=cfg.headless if headless is None else headless, viewport={"width": 1280, "height": 900},
        )
        try:
            yield context
        finally:
            try:
                context.close()
            except Exception:
                pass


def no_password_saving(profile: Path) -> None:
    """helper's browser never offers to save a password: the Workday login lives in .env only, and a "Save
    password?" bubble would sit over the form. Chrome reads these settings when it starts."""
    prefs_path = profile / "Default" / "Preferences"
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.is_file() else {}
    except (OSError, ValueError):
        return
    if prefs.get("credentials_enable_service") is False and prefs.get("profile", {}).get("password_manager_enabled") is False:
        return
    prefs["credentials_enable_service"] = False
    prefs.setdefault("profile", {})["password_manager_enabled"] = False
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")


def make_pause(cfg: Config, sleep: Callable[[float], None] = time.sleep) -> Callable[[], None]:
    lo, hi = cfg.pause_ms
    return lambda: sleep(random.uniform(lo, hi) / 1000)


_CHALLENGE_URL_RE = re.compile(r"recaptcha/(api2|enterprise)/bframe|hcaptcha\.com/captcha/.*frame=challenge|"
                                r"challenges\.cloudflare\.com", re.I)


def captcha_visible(page: Any) -> bool:
    """A CAPTCHA challenge showing anywhere on the page, including inside an embedded application frame (iCIMS puts
    its hCaptcha inside its own frame)."""
    for selector in CAPTCHA_SELECTORS:
        frames = page.locator(selector)
        for i in range(frames.count()):
            try:
                frame = frames.nth(i)
                box = frame.bounding_box()
                if box and box["width"] > 60 and box["height"] > 60 and frame.is_visible():
                    return True
            except Exception:
                continue
    for frame in getattr(page, "frames", []):
        if frame is page.main_frame or not _CHALLENGE_URL_RE.search(frame.url or ""):
            continue
        try:
            element = frame.frame_element()
            box = element.bounding_box()
            if box and box["width"] > 60 and box["height"] > 60 and element.is_visible():
                return True
        except Exception:
            continue
    return False
