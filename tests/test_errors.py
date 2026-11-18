"""Clear error messages (milestone 11): helper's own messages, plain explanations for everything else, and a saved
traceback for real bugs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests
import typer
from pydantic import ValidationError

from helper import errors
from helper.cli import guarded
from helper.errors import HelperError, explain, problems, report_unexpected
from helper.models import Profile


def test_settings_problems_are_one_line_each() -> None:
    with pytest.raises(ValidationError) as caught:
        Profile.model_validate({"tier1": {"first_name": 3}, "colour": "blue"})
    text = problems(caught.value)
    assert "  - header: missing" in text
    assert "  - tier1.first_name: Input should be a valid string" in text
    assert "  - colour: not a setting helper knows (a typo?)" in text
    assert "pydantic.dev" not in text and "input_value" not in text  # no noise, and no values echoed back
    with pytest.raises(ValidationError) as caught:
        Profile.model_validate({"header": {"name": None}})
    assert "  - header.name: not filled in" in problems(caught.value)


class PlaywrightError(Exception):
    """Stands in for playwright._impl._errors.Error / TimeoutError (matched by module, as the real ones are)."""


PlaywrightError.__module__ = "playwright._impl._errors"


@pytest.mark.parametrize("exc,expected", [
    (ModuleNotFoundError("No module named 'playwright'", name="playwright"), "'playwright' isn't installed"),
    (PlaywrightError("BrowserType.launch: Executable doesn't exist at /x/chrome\n Please run playwright install"),
     ".venv/bin/playwright install chromium"),
    (PlaywrightError("BrowserType.launch_persistent_context: Failed to create a ProcessSingleton for your profile"),
     "browser profile is in use"),
    (PlaywrightError("Page.click: Target page, context or browser has been closed"), "window was closed"),
    (type("TimeoutError", (PlaywrightError,), {"__module__": PlaywrightError.__module__})(
        "Page.goto: Timeout 45000ms exceeded."), "took too long"),
    (sqlite3.OperationalError("database is locked"), "helper.db is busy"),
    (requests.ConnectionError("boom", request=requests.Request("GET", "https://api.lever.co/v0/postings").prepare()),
     "Couldn't reach api.lever.co. Check your internet connection"),
])
def test_common_failures_are_explained(exc: BaseException, expected: str) -> None:
    assert expected in (explain(exc) or "")


def test_a_bug_says_so_and_saves_the_details(tmp_path: Path) -> None:
    log = tmp_path / "last_error.txt"
    try:
        {}["missing"]
    except KeyError as exc:
        message = report_unexpected(exc, log)
    assert message.startswith("helper hit an unexpected error: KeyError: 'missing'")
    assert "This is a bug in helper, not something you did" in message and str(log) in message
    assert "Traceback (most recent call last)" in log.read_text(encoding="utf-8")
    assert explain(KeyError("x")) is None


def test_every_command_turns_failures_into_messages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(errors, "ERROR_LOG", tmp_path / "last_error.txt")
    monkeypatch.delenv("HELPER_DEBUG", raising=False)

    def run(exc: BaseException) -> tuple[int, str]:
        def command() -> None:
            raise exc

        with pytest.raises(typer.Exit) as stop:
            guarded(command)()
        return stop.value.exit_code, capsys.readouterr().err

    assert run(HelperError("Missing file: data/x.yaml.")) == (1, "Missing file: data/x.yaml.\n")
    code, err = run(KeyboardInterrupt())
    assert code == 130 and err.startswith("Stopped. Everything finished so far is saved")
    code, err = run(sqlite3.OperationalError("database is locked"))
    assert code == 1 and "helper.db is busy" in err and "last_error.txt" in err
    code, err = run(ZeroDivisionError("division by zero"))
    assert code == 1 and "unexpected error: ZeroDivisionError" in err
    assert run(typer.Exit(4))[0] == 4  # a command's own exit code passes through untouched

    monkeypatch.setenv("HELPER_DEBUG", "1")  # shows the real traceback instead
    with pytest.raises(ZeroDivisionError):
        guarded(lambda: 1 / 0)()


def test_a_missing_profile_says_how_to_make_one(tmp_path: Path) -> None:
    from helper.config import load_yaml

    with pytest.raises(HelperError, match="Run `helper setup` to create it from data/profile.example.yaml"):
        load_yaml(tmp_path / "profile.yaml")


def test_a_dropped_network_is_explained_not_called_a_bug() -> None:  # Samsung, ERR_NETWORK_CHANGED, 2026-11-17
    from helper.errors import explain

    message = explain(PlaywrightError("Page.goto: net::ERR_NETWORK_CHANGED at https://sec.wd3.myworkdayjobs.com/x"))
    assert message and "ERR_NETWORK_CHANGED" in message and "nothing was submitted" in message
