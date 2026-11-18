"""The browser helper fills forms in (spec 10.1): Playwright Chromium with its own persistent profile, headed by
default and separate from your everyday Chrome. Small human-like pauses; no stealth plugins or detection evasion."""

from __future__ import annotations

import json
import os
import random
import re
import socket
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


PROFILE_SLOTS = 6
_CLAIM = "helper-fill.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _in_use(profile: Path) -> bool:
    """Another helper fill or Chrome has this profile open. Locks left by a crashed run are cleared."""
    claim = profile / _CLAIM
    try:
        if _alive(int(claim.read_text(encoding="utf-8").strip())):
            return True
        claim.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    try:
        host, _, pid = os.readlink(profile / "SingletonLock").rpartition("-")
    except OSError:
        return False
    if host != socket.gethostname() or not pid.isdigit() or _alive(int(pid)):
        return True
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):  # Chrome's lock from a crashed run
        (profile / name).unlink(missing_ok=True)
    return False


def claim_profile(base: Path, slots: int = PROFILE_SLOTS) -> Path:
    """helper's browser profile, or the next numbered one (browser-profile-2, -3...) when other fills have it open,
    so several applications can fill side by side, each in its own window. Claimed for this process. `slots` 0 means
    no limit (Vatsal, 2026-11-16: the job hunt keeps stuck windows open and still starts new ones)."""
    import itertools

    from .errors import HelperError

    for i in (itertools.count() if slots == 0 else range(slots)):
        profile = base if i == 0 else base.with_name(f"{base.name}-{i + 1}")
        profile.mkdir(parents=True, exist_ok=True)
        if _in_use(profile):
            continue
        try:  # atomic: two fills starting together can't take the same profile
            fd = os.open(profile / _CLAIM, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return profile
    raise HelperError(f"{slots} helper fills are already open. Finish or close one, then try again.")


def release_profile(profile: Path) -> None:
    try:
        if (profile / _CLAIM).read_text(encoding="utf-8").strip() == str(os.getpid()):
            (profile / _CLAIM).unlink()
    except (OSError, ValueError):
        pass


# Windows behind others, or with the display asleep, get throttled by Chrome until clicks time out after 30 seconds
# (Scout, Samsung, Little Caesars on an unattended evening, 2026-11-17). These keep each window running at full speed;
# they change nothing a site can see about the browser.
KEEP_ACTIVE_ARGS = ("--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling")


@contextmanager
def browser_session(paths: Paths, cfg: Config, *, headless: Optional[bool] = None) -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    profile = claim_profile(paths.browser_profile(cfg), cfg.max_browser_windows)
    try:
        no_password_saving(profile)
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(profile), headless=cfg.headless if headless is None else headless,
                viewport={"width": 1280, "height": 900}, args=list(KEEP_ACTIVE_ARGS),
            )
            try:
                yield context
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    finally:
        release_profile(profile)


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
