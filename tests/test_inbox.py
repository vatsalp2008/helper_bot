"""helper inbox (Vatsal, 2026-11-27): application emails read-only from Gmail, Claude says what each is, helper changes
statuses by fixed rules after Vatsal's OK."""

from __future__ import annotations

import imaplib
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from helper import inbox
from helper.config import Paths, load_config
from helper.errors import HelperError, NeedsUser, WaitingForClaude
from helper.runs import read_json, run_dir, write_json
from helper.tracker import queue
from helper.tracker import repo as apps
from helper.tracker.commands import open_db

OWN = "vatsal.test@gmail.com"


def _mail(sender: str, subject: str, body: str, when: str = "Mon, 24 Nov 2026 10:00:00 -0500", mid: str = "",
          html: str = "") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = OWN
    msg["Subject"] = subject
    msg["Date"] = when
    msg["Message-ID"] = mid or f"<{abs(hash((sender, subject)))}@example.com>"
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return bytes(msg)


class FakeSource:
    def __init__(self, mails: list[bytes]) -> None:
        self.mails = {str(i): raw for i, raw in enumerate(mails, 1)}
        self.bodies: list[str] = []

    def headers(self, since: date) -> list[tuple[str, bytes]]:
        return [(uid, raw.split(b"\n\n", 1)[0] + b"\n\n") for uid, raw in self.mails.items()]

    def message(self, uid: str) -> bytes:
        self.bodies.append(uid)
        return self.mails[uid]

    def close(self) -> None:
        pass


MAILS = [
    _mail("Acme Recruiting <no-reply@us.greenhouse-mail.io>", "Your application to Acme",
          "Hi Vatsal, thank you for your interest in the Software Engineer Intern role at Acme. Unfortunately we will "
          "not be moving forward.", mid="<reject@acme>"),
    _mail("Globex Talent <talent@globex.com>", "Globex next steps: online assessment",
          "Please complete the HackerRank assessment for Data Engineer, New Grad by Dec 3, 2026.",
          when="Tue, 25 Nov 2026 09:00:00 -0500", mid="<oa@globex>"),
    _mail("Globex <auto-confirm@globex.com>", "Your order has shipped", "Your Globex order is on its way.",
          mid="<order@globex>"),  # the company's shop, not its recruiting: left out
    _mail("Workday <initech@myworkday.com>", "Verify your email to continue your application",
          "Your verification code is 123456.", mid="<code@initech>"),  # codes are Vatsal's: left out
    _mail("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>", "30 new jobs for software engineer", "...",
          mid="<alert@li>"),
    _mail(f"Vatsal <{OWN}>", "Re: your application", "Thanks!", mid="<own@me>"),  # his own mail
    _mail("Mom <mom@example.com>", "Dinner Sunday?", "Call me.", mid="<mom>"),
    _mail("Initech Careers <careers@initech.com>", "Thank you for applying to Initech",
          "We received your application for Backend Engineer Intern.", mid="<received@initech>"),
]


def _setup(repo_root: Path) -> tuple[Paths, Any, dict[str, int]]:
    paths = Paths(repo_root)
    cfg = load_config(paths)
    conn = open_db(paths, cfg)
    ids = {
        "acme": apps.insert(conn, company="Acme Inc.", role_title="Software Engineer Intern", status="applied",
                            date_applied="2026-11-20", job_url="https://job-boards.greenhouse.io/acme/jobs/1"),
        "globex": apps.insert(conn, company="Globex Corporation", role_title="Data Engineer, New Grad",
                              status="applied", date_applied="2026-11-18"),
        "initech": apps.insert(conn, company="Initech", role_title="Backend Engineer Intern", status="filled",
                               run_id="r-initech"),
        "hooli": apps.insert(conn, company="Hooli", role_title="ML Engineer", status="interview",
                             date_applied="2026-10-01"),  # set by hand: never changed
    }
    conn.commit()
    conn.close()
    return paths, cfg, ids


def _start(paths: Paths, cfg: Any, source: FakeSource) -> str:
    with pytest.raises(WaitingForClaude) as waiting:
        inbox.start(paths, cfg, since=date(2026, 11, 20), source=source, own=OWN, now=datetime(2026, 11, 27, 20, 0))
    return waiting.value.run_id


def test_only_application_mail_reaches_claude_and_codes_stay_out(repo: Path) -> None:
    paths, cfg, ids = _setup(repo)
    source = FakeSource(MAILS)
    run_id = _start(paths, cfg, source)
    inp = read_json(run_dir(paths, run_id) / inbox.INPUT)
    by_id = {e["message_id"]: e for e in inp["emails"]}
    assert set(by_id) == {"<reject@acme>", "<oa@globex>", "<received@initech>"}
    assert [c["application_id"] for c in by_id["<reject@acme>"]["candidates"]] == [ids["acme"]]
    assert by_id["<oa@globex>"]["candidates"][0]["application_id"] == ids["globex"]
    assert "Unfortunately" in by_id["<reject@acme>"]["body"]
    assert "123456" not in str(inp)  # the verification email isn't in Claude's file, nor its code
    assert inp["left_out"]["sign-in or verification emails (yours)"] == 1
    assert inp["left_out"]["job alerts and surveys"] == 1
    assert set(source.bodies) == {"1", "2", "8"}  # only kept mail is read in full


def _answers(paths: Paths, run_id: str, ids: dict[str, int], **extra: Any) -> None:
    write_json(run_dir(paths, run_id) / inbox.OUTPUT, {"emails": [
        {"message_id": "<reject@acme>", "category": "rejection", "application_id": ids["acme"],
         "summary": "Acme is not moving forward."},
        {"message_id": "<oa@globex>", "category": "oa", "application_id": ids["globex"],
         "summary": "Globex sent a HackerRank assessment.", "action": "Take the HackerRank assessment.",
         "deadline": "2026-12-03"},
        {"message_id": "<received@initech>", "category": "received", "application_id": ids["initech"],
         "summary": "Initech received the application.", **extra},
    ]})


def test_changes_wait_for_vatsal_then_reach_the_tracker_and_the_list(repo: Path) -> None:
    paths, cfg, ids = _setup(repo)
    run_id = _start(paths, cfg, FakeSource(MAILS))
    _answers(paths, run_id, ids)

    with pytest.raises(NeedsUser) as paused:
        inbox.continue_inbox(paths, cfg, run_id)
    text = str(paused.value)
    assert f"#{ids['acme']} Acme Inc., Software Engineer Intern: applied -> rejected" in text
    assert f"#{ids['globex']} Globex Corporation, Data Engineer, New Grad: applied -> oa" in text
    assert f"#{ids['initech']} Initech, Backend Engineer Intern: filled -> applied" in text
    assert "Take the HackerRank assessment (by 2026-12-03). Email Nov 25" in text
    assert f"--approve-inbox" in text
    conn = open_db(paths, cfg)
    assert apps.get(conn, ids["acme"])["status"] == "applied"  # nothing changed before the OK
    conn.close()

    out = inbox.continue_inbox(paths, cfg, run_id, approve=True)
    assert "Applied." in out
    conn = open_db(paths, cfg)
    try:
        assert apps.get(conn, ids["acme"])["status"] == "rejected"
        assert apps.get(conn, ids["globex"])["status"] == "oa"
        initech = apps.get(conn, ids["initech"])
        assert (initech["status"], initech["date_applied"]) == ("applied", "2026-11-24")  # the email's day
        assert apps.history(conn, ids["acme"])[-1]["note"].startswith("email: Acme is not moving forward.")
        todos = queue.page_doc(conn)["inbox"]
        assert [(t["company"], t["action"], t["deadline"]) for t in todos] == [
            ("Globex Corporation", "Take the HackerRank assessment.", "2026-12-03")]
        assert "From your inbox (1)" in queue.render_markdown(conn)
        inbox.mark_done(conn, todos[0]["id"])
        assert queue.page_doc(conn)["inbox"] == []
    finally:
        conn.close()
    assert "already applied" in inbox.continue_inbox(paths, cfg, run_id, approve=True)

    # the next check skips mail already checked
    msg = inbox.start(paths, cfg, source=FakeSource(MAILS), own=OWN, now=datetime(2026, 11, 28, 2, 0))
    assert msg.startswith("No new application emails since 2026-11-26")


def test_auto_review_applies_at_once(repo: Path) -> None:
    paths, cfg, ids = _setup(repo)
    cfg = cfg.model_copy(update={"inbox_review": "auto"})
    run_id = _start(paths, cfg, FakeSource(MAILS))
    _answers(paths, run_id, ids)
    inbox.continue_inbox(paths, cfg, run_id)
    conn = open_db(paths, cfg)
    try:
        assert apps.get(conn, ids["acme"])["status"] == "rejected"
    finally:
        conn.close()


def test_claudes_answers_are_checked(repo: Path) -> None:
    paths, cfg, ids = _setup(repo)
    run_id = _start(paths, cfg, FakeSource(MAILS))
    write_json(run_dir(paths, run_id) / inbox.OUTPUT, {"emails": [
        {"message_id": "<reject@acme>", "category": "ghosted", "application_id": ids["acme"], "summary": "x"},
        {"message_id": "<oa@globex>", "category": "oa", "application_id": ids["hooli"],
         "summary": "Globex sent an assessment — soon.", "deadline": "Dec 3"},
        {"message_id": "<oa@globex>", "category": "other", "application_id": None, "summary": "dup"},
    ]})
    with pytest.raises(HelperError) as rejected:
        inbox.continue_inbox(paths, cfg, run_id)
    text = str(rejected.value)
    assert "category 'ghosted'" in text
    assert f"application #{ids['hooli']} (Hooli) isn't a candidate" in text
    assert "needs an action" in text
    assert "em dash" in text
    assert "deadline 'Dec 3'" in text
    assert "has 2 entries" in text
    assert "no entry for the email 'Thank you for applying to Initech'" in text
    assert read_json(run_dir(paths, run_id) / inbox.VALIDATION)["ok"] is False


@pytest.mark.parametrize("current, set_by, category, expected", [
    ("applied", "helper", "rejection", "rejected"),
    ("applied", "you (Notion)", "interview", "interview"),
    ("submitted_unverified", "helper", "received", "applied"),
    ("filled", "helper", "oa", "oa"),
    ("applied", "helper", "received", None),            # already sent
    ("interview", "you", "rejection", None),            # set by hand: never changed
    ("oa", "email", "interview", "interview"),          # an earlier email's status moves forward
    ("interview", "email", "oa", None),                 # never backwards
    ("oa", "email", "rejection", "rejected"),
    ("offer", "email", "rejection", None),
    ("rejected", "email", "interview", None),
    ("skipped", "helper", "rejection", None),
    ("applied", "helper", "next_step", None),
    ("applied", "helper", "other", None),
])
def test_status_rules(current: str, set_by: str, category: str, expected: Any) -> None:
    assert inbox.decide(current, set_by, category)[0] == expected


def test_company_names_match_without_inc_or_punctuation() -> None:
    assert inbox.company_key("The Trade Desk, Inc.") == "trade desk"
    assert inbox.company_key("AT&T") == "at and t"
    index = {"capital one": ["row"], "trade desk": ["row"]}
    assert inbox.companies_in("Update from Capital One Recruiting", index) == {"capital one"}
    assert inbox.companies_in("", index, {"email", "capitalone"}) == {"capital one"}
    assert inbox.companies_in("Tradedesk fans", index) == set()


def test_html_only_mail_is_read_as_text() -> None:
    raw = _mail("A <a@b.com>", "s", "", html="<p>We regret to inform you</p><p>Thanks</p>")
    assert "We regret to inform you" in inbox.body_text(raw)


class FakeImap:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail_login = False

    def login(self, user: str, password: str) -> None:
        if self.fail_login:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")

    def select(self, box: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", box, readonly))
        return "OK", [b"3"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        self.calls.append(("uid", command) + args)
        if command == "SEARCH":
            return "OK", [b"7 9"]
        return "OK", [(b"1 (UID 7 BODY[HEADER.FIELDS (FROM)] {20}", b"From: a@b.com\r\n\r\n"), b")",
                      (b"2 (UID 9 BODY[HEADER.FIELDS (FROM)] {20}", b"From: c@d.com\r\n\r\n"), b")"]

    def logout(self) -> None:
        pass


def test_gmail_is_opened_read_only_and_never_marks_mail_read() -> None:
    fake = FakeImap()
    gmail = inbox.Gmail("a@gmail.com", "pw", connect=lambda *a, **k: fake)
    got = gmail.headers(date(2026, 11, 20))
    gmail.message("7")
    assert [uid for uid, _ in got] == ["7", "9"]
    assert ("select", '"[Gmail]/All Mail"', True) in fake.calls
    assert ("uid", "SEARCH", None, "SINCE", "20-Nov-2026") in fake.calls
    fetches = [c[3] for c in fake.calls if c[:2] == ("uid", "FETCH")]
    assert fetches and all("BODY.PEEK" in f for f in fetches)


def test_a_refused_gmail_sign_in_says_what_to_check_without_the_password() -> None:
    fake = FakeImap()
    fake.fail_login = True
    with pytest.raises(HelperError) as refused:
        inbox.Gmail("a@gmail.com", "secret-pw", connect=lambda *a, **k: fake)
    assert "GMAIL_APP_PASSWORD" in str(refused.value) and "secret-pw" not in str(refused.value)


def test_missing_gmail_settings_say_what_to_add(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(HelperError, match="GMAIL_ADDRESS and GMAIL_APP_PASSWORD"):
        inbox.gmail_from_env(Paths(repo))


def test_codes_alerts_and_everyday_words_stay_out_and_job_site_senders_name_the_company(repo: Path) -> None:
    paths, cfg, ids = _setup(repo)
    conn = open_db(paths, cfg)
    fan = apps.insert(conn, company="Fanatics Collectibles", role_title="People Intelligence Intern", status="applied")
    dex = apps.insert(conn, company="Dexcom", role_title="SW Development Engineer 1", status="applied")
    also = apps.insert(conn, company="ALSO", role_title="Software Engineering Intern", status="applied")
    conn.commit()
    conn.close()
    mails = [
        _mail("ibwujb.fa.sender@workflow.email.us-ashburn-1.ocs.oraclecloud.com", "Confirm your identity",
              "You must confirm your identity using the one-time pass code : 437602", mid="<oracle-code>"),
        _mail("no-reply@fanatics.com", "Important information about your application to Fanatics",
              "We have decided to move forward with other candidates whose skills match our current needs. We also "
              "thank you.", mid="<fanatics>"),
        _mail("dexcom@myworkday.com", "Update on your Application for JR121398 , SW Development Engineer 1",
              "Thank you for applying.", mid="<dexcom>"),
        _mail("Snowflake Careers <x@careeralerts.snowflake.com>", "New Job Alert", "jobs", mid="<snow>"),
        _mail("Microsoft Careers <a@email.careers.microsoft.com>", "2 new job(s) at Microsoft for: Jobs in US", "x",
              mid="<ms>"),
        _mail("T-Mobile Candidate Experience <t@qemailserver.com>", "We'd Love to Hear About Your Application Experience",
              "survey", mid="<survey>"),
    ]
    run_id = _start(paths, cfg, FakeSource(mails))
    inp = read_json(run_dir(paths, run_id) / inbox.INPUT)
    by_id = {e["message_id"]: [c["application_id"] for c in e["candidates"]] for e in inp["emails"]}
    assert by_id == {"<fanatics>": [fan], "<dexcom>": [dex]}  # not "ALSO", "Match" or "Current" from the words
    assert "437602" not in str(inp)
    assert inp["left_out"]["sign-in or verification emails (yours)"] == 1
    assert inp["left_out"]["job alerts and surveys"] == 3
    assert also not in by_id["<fanatics>"]


def test_a_step_already_taken_moves_the_status_but_adds_no_to_do(repo: Path) -> None:
    paths, cfg, ids = _setup(repo)
    run_id = _start(paths, cfg, FakeSource(MAILS))
    write_json(run_dir(paths, run_id) / inbox.OUTPUT, {"emails": [
        {"message_id": "<reject@acme>", "category": "other", "application_id": None, "summary": "x"},
        {"message_id": "<oa@globex>", "category": "oa", "application_id": ids["globex"], "done": True,
         "summary": "Globex confirmed the assessment was submitted."},
        {"message_id": "<received@initech>", "category": "received", "application_id": None, "summary": "x"},
    ]})
    out = inbox.continue_inbox(paths, cfg, run_id, approve=True)
    assert "To do" not in out and "applied -> oa" in out
    conn = open_db(paths, cfg)
    try:
        assert apps.get(conn, ids["globex"])["status"] == "oa"
        assert queue.page_doc(conn)["inbox"] == []
    finally:
        conn.close()


def test_a_requisition_number_in_the_email_finds_an_application_under_its_legal_name(repo: Path) -> None:
    paths, cfg, ids = _setup(repo)
    conn = open_db(paths, cfg)
    pwc = apps.insert(conn, company="PRICEWATERHOUSECOOPERS LLP", role_title="Software Engineering - Associate",
                      status="applied", job_id="768636WD")
    apps.insert(conn, company="Old Co", role_title="Intern", status="applied", job_id="req-2025")  # too generic
    conn.commit()
    conn.close()
    mails = [_mail("pwc@myworkday.com", "Reminder: Action required for your application to be considered by PwC",
                   "Please complete our assessment for 768636WD Assurance Innovation (req-2025).", mid="<pwc>")]
    run_id = _start(paths, cfg, FakeSource(mails))
    (email,) = read_json(run_dir(paths, run_id) / inbox.INPUT)["emails"]
    assert [(c["application_id"], c.get("job_id_in_email")) for c in email["candidates"]] == [(pwc, "768636WD")]


def test_marking_an_application_rejected_closes_its_to_dos(repo: Path) -> None:
    paths, cfg, ids = _setup(repo)
    conn = open_db(paths, cfg)
    for n, app in enumerate((ids["globex"], ids["acme"])):
        conn.execute("INSERT INTO inbox_messages (message_id, received_at, category, application_id, action, run_id, "
                     "processed_at, done) VALUES (?, '2026-11-25T10:00', 'oa', ?, 'Take the test', 'r', 'x', 0)",
                     (f"<m{n}>", app))
    assert inbox.close_for(conn, ids["globex"], "interview") == 0
    assert inbox.close_for(conn, ids["globex"], "rejected") == 1
    assert [t["action"] for t in inbox.open_todos(conn, today=date(2026, 11, 27))] == ["Take the test"]
    conn.close()
