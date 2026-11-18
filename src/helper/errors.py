"""Exit codes and exceptions shared by every command."""

from __future__ import annotations

import re
import traceback
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_WAITING_FOR_CLAUDE = 3  # an input file was written; Claude must write the matching output file
EXIT_NEEDS_USER = 4  # paused for the user: eligibility blocker, CAPTCHA, unknown required field, ...
EXIT_STOPPED = 130  # Ctrl+C


class HelperError(Exception):
    """An expected failure with a message meant for the user. Exits with EXIT_ERROR."""

    exit_code = EXIT_ERROR


class WaitingForClaude(HelperError):
    """helper wrote an input file and needs Claude (this session) to write the output file."""

    exit_code = EXIT_WAITING_FOR_CLAUDE

    def __init__(self, run_id: str, input_file: str, output_file: str, note: str = "") -> None:
        self.run_id = run_id
        self.input_file = input_file
        self.output_file = output_file
        msg = (
            f"waiting for Claude: read {input_file} and write {output_file}, "
            f"then run `helper continue {run_id}`."
        )
        if note:
            msg += f"\n{note}"
        super().__init__(msg)


class NeedsUser(HelperError):
    """The run paused because only the user can decide or act."""

    exit_code = EXIT_NEEDS_USER


def problems(exc: Any) -> str:
    """A pydantic ValidationError as one short line per problem: where it is and what's wrong (no values echoed)."""
    lines = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ())) or "(the whole file)"
        kind = err.get("type", "")
        what = ("missing" if kind == "missing" else "not a setting helper knows (a typo?)" if kind == "extra_forbidden"
                else "not filled in" if err.get("input", ...) is None else err.get("msg", "invalid"))
        lines.append(f"  - {where}: {what}")
    return "\n".join(lines)


ERROR_LOG = Path.home() / ".helper" / "last_error.txt"
STOPPED = ("Stopped. Everything finished so far is saved: run the same command again, or `helper continue <run_id>` "
           "for a run, to go on.")


def explain(exc: BaseException) -> Optional[str]:
    """What went wrong, in plain words, for failures that aren't helper's own messages: a missing install, a busy
    browser profile or database, no internet, a slow site. None when helper doesn't recognize it (a bug)."""
    text, kind, module = str(exc), type(exc).__name__, type(exc).__module__ or ""
    if isinstance(exc, ModuleNotFoundError):
        return (f"The Python package '{exc.name}' isn't installed in helper's .venv. Run: "
                'uv pip install --python .venv/bin/python -e ".[dev]"')
    if "Executable doesn't exist" in text or "playwright install" in text:
        return "Playwright's Chromium isn't installed. Run: .venv/bin/playwright install chromium"
    if module.startswith("playwright"):
        if re.search(r"ProcessSingleton|SingletonLock|user data directory is already in use", text, re.I):
            return ("helper's browser profile is in use: another `helper fill` still has its window open, or one that "
                    "crashed left the profile locked. Close that Chromium window (or let its run finish) and try again.")
        if re.search(r"(Target page, context or browser|Browser) has been closed|browser has disconnected", text, re.I):
            return ("The browser window was closed before helper finished. Run the same command again to pick up where "
                    "it stopped.")
        if "Timeout" in kind or "Timeout" in text.split("\n")[0]:
            return (f"A page took too long to respond ({text.splitlines()[0][:160]}). The site may be slow or down; "
                    "try again in a minute.")
        if m := re.search(r"net::(ERR_[A-Z_]+)", text):  # ERR_NETWORK_CHANGED: Wi-Fi reconnecting (2026-11-17)
            return (f"The network dropped while loading a page ({m.group(1)}). Check the internet connection, then run "
                    "the same command again; nothing was submitted.")
    if kind == "OperationalError" and "locked" in text:
        return "helper.db is busy: another helper command is writing to it. Wait for it to finish, then try again."
    if module.startswith(("requests", "urllib3")) and kind in ("ConnectionError", "Timeout", "ConnectTimeout",
                                                                 "ReadTimeout", "SSLError", "ProxyError"):
        request = getattr(exc, "request", None)
        host = urlparse(getattr(request, "url", "") or "").hostname or "the site"
        return f"Couldn't reach {host}. Check your internet connection and try again."
    return None


def report_unexpected(exc: BaseException, log: Optional[Path] = None) -> str:
    """The message for an exception that escaped a command. The full traceback is saved for Claude to read."""
    log = log or ERROR_LOG
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("".join(traceback.format_exception(exc)), encoding="utf-8")
        details = f"The full details are in {log}."
    except OSError:
        details = "Run it again with HELPER_DEBUG=1 to see the full details."
    known = explain(exc)
    if known:
        return f"{known}\n({details})"
    first = (str(exc).splitlines() or [""])[0][:300]
    return (f"helper hit an unexpected error: {type(exc).__name__}: {first}\n"
            f"This is a bug in helper, not something you did. {details} Show them to Claude in your session, or run "
            "the command again with HELPER_DEBUG=1 to see them here.")
