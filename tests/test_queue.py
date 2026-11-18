"""The job hunt's list (2026-11-16): postings move along as runs go, and leave once sent."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from helper import hunt
from helper.config import Paths, load_config
from helper.tracker import queue
from helper.tracker.commands import open_db
from helper.tracker.repo import insert as insert_application

URL = "https://job-boards.greenhouse.io/acme/jobs/1"


def _setup(repo: Path):  # type: ignore[no-untyped-def]
    paths = Paths(repo)
    cfg = load_config(paths)
    conn = open_db(paths, cfg)
    queue.add(conn, url=URL + "?gh_src=x", company="Acme", title="Software Engineer Intern", location="New York, NY",
              ats="greenhouse", source="greenhouse", job_id="1")
    conn.commit()
    conn.close()
    return paths, cfg


def _row(paths: Paths, cfg, key: str = URL):  # type: ignore[no-untyped-def]
    conn = open_db(paths, cfg)
    try:
        return queue.find(conn, key)
    finally:
        conn.close()


def test_a_listed_posting_moves_along_and_leaves_once_sent(repo: Path) -> None:
    paths, cfg = _setup(repo)
    assert _row(paths, cfg)["status"] == "queued"  # stored without tracking parameters
    state = SimpleNamespace(run_id="r1", application_id=11, options={"eligibility": {"flags": []}})
    hunt.on_run_started(paths, cfg, URL, state)
    assert (_row(paths, cfg)["status"], _row(paths, cfg)["run_id"]) == ("applying", "r1")
    hunt.on_fill_message(paths, cfg, "r1", "My Information: filling 14 question(s).")
    assert _row(paths, cfg)["status"] == "applying"  # progress isn't a stop
    hunt.on_fill_message(paths, cfg, "r1", "A CAPTCHA is showing: solve it in the browser window.")
    row = _row(paths, cfg)
    assert row["status"] == "needs_you" and "CAPTCHA" in row["reason"]
    text = (paths.exports / hunt.QUEUE_MD).read_text(encoding="utf-8")
    assert "Needs you" in text and "Acme" in text and "CAPTCHA" in text
    hunt.on_fill_outcome(paths, cfg, "r1", 11, "confirmed")
    assert _row(paths, cfg) is None  # sent: off the list
    assert "Nothing on the list" in (paths.exports / hunt.QUEUE_MD).read_text(encoding="utf-8")


def test_ineligible_runs_are_skipped_and_unclear_submits_wait_for_vatsal(repo: Path) -> None:
    paths, cfg = _setup(repo)
    blocked = SimpleNamespace(run_id="r2", application_id=None, options={"eligibility": {"flags": [
        {"level": "blocker", "message": "It requires U.S. citizenship."}]}})
    hunt.on_run_started(paths, cfg, URL, blocked)
    row = _row(paths, cfg)
    assert row["status"] == "skipped" and "citizenship" in row["reason"]
    hunt.on_fill_outcome(paths, cfg, "r2", 12, "unverified")
    assert _row(paths, cfg)["status"] == "needs_you" and "check it was sent" in _row(paths, cfg)["reason"]


def test_marking_applied_and_resume_only_runs(repo: Path) -> None:
    paths, cfg = _setup(repo)
    state = SimpleNamespace(run_id="r3", application_id=13, options={})
    hunt.on_run_started(paths, cfg, URL, state)
    hunt.on_resume_ready(paths, cfg, "r3")
    row = _row(paths, cfg)
    assert row["status"] == "ready_for_you" and "helper assist r3" in row["reason"]
    conn = open_db(paths, cfg)
    try:
        hunt.on_marked(paths, conn, 13, "applied")
        assert queue.find(conn, URL) is None
        assert queue.add(conn, url="https://x.test/2", company="B", title="T") is not None
        assert queue.add(conn, url="https://x.test/2", company="B", title="T") is None  # already listed
    finally:
        conn.close()


def test_marking_applied_finds_a_posting_listed_by_its_run_only(repo: Path) -> None:
    # Airbnb and apexanalytix (2026-11-18): listed before their applications existed, so the rows held only the run
    paths, cfg = _setup(repo)
    hunt.on_run_started(paths, cfg, URL, SimpleNamespace(run_id="r4", application_id=None, options={}))
    conn = open_db(paths, cfg)
    try:
        app_id = insert_application(conn, company="Acme", role_title="Software Engineer Intern", run_id="r4")
        conn.commit()
        assert queue.find(conn, URL)["application_id"] is None
        hunt.on_marked(paths, conn, app_id, "applied")
        assert queue.find(conn, URL) is None
    finally:
        conn.close()


def test_the_web_page_record_shows_postings_only_and_recent_skips(repo: Path) -> None:
    paths, cfg = _setup(repo)
    conn = open_db(paths, cfg)
    try:
        queue.add(conn, url="https://x.test/old", company="Old", title="Intern", status="skipped", reason="ineligible")
        conn.execute("UPDATE postings SET updated_at = '2026-01-01T00:00:00+00:00' WHERE url = 'https://x.test/old'")
        queue.set_note(conn, "12 found, 9 applied, 2 need you")
        doc = queue.page_doc(conn)
        assert [r["company"] for r in doc["rows"]] == ["Acme"]  # the old skip stays in the database, off the page
        assert set(doc["rows"][0]) <= {"company", "title", "location", "url", "ats", "status", "reason", "run_id",
                                       "posted_at", "updated_at"}
        assert "12 found" in doc["note"]
        urls, _ = queue.known(conn)
        assert "https://x.test/old" in urls  # so a search doesn't bring it back
    finally:
        conn.close()


def test_find_skips_a_posting_whose_full_title_fails_the_filters(repo: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from helper import find

    paths = Paths(repo)
    cfg = load_config(paths)
    feed = find.Found(url="https://jobs.smartrecruiters.com/Acme/744000000000003", company="Acme",
                      title="Forward Deployed Engineer Intern", location="Santa Clara, CA", ats="smartrecruiters",
                      source="list", how="you apply")
    monkeypatch.setattr(find, "search", lambda *a, **k: ([feed], find.Sources()))

    def check(paths, cfg, found, limit):  # type: ignore[no-untyped-def]
        found[0].title = "Associate Forward Deployed Engineer Intern (Bachelors Summer 2027)"
        found[0].eligibility = {"recommendation": "build", "blockers": [], "warnings": []}
    monkeypatch.setattr(find, "check_all", check)
    out = hunt.find_command(paths, cfg, 24, 10)
    assert "Ineligible (on the list as skipped): 1" in out
    conn = open_db(paths, cfg)
    try:
        assert queue.find(conn, feed.url)["status"] == "skipped"
    finally:
        conn.close()


def test_a_network_hiccup_leaves_a_posting_unchecked_not_skipped(  # Nelnet's SSLError, 2026-11-17
        repo: Path, sample_profile_text: str, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from helper import apply as apply_mod
    from helper import find
    from helper.convert import run_convert
    from helper.errors import HelperError

    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    paths = Paths(repo)
    cfg = load_config(paths)
    run_convert(paths, cfg)
    flaky = find.Found(url="https://nelnet.wd1.myworkdayjobs.com/x/job/Lincoln-NE/Intern---Data-Analytics_R1",
                       company="nelnet", title="Intern - Data Analytics", location="Lincoln, NE", ats="workday",
                       source="workday", how="helper fills")
    closed = find.Found(url="https://acme.wd1.myworkdayjobs.com/x/job/Remote/Data-Intern_R2", company="acme",
                        title="Data Intern", location="Remote", ats="workday", source="workday", how="helper fills")

    def fetch(url: str, purpose: str = "apply"):  # type: ignore[no-untyped-def]
        if "nelnet" in url:
            raise HelperError("Couldn't reach Workday (SSLError). Check your internet connection and try again.")
        raise HelperError("Workday says this posting no longer takes applications")
    monkeypatch.setattr(apply_mod, "fetch_job", fetch)
    monkeypatch.setattr(find, "search", lambda *a, **k: ([flaky, closed], find.Sources()))
    out = hunt.find_command(paths, cfg, 24, 10)
    assert "Ineligible (on the list as skipped): 1" in out
    assert "couldn't read it: Couldn't reach Workday (SSLError)" in out
    conn = open_db(paths, cfg)
    try:
        assert queue.find(conn, flaky.url) is None  # not on the list: the next search checks it again
        assert queue.find(conn, closed.url)["status"] == "skipped"
    finally:
        conn.close()


def test_apply_manually_is_its_own_group(repo: Path) -> None:  # Vatsal, 2026-11-17: Boston Scientific
    paths, cfg = _setup(repo)
    conn = open_db(paths, cfg)
    try:
        assert queue.set_status(conn, URL, "apply_manually", "Submit clicked, no confirmation: apply on their site")
        conn.commit()
        text = queue.render_markdown(conn)
        assert "## Apply manually (on the company's own site) (1)" in text or "Apply manually (on the company's own site)" in text
        assert [r["status"] for r in queue.page_doc(conn)["rows"]] == ["apply_manually"]
    finally:
        conn.close()


def test_a_built_lever_run_goes_to_apply_manually(repo: Path) -> None:  # Vatsal, 2026-11-17: Lever by hand for now
    paths, cfg = _setup(repo)
    conn = open_db(paths, cfg)
    try:
        queue.add(conn, url="https://jobs.lever.co/acme/1234", company="Acme", title="Data Intern", ats="lever",
                  source="lever")
        assert queue.link_run(conn, "https://jobs.lever.co/acme/1234", "run-lever")
        conn.commit()
    finally:
        conn.close()
    hunt.on_resume_ready(paths, cfg, "run-lever")
    row = _row(paths, cfg, "run-lever")
    assert row["status"] == "apply_manually" and "helper assist run-lever" in row["reason"]
    from helper.find import YOU_APPLY

    assert "lever" in YOU_APPLY
