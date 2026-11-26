"""The job hunt's list (2026-11-16): postings move along as runs go, and leave once sent."""

from __future__ import annotations

import json
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
    # Wurl (2026-11-20): the question under the heading is what's waiting
    hunt.on_fill_message(paths, cfg, "r1", "Only you can answer these; fill them in the window:\n"
                                           "  How many hours per week would you be available?")
    assert _row(paths, cfg)["reason"] == ("Only you can answer these; fill them in the window: How many hours per "
                                          "week would you be available?")
    hunt.on_fill_message(paths, cfg, "r1", "A CAPTCHA is showing: solve it in the browser window.")
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
    # HPE (2026-11-19): a posting taken down leaves the open windows for good
    hunt.on_fill_outcome(paths, cfg, "r2", 12, "posting_closed", "posting closed: the site says 'gone'")
    assert (_row(paths, cfg)["status"], _row(paths, cfg)["reason"]) == ("skipped", "posting closed: the site says 'gone'")


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


def test_a_link_listed_without_details_takes_its_runs_details(repo: Path) -> None:  # Akuna, Disney, 2026-11-18
    paths, cfg = _setup(repo)
    link = "https://akunacapital.com/careers/job/8018853/?gh_jid=8018853"
    hunt.add(paths, cfg, [link])
    assert (_row(paths, cfg, link)["company"], _row(paths, cfg, link)["title"]) == ("?", "?")
    (paths.runs / "r5").mkdir(parents=True)
    (paths.runs / "r5" / "job.json").write_text(json.dumps({
        "company": "Akuna Capital", "role_title": "Software Engineer Intern - Python, Summer 2027",
        "location": "Chicago, IL", "ats": "greenhouse", "job_id": "8018853"}), encoding="utf-8")
    hunt.on_run_started(paths, cfg, link, SimpleNamespace(run_id="r5", application_id=None, options={}))
    row = _row(paths, cfg, link)
    assert (row["company"], row["title"], row["location"]) == (
        "Akuna Capital", "Software Engineer Intern - Python, Summer 2027", "Chicago, IL")
    # a row the search named keeps its own details
    (paths.runs / "r6").mkdir()
    (paths.runs / "r6" / "job.json").write_text(json.dumps({"company": "Acme Corp", "role_title": "Other"}),
                                                encoding="utf-8")
    hunt.on_run_started(paths, cfg, URL, SimpleNamespace(run_id="r6", application_id=None, options={}))
    assert (_row(paths, cfg)["company"], _row(paths, cfg)["title"]) == ("Acme", "Software Engineer Intern")


def test_a_link_listed_after_its_run_started_takes_its_runs_details(repo: Path) -> None:  # Moderna, 2026-11-23
    paths, cfg = _setup(repo)
    link = "https://careers.example.com/job/abc-123/data-analyst/"
    (paths.runs / "r7").mkdir(parents=True)
    (paths.runs / "r7" / "job.json").write_text(json.dumps({
        "url": link + "?utm_source=x", "company": "Example Co", "role_title": "Data Analyst",
        "location": "New York, NY", "ats": "generic"}), encoding="utf-8")
    (paths.runs / "r7" / "state.json").write_text(json.dumps({"application_id": 42}), encoding="utf-8")
    hunt.add(paths, cfg, [link])
    row = _row(paths, cfg, link)
    assert (row["company"], row["title"], row["run_id"], row["application_id"]) == (
        "Example Co", "Data Analyst", "r7", 42)


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


def test_a_page_title_that_only_leaves_out_the_field_keeps_the_posting(repo: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Aon (2026-11-23): the list said "Early Careers Health Analytics Specialist", the iCIMS page "Early Careers:
    # Health Specialist", and the search skipped a data science role as not a fit
    from helper import find

    paths = Paths(repo)
    cfg = load_config(paths)
    aon = find.Found(url="https://jobs.aon.com/jobs/113794?icims=1&lang=en-us", company="Aon",
                     title="Early Careers Health Analytics Specialist", location="Chicago, IL; Atlanta, GA",
                     ats="icims", source="list", how="helper fills")
    vague = find.Found(url="https://jobs.acme.test/jobs/2", company="Acme", title="Early Careers Specialist",
                       location="Chicago, IL", ats="icims", source="list", how="helper fills")
    monkeypatch.setattr(find, "search", lambda *a, **k: ([aon, vague], find.Sources()))

    def check(paths, cfg, found, limit):  # type: ignore[no-untyped-def]
        found[0].title = "Early Careers: Health Specialist"
        for f in found:
            f.eligibility = {"recommendation": "build", "blockers": [], "warnings": []}
    monkeypatch.setattr(find, "check_all", check)
    out = hunt.find_command(paths, cfg, 24, 10)
    assert "Ineligible (on the list as skipped): 1" in out  # only the posting neither title puts in the field
    conn = open_db(paths, cfg)
    try:
        assert queue.find(conn, aon.url) is None
        assert queue.find(conn, vague.url)["status"] == "skipped"
    finally:
        conn.close()


def test_a_page_location_that_only_leaves_out_the_state_keeps_the_posting(repo: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # TQL (2026-11-25): the list said "Cincinnati, OH", the Avature page "Cincinnati", and the search skipped its
    # Software Development Intern as not a fit. A page that names a place abroad still skips the posting.
    from helper import find

    paths = Paths(repo)
    cfg = load_config(paths)
    tql = find.Found(url="https://tql.avature.net/en_US/TQLexternalcareers/JobDetail/19763", company="TQL",
                     title="Software Development Intern (Summer 2027)", location="Cincinnati, OH", ats="avature",
                     source="list", how="helper fills")
    abroad = find.Found(url="https://acme.avature.net/en_US/careers/JobDetail/2", company="Acme",
                        title="Software Engineering Intern", location="Remote", ats="avature", source="list",
                        how="helper fills")
    monkeypatch.setattr(find, "search", lambda *a, **k: ([tql, abroad], find.Sources()))

    def check(paths, cfg, found, limit):  # type: ignore[no-untyped-def]
        found[0].location = "Cincinnati"
        found[1].location = "Toronto"
        for f in found:
            f.eligibility = {"recommendation": "build", "blockers": [], "warnings": []}
    monkeypatch.setattr(find, "check_all", check)
    out = hunt.find_command(paths, cfg, 24, 10)
    assert "Ineligible (on the list as skipped): 1" in out
    conn = open_db(paths, cfg)
    try:
        assert queue.find(conn, tql.url) is None
        assert queue.find(conn, abroad.url)["status"] == "skipped"
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

    assert "lever" in YOU_APPLY and "ukg" in YOU_APPLY  # UKG: Apply needs an account (rule 8)


def test_a_window_closed_while_a_flow_waits_shows_on_the_list(repo: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Constellation, Sheetz and Epsilon (2026-11-20): Vatsal closed iCIMS windows that waited for him. helper stopped
    # with "The browser window was closed" and the list still said "Paused for you" about windows that were gone
    import pytest

    pytest.importorskip("playwright.sync_api")
    from helper import apply
    from helper.adapters import oracle
    from helper.answers import AnswerPlan
    from helper.models import Job, RunState
    from helper.runs import save_state, set_step, write_json

    paths, cfg = _setup(repo)
    fixtures = Path(__file__).resolve().parent / "fixtures"
    (repo / "data" / "profile.yaml").write_text((fixtures / "profile_sample.yaml").read_text(encoding="utf-8"),
                                                encoding="utf-8")
    run_id = "20261120-010000-acme-software-engineer-intern"
    folder = paths.runs / run_id
    folder.mkdir(parents=True)
    pdf = paths.resumes / run_id / "AlexExample_Resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 test")
    job = Job(url=URL, ats="icims", company="Acme", role_title="Software Engineer Intern", description_text="x",
              source="page", fetched_at="now", apply_url=(fixtures / "oracle_apply.html").resolve().as_uri())
    write_json(folder / "job.json", job.model_dump(mode="json"))
    write_json(folder / "answers_plan.json", AnswerPlan(answers=[]).model_dump(mode="json"))
    conn = open_db(paths, cfg)
    app_id = insert_application(conn, company="Acme", role_title="Software Engineer Intern", job_url=URL, run_id=run_id,
                                resume_path=f"resumes/{run_id}/AlexExample_Resume.pdf")
    conn.commit()
    conn.close()
    state = RunState(run_id=run_id, created_at="now", command="apply", url=URL, application_id=app_id,
                     options={"run": {"projects_per_resume": 5, "bullets_per_item": 2, "grad_date": "May 2027"},
                              "result": {"pdf": f"resumes/{run_id}/AlexExample_Resume.pdf"}})
    set_step(state, "answers", "done")
    save_state(paths, state)
    hunt.on_run_started(paths, cfg, URL, SimpleNamespace(run_id=run_id, application_id=app_id,
                                                         options={"eligibility": {"flags": []}}))

    def run_flow(page, ctx):  # type: ignore[no-untyped-def]
        ctx.say("Paused for you: iCIMS is showing a CAPTCHA check. Solve it in the window; helper never does (rule 6).")
        page.close()  # Vatsal closes the window while helper waits
        page.wait_for_timeout(200)

    fake = SimpleNamespace(FlowContext=oracle.FlowContext, run_flow=run_flow, is_submit_request=lambda url, method: False)
    monkeypatch.setattr("helper.adapters.adapter_for", lambda url: fake)
    monkeypatch.setattr(apply, "_context", lambda *args: SimpleNamespace(approved=None))  # no master list here
    monkeypatch.setattr(apply, "_history", lambda *args: ([], []))
    outcome = apply.fill_run(paths, cfg, run_id, headless=True, wait_minutes=0.1, poll_seconds=0.2)
    assert outcome.status == "filled" and "the browser was closed" in outcome.message, outcome.message
    # Garmin (2026-11-22): closed at the CAPTCHA before anything was filled, so the row is still 'drafted'
    assert "The application stays 'drafted'." in outcome.message, outcome.message
    row = _row(paths, cfg)
    assert row["status"] == "needs_you" and "Paused" not in row["reason"], row["reason"]
    assert "the window was closed" in row["reason"] and f"helper fill {run_id} --keep-open" in row["reason"]


def test_a_placeholder_company_takes_the_name_its_run_read(repo: Path) -> None:  # UKG's CAS posting, 2026-11-20
    paths = Paths(repo)
    cfg = load_config(paths)
    conn = open_db(paths, cfg)
    queue.add(conn, url=URL, company="Unknown", title="2027 Technology Summer Intern", location="Columbus, OH",
              ats="ukg", source="list")
    assert queue.fill_details(conn, URL, company="CAS")
    assert queue.find(conn, URL)["company"] == "CAS"
    assert queue.fill_details(conn, URL, company="Someone Else")  # a real name, once there, stays
    assert queue.find(conn, URL)["company"] == "CAS"
    conn.close()


def test_a_page_that_names_no_employer_takes_the_list_company(repo: Path) -> None:
    # UKG boards without a footer link (Werner, AFL, Encore, 2026-11-20) came out "Unknown", which the letter would use
    from helper.models import Job

    paths, cfg = _setup(repo)
    url = "https://recruiting2.ultipro.com/WER1000WERE/JobBoard/9cc9/OpportunityDetail?opportunityId=48ee"
    conn = open_db(paths, cfg)
    try:
        queue.add(conn, url=url, company="Werner Electric Supply", title="AI Intern (Summer 2027)", ats="ukg",
                  source="list")
        conn.commit()
    finally:
        conn.close()
    page = Job(url=url, ats="generic", company="Unknown", role_title="AI Intern (Summer 2027)", description_text="x",
               source="browser", fetched_at="2026-11-20T17:50:00")
    assert hunt.named_job(paths, cfg, page, url).company == "Werner Electric Supply"
    assert hunt.named_job(paths, cfg, page, url, "Werner").company == "Werner"  # --company wins
    named = page.model_copy(update={"company": "Acuity"})
    assert hunt.named_job(paths, cfg, named, url).company == "Acuity"  # a page's own name stays
    assert hunt.named_job(paths, cfg, page, "https://example.com/not-listed").company == "Unknown"
