"""The browser helper fills forms in (spec 10.1): Playwright Chromium with its own persistent profile, headed by
default and separate from your everyday Chrome. Small human-like pauses; no stealth plugins or detection evasion."""

from __future__ import annotations

import random
import time
from contextlib import contextmanager
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


def make_pause(cfg: Config, sleep: Callable[[float], None] = time.sleep) -> Callable[[], None]:
    lo, hi = cfg.pause_ms
    return lambda: sleep(random.uniform(lo, hi) / 1000)


def captcha_visible(page: Any) -> bool:
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
    return False
