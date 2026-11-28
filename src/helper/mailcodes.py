"""Emailed codes and account links, read by helper itself (Vatsal, 2026-11-27).

Sites send a one-time code or an account link to the application email: Oracle's "Confirm your identity" code,
Greenhouse's security code at Submit, and Workday's "Verify your candidate account" link. helper waits for that email in
Gmail (read-only, the app password `helper inbox` uses), takes the code or the link, and uses it once, only on the page
that asked for it:
- the email has to arrive after the page asked (with two minutes' grace for clocks);
- it has to come from that site's own mail for this company: Oracle by its tenant (egup.fa.sender... for the careers
  site at egup.fa...oraclecloud.com) or the company's name, Greenhouse by the company in its subject, and Workday by the
  link's host being the window's own host;
- a code is never printed, logged, saved, or shown to Claude.
CAPTCHAs stay Vatsal's (rule 6). Without GMAIL_* in .env, or when no email comes, the window waits for him as before.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol
from urllib.parse import urlparse

from .config import Paths, env_value

GRACE = 120  # seconds: an email dated a little before the page asked still counts
WAIT = 240  # seconds to wait for the email
POLL = 6
KINDS = ("oracle", "greenhouse", "workday")


@dataclass
class Request:
    kind: str  # oracle | greenhouse | workday
    since: float  # when the page asked (epoch seconds)
    host: str  # the window's host
    company: str = ""


class Recent(Protocol):
    def recent(self) -> list[bytes]: ...  # whole messages from the last hour, oldest first
    def close(self) -> None: ...


def _when(msg: Any) -> Optional[float]:
    try:
        return email.utils.parsedate_to_datetime(str(msg.get("Date", ""))).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def _texts(msg: Any) -> tuple[str, str]:
    """(plain text, html) of a message."""
    plain, markup = "", ""
    for part in msg.walk():
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, AssertionError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
        if kind == "text/plain":
            plain += content + "\n"
        else:
            markup += content + "\n"
    return plain, markup


def _words(plain: str, markup: str) -> str:
    from .htmltext import html_to_text

    text = plain if len(plain.strip()) >= 40 else (html_to_text(markup) if markup else plain)
    return re.sub(r"\s+", " ", text)


def links(plain: str, markup: str) -> list[str]:
    found = [html.unescape(u) for u in re.findall(r"""href\s*=\s*["'](https?://[^"']+)["']""", markup, re.I)]
    found += re.findall(r"https?://[^\s<>\"')\]]+", plain)
    return found


def _names(company: str, text: str) -> bool:
    from .inbox import _norm, company_keys

    hay = _norm(text)
    return any(f" {key} " in hay for key in company_keys(company))


def match(raw: bytes, req: Request) -> Optional[str]:
    """The code (Oracle, Greenhouse) or the link (Workday) in this email, if it's the one this page asked for."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    when = _when(msg)
    if when is None or when < req.since - GRACE:
        return None
    name, addr = email.utils.parseaddr(str(msg.get("From", "")))
    addr = addr.lower()
    domain = addr.rpartition("@")[2]
    subject = re.sub(r"\s+", " ", str(msg.get("Subject", "")))
    plain, markup = _texts(msg)
    text = _words(plain, markup)
    if req.kind == "greenhouse":
        if not (domain.endswith("greenhouse-mail.io") or domain.endswith("greenhouse.io")) \
                or not re.search(r"(?i)security code", subject):
            return None
        if req.company and not _names(req.company, subject):
            return None
        m = re.search(r"(?i)security code field on your application:?\s*([A-Za-z0-9]{6,12})\b", text)
        return m.group(1) if m else None
    if req.kind == "oracle":
        if "oraclecloud.com" not in domain and not domain.endswith("oracle.com"):
            return None
        if not re.search(r"(?i)identity|verification code|pass ?code", f"{subject} {text}"):
            return None
        tenant = req.host.lower().split(".")[0]
        if not tenant or addr.split("@")[0].split(".")[0] != tenant:
            if not (req.company and _names(req.company, f"{name} {text}")):
                return None
        m = re.search(r"(?i)(?:pass ?code|verification code|confirm your identity[^:]{0,80})\s*:?\s*(\d{4,8})\b", text)
        return m.group(1) if m else None
    if req.kind == "workday":
        if not re.search(r"(?i)verify|activate|confirm", subject):
            return None
        for url in links(plain, markup):
            parsed = urlparse(url)
            if (parsed.hostname or "").lower() == req.host.lower() and "/activate/" in parsed.path:
                return url
        return None
    return None


def wait_for(req: Request, source: Recent, *, timeout: float = WAIT, poll: float = POLL,
             sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time) -> Optional[str]:
    """The newest matching code or link, waiting up to `timeout` seconds for its email."""
    deadline = now() + timeout
    try:
        while True:
            hits = [found for raw in source.recent() if (found := match(raw, req))]
            if hits:
                return hits[-1]  # a resent code replaces the first
            if now() >= deadline:
                return None
            sleep(poll)
    finally:
        source.close()


class GmailRecent:
    """The last hour of mail, read-only. New mail shows after NOOP; messages already read are kept."""

    def __init__(self, gmail: Any) -> None:
        self.gmail = gmail
        self.cache: dict[str, bytes] = {}

    def recent(self) -> list[bytes]:
        imap = self.gmail.imap
        imap.noop()
        typ, data = imap.uid("SEARCH", "X-GM-RAW", '"newer_than:1h"')
        uids = [u.decode() for u in (data[0].split() if typ == "OK" and data and data[0] else [])][-60:]
        for uid in uids:
            if uid not in self.cache:
                self.cache[uid] = self.gmail.message(uid)
        return [self.cache[u] for u in uids]

    def close(self) -> None:
        self.gmail.close()


Fetch = Callable[[str, float, str], Optional[str]]


def fetcher(paths: Paths, company: str, say: Callable[[str], None] = lambda s: None) -> Optional[Fetch]:
    """`get(kind, since, host)` -> the code or link, or None. None when Gmail isn't set up in .env."""
    if not (env_value(paths, "GMAIL_ADDRESS") and env_value(paths, "GMAIL_APP_PASSWORD")):
        return None

    def get(kind: str, since: float, host: str) -> Optional[str]:
        from .errors import HelperError
        from .inbox import gmail_from_env

        try:
            gmail, _ = gmail_from_env(paths)
        except HelperError as exc:
            say(f"helper couldn't read Gmail for the emailed code ({exc}).")
            return None
        return wait_for(Request(kind=kind, since=since, host=host, company=company), GmailRecent(gmail))

    return get


# --------------------------------------------------------------------------- typing a code into the page

_CODE_INPUTS_JS = r"""
() => {
  const shown = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const words = /code|pin|otp|security|verif|passcode|one-?time/i;
  document.querySelectorAll('[data-helper-code]').forEach(e => e.removeAttribute('data-helper-code'));
  const boxes = [...document.querySelectorAll('input')].filter(e => shown(e) && !e.disabled && !e.readOnly
    && ['text', 'tel', 'number', 'password', ''].includes((e.getAttribute('type') || '').toLowerCase())
    && words.test([e.id, e.name, e.className, e.getAttribute('aria-label'), e.placeholder, e.autocomplete,
                   e.labels && e.labels[0] ? e.labels[0].innerText : ''].join(' ')));
  boxes.forEach((e, i) => e.setAttribute('data-helper-code', String(i)));
  return boxes.map(e => e.maxLength > 0 ? e.maxLength : 0);
}
"""


def enter_code(frame: Any, code: str) -> bool:
    """Types the code into the page's code box, or one character per box (Oracle's six digit boxes). True when the
    boxes hold exactly the code afterwards."""
    try:
        sizes = list(frame.evaluate(_CODE_INPUTS_JS))
    except Exception:
        return False
    box = lambda i: frame.locator(f"[data-helper-code='{i}']").first  # noqa: E731
    try:
        if len(sizes) == 1 and (sizes[0] == 0 or sizes[0] >= len(code)):
            box(0).click(timeout=5000)
            box(0).fill("")
            box(0).press_sequentially(code, delay=40)
            got = box(0).input_value(timeout=2000)
        elif len(sizes) == len(code):
            for i, ch in enumerate(code):
                box(i).click(timeout=5000)
                box(i).fill("")
                box(i).press_sequentially(ch, delay=40)
            got = "".join(box(i).input_value(timeout=2000) for i in range(len(code)))
        else:
            return False
    except Exception:
        # Oracle's fixture moves on once six digits are in, so the boxes can be gone by now: that counts as taken
        try:
            return not frame.locator("[data-helper-code]").count()
        except Exception:
            return False
    return got == code
