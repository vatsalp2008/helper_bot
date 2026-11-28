"""Emailed codes and account links read from Gmail by helper (Vatsal, 2026-11-27): only the email this page asked
for, used once on that page, never shown."""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from helper import mailcodes
from helper.mailcodes import Request

NOW = datetime(2026, 11, 27, 21, 0, tzinfo=timezone.utc).timestamp()


def _mail(sender: str, subject: str, body: str, at: float = NOW + 30, html: str = "") -> bytes:
    msg = EmailMessage()
    msg["From"], msg["Subject"] = sender, subject
    msg["Date"] = format_datetime(datetime.fromtimestamp(at, timezone.utc))
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return bytes(msg)


GH = _mail("Greenhouse <no-reply@us.greenhouse-mail.io>", "Security code for your application to Acme",
           "Copy and paste this code into the security code field on your application: Ab3dE9fG After you enter the "
           "code, resubmit your application.")
ORACLE = _mail("egup.fa.sender.1@workflow.email.us-phoenix-1.ocs.oraclecloud.com", "Confirm your identity",
               "Before you get started. You must confirm your identity using the one-time pass code : 188450 Note : "
               "This code will expire in 10 minutes. Regards, Vertiv Talent Acquisition Team")
JPM = _mail("JPMorgan Chase & Co. Human Resources <eino.fa.sender@workflow.mail.us2.cloud.oracle.com>",
            "Take action to confirm your identity",
            "Vatsal, Please use this code to confirm your identity on our careers site: 482913 This verification code "
            "will expire in 10 minutes.")
WD = _mail("Postman <people-support@postman.com>", "Verify your candidate account", "Click this link",
           html='<p>Click this link to confirm your email address</p><a href="https://postman.wd5.myworkdayjobs.com/'
                'careers/activate/tok123?redirect=%2Fjob&amp;x=1">Verify</a>')


@pytest.mark.parametrize("raw, req, expected", [
    (GH, Request("greenhouse", NOW, "job-boards.greenhouse.io", "Acme Inc."), "Ab3dE9fG"),
    (GH, Request("greenhouse", NOW, "job-boards.greenhouse.io", "Globex"), None),  # another company's code
    (ORACLE, Request("oracle", NOW, "egup.fa.us6.oraclecloud.com", "Vertiv"), "188450"),  # by its tenant
    (ORACLE, Request("oracle", NOW, "ibwujb.fa.us2.oraclecloud.com", "Ralliant"), None),  # another tenant
    (JPM, Request("oracle", NOW, "jpmc.fa.oraclecloud.com", "JPMorgan Chase"), "482913"),  # by the company's name
    (WD, Request("workday", NOW, "postman.wd5.myworkdayjobs.com", "Postman"),
     "https://postman.wd5.myworkdayjobs.com/careers/activate/tok123?redirect=%2Fjob&x=1"),
    (WD, Request("workday", NOW, "evil.wd5.myworkdayjobs.com", "Postman"), None),  # only this window's own site
    (GH, Request("greenhouse", NOW + 600, "job-boards.greenhouse.io", "Acme"), None),  # older than the page's ask
    (GH, Request("oracle", NOW, "egup.fa.us6.oraclecloud.com", "Acme"), None),  # the wrong kind of email
])
def test_only_the_email_this_page_asked_for_counts(raw: bytes, req: Request, expected: Any) -> None:
    assert mailcodes.match(raw, req) == expected


class Source:
    def __init__(self, batches: list[list[bytes]]) -> None:
        self.batches, self.closed = batches, False

    def recent(self) -> list[bytes]:
        return self.batches.pop(0) if len(self.batches) > 1 else self.batches[0]

    def close(self) -> None:
        self.closed = True


def test_helper_waits_for_the_email_and_takes_the_newest_code() -> None:
    resent = _mail("egup.fa.sender.1@x.oraclecloud.com", "Confirm your identity",
                   "confirm your identity using the one-time pass code : 777111", at=NOW + 90)
    source = Source([[], [ORACLE, resent]])
    clock = {"t": NOW}
    got = mailcodes.wait_for(Request("oracle", NOW, "egup.fa.us6.oraclecloud.com", "Vertiv"), source,
                             sleep=lambda s: clock.update(t=clock["t"] + s), now=lambda: clock["t"])
    assert got == "777111" and source.closed
    none = Source([[]])
    assert mailcodes.wait_for(Request("oracle", NOW, "egup.fa.us6.oraclecloud.com"), none, timeout=12,
                              sleep=lambda s: clock.update(t=clock["t"] + s), now=lambda: clock["t"]) is None


def test_without_gmail_settings_there_is_no_fetcher(repo: Any) -> None:
    from helper.config import Paths

    assert mailcodes.fetcher(Paths(repo), "Acme") is None


@pytest.fixture
def page() -> Iterator[Any]:
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium isn't installed: {exc}")
        yield browser.new_context().new_page()
        browser.close()


def test_a_code_goes_into_one_box_or_one_character_per_box(page: Any) -> None:
    page.set_content('<p>Enter the security code</p><input id="name"><input id="security-code">')
    assert mailcodes.enter_code(page.main_frame, "Ab3dE9fG")
    assert (page.input_value("#security-code"), page.input_value("#name")) == ("Ab3dE9fG", "")
    page.set_content("".join(f'<input class="pin-code-input__input" maxlength="1" id="b{i}">' for i in range(6)))
    assert mailcodes.enter_code(page.main_frame, "188450")
    assert "".join(page.input_value(f"#b{i}") for i in range(6)) == "188450"
    page.set_content('<input id="first_name"><input id="email">')
    assert not mailcodes.enter_code(page.main_frame, "188450")  # no code box: nothing is typed anywhere


def test_greenhouse_s_code_at_submit_is_typed_in_and_the_held_submit_is_not_logged_as_sent(page: Any) -> None:
    from helper import apply as ap
    from helper.adapters import generic, greenhouse

    page.set_content("<h2>Enter the verification code we sent to your email</h2><input id='security-input'>")
    asked: list[str] = []
    fetch = lambda kind, since, host: asked.append(kind) or "Ab3dE9fG"  # noqa: E731
    assert not ap._enter_emailed_code(page, None, generic, fetch)  # only Greenhouse's code email is known
    assert ap._enter_emailed_code(page, None, greenhouse, fetch)
    assert asked == ["greenhouse"] and page.input_value("#security-input") == "Ab3dE9fG"
    # helper clicked Submit, and the site still asks for the code: nothing was sent, so it's a timeout, not
    # submitted_unverified (rule 10's unclear submit)
    outcome = ap._wait_for_submit(page, greenhouse, {"value": True}, 0.05, 0.3, None, from_email=None)
    assert outcome == "timeout"
