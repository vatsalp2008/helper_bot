"""Any other site (milestone 10): known systems behind company pages, reading and filling unfamiliar forms, sign-in
walls, and the stop messages for sites helper never fills."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from helper.adapters import adapter_for, generic, lever
from helper.answers import AnswerContext, plan_answers
from helper.errors import NeedsUser
from helper.models import Config, Job, Profile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FORM = f"file://{(FIXTURES / 'generic_company_form.html').resolve()}"
PROFILE = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
PROFILE.tier1.how_did_you_hear = "Career site"
PROFILE.tier2.eeo.auto_answer, PROFILE.tier2.eeo.gender = True, "Male"


def test_which_adapter_takes_a_link() -> None:
    assert adapter_for("https://careers.mockco.com/jobs/123") is generic
    assert adapter_for("https://apply.workable.com/mockco/j/ABC123/") is generic
    taleo = "https://acme.taleo.net/careersection/2/jobdetail.ftl?job=1234"
    with pytest.raises(NeedsUser, match="helper doesn't fill Taleo") as stop:
        adapter_for(taleo)
    assert f"helper resume {taleo}" in str(stop.value)
    assert adapter_for(taleo, purpose="resume") is generic
    with pytest.raises(NeedsUser, match="never automates LinkedIn"):
        adapter_for("https://www.linkedin.com/jobs/view/123", purpose="resume")


def test_known_systems_behind_a_company_page_are_found() -> None:
    assert generic.known_link(["https://mockco.com/about", "https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b/apply"]) \
        == "https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b/apply"
    assert generic.known_link(["https://job-boards.greenhouse.io/embed/job_app?for=acme&token=123"]).startswith(
        "https://job-boards.greenhouse.io/embed/job_app")
    assert generic.known_link(["https://acme.wd5.myworkdayjobs.com/External/job/Seattle/Intern_R1"])
    assert generic.known_link(["https://jobs.ashbyhq.com/acme/3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c"])
    assert generic.known_link(["https://mockco.com/careers", "https://www.lever.co/pricing"]) is None


def test_a_known_system_gets_the_posting(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(lever, "fetch_job", lambda url, session=None: seen.append(url) or "lever's job")
    link = "https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
    assert generic.fetch_job("https://acme.com/careers/42", scan=lambda url: generic.Scan(delegate=link)) == "lever's job"
    assert seen == [link]


def test_a_page_without_a_description_asks_for_the_text() -> None:
    from helper.errors import HelperError

    with pytest.raises(HelperError, match="--jd-file"):  # nor does the page's own HTML have a JobPosting
        generic.fetch_job("https://acme.com/careers/42", FakeSession({}), scan=lambda url: generic.Scan())


playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def browser() -> Iterator[Any]:
    with playwright_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium isn't installed: {exc}")
        yield b
        b.close()


def test_reading_a_company_page_with_its_own_form(browser: Any) -> None:
    page = browser.new_page()
    page.goto(FORM)
    scan = generic.scan_page(page)
    assert (scan.title, scan.company, scan.delegate) == ("Software Engineer Intern (Summer 2027)", "Mockco", None)
    assert "backend services in Python and SQL" in scan.description and scan.apply_url == FORM
    by_label = {q.label: q for q in scan.questions}
    assert list(by_label) == ["First name", "Last name", "Email address", "Phone", "Resume (PDF)", "LinkedIn profile",
                              "Are you legally authorized to work in the United States?", "How did you hear about us?",
                              "Why do you want to work at Mockco?", "Gender", "I agree to the Mockco privacy policy"]
    assert by_label["Resume (PDF)"].type == "file" and by_label["Resume (PDF)"].required
    assert by_label["Are you legally authorized to work in the United States?"].options == ["Yes", "No"]
    assert by_label["How did you hear about us?"].options == ["LinkedIn", "Company website", "Referral"]
    assert by_label["Gender"].section == "eeoc" and by_label["I agree to the Mockco privacy policy"].type == "boolean"
    page.close()


def test_a_company_page_that_links_to_lever(browser: Any) -> None:
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'generic_links_to_lever.html').resolve()}")
    assert generic.scan_page(page).delegate == "https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b/apply"
    page.close()


def test_a_sign_in_wall_is_left_to_vatsal(browser: Any) -> None:
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'generic_sign_in_wall.html').resolve()}")
    scan = generic.scan_page(page)
    assert scan.login_wall and scan.questions == [] and scan.apply_url is None
    assert "sign in or create an account" in generic.no_form_reason(page)
    page.close()


def plan(questions: list[Any]) -> Any:
    job = Job(company="Mockco", role_title="Intern", description_text="x", source="page", fetched_at="now",
              questions=questions)
    result = plan_answers(questions, AnswerContext(profile=PROFILE, cfg=Config(), job=job, grad_date="May 2027",
                                                   employment="internship", season="summer", season_year=2027))
    why = next(a for a in result.answers if a.label.startswith("Why"))
    why.kind, why.value, why.status = "auto", "Mockco's logistics tools are the kind of product I want to build.", "approved"
    return result


def test_filling_an_unfamiliar_form_never_submits_it(browser: Any, tmp_path: Path) -> None:
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    page = browser.new_page()
    page.goto(FORM)
    results = generic.fill_form(page.main_frame, plan(generic.discover(page.main_frame)).answers, pdf, country=None,
                                city=None, pause=lambda: None)
    assert [r.label for r in results if not r.ok] == []
    assert page.input_value("#fname") == PROFILE.tier1.first_name and page.input_value("#email") == PROFILE.tier1.email
    assert page.input_value("#li") == PROFILE.tier1.linkedin_url
    assert page.is_checked("input[name=authorized][value=yes]") and page.is_checked("input[name=privacy]")
    assert page.locator("#source").evaluate("s => s.options[s.selectedIndex].text") == "Company website"
    assert page.locator("#gender").evaluate("s => s.options[s.selectedIndex].text") == "Male"
    assert page.evaluate("() => document.getElementById('cv').files[0].name") == pdf.name
    assert page.input_value("#why").startswith("Mockco's logistics")
    assert page.evaluate("() => window.submitted") is False  # never Submit
    page.close()


def start(repo: Path, apply_url: str, mode: str = "review") -> tuple[Any, str]:
    from helper.config import Paths, load_config
    from helper.models import RunState
    from helper.runs import save_state, set_step, write_json
    from helper.tracker import repo as tracker_repo
    from helper.tracker.commands import open_db

    (repo / "data" / "profile.yaml").write_text(yaml.safe_dump(PROFILE.model_dump(mode="json")), encoding="utf-8")
    config = repo / "config.yaml"
    config.write_text(re.sub(r"(?m)^auto_submit_min_clean_runs:.*$", "auto_submit_min_clean_runs: 1",
                             config.read_text(encoding="utf-8")), encoding="utf-8")
    paths = Paths(repo)
    run_id = "20261115-120000-mockco-swe-intern"
    folder = paths.runs / run_id
    folder.mkdir(parents=True)
    pdf = paths.resumes / run_id / "AlexExample_Resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 test")
    page_questions = generic.Scan  # silence linters about the unused name in some environments
    del page_questions
    job = Job(url="https://careers.mockco.com/jobs/123", ats="generic", company="Mockco", role_title="Intern",
              description_text="x", source="page", fetched_at="now", apply_url=apply_url)
    write_json(folder / "job.json", job.model_dump(mode="json"))
    with playwright_api.sync_playwright() as pw:  # plan from the real form, as intake does
        b = pw.chromium.launch(headless=True)
        page = b.new_page()
        page.goto(FORM)
        questions = generic.discover(page.main_frame)
        b.close()
    write_json(folder / "answers_plan.json", plan(questions).model_dump(mode="json"))
    conn = open_db(paths, load_config(paths))
    app_id = tracker_repo.insert(conn, company="Mockco", role_title="Intern", job_url=job.url, run_id=run_id,
                                 resume_path=f"resumes/{run_id}/AlexExample_Resume.pdf")
    from helper.autosubmit import record_fill_run

    record_fill_run(conn, "earlier", "generic", "review", "applied", [])
    conn.commit()
    conn.close()
    options: dict[str, Any] = {"run": {"projects_per_resume": 5, "bullets_per_item": 2, "grad_date": "May 2027"},
                               "result": {"pdf": f"resumes/{run_id}/AlexExample_Resume.pdf"}, "submit_mode": mode}
    state = RunState(run_id=run_id, created_at="now", command="apply", url=job.url, application_id=app_id, options=options)
    set_step(state, "answers", "done")
    save_state(paths, state)
    return paths, run_id


def test_a_company_site_auto_submits_once_everything_is_filled(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from helper.apply import fill_run
    from helper.config import load_config

    paths, run_id = start(repo, FORM, mode="auto")  # Vatsal, 2026-11-16: company sites auto-submit too
    outcome = fill_run(paths, load_config(paths), run_id, headless=True, wait_minutes=0.5, poll_seconds=0.3)
    printed = capsys.readouterr().out
    assert "helper clicked Submit once" in printed, printed
    assert outcome.status == "applied", outcome.message  # helper saw the thank-you page
    saved = json.loads((paths.runs / run_id / "fill_log.json").read_text(encoding="utf-8"))
    assert all(r["ok"] for r in saved) and len(saved) == 11


def test_blocked_closed_or_empty_pages_are_not_used_as_a_description() -> None:
    assert generic.readable("Engineer Intern", "We build data tools for logistics teams. " * 20)
    assert not generic.readable("Access Denied", "You don't have permission to access this page. " * 20)
    assert not generic.readable("Engineer Intern", "This position is no longer available. " + "More text. " * 60)
    assert not generic.readable("www.example.com", "Loading")


# --------------------------------------------------------------------------- pages that turn helper's browser away


class FakePage:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code, self.text = status, text


class FakeSession:
    def __init__(self, pages: dict[str, FakePage]) -> None:
        self.pages, self.asked = pages, []

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakePage:
        self.asked.append(url)
        return self.pages.get(url, FakePage(404))


JIBE_URL = "https://mockbank.jibeapply.com/jobs/6233"
QUALIFICATIONS = "".join(f"<li>Experience building {t} services in Python with FastAPI, SQL and Docker.</li>"
                         for t in ("retrieval", "agent", "evaluation", "ingestion", "search", "reporting", "billing"))
JIBE_PAGE = ("<html><head><script type=\"application/ld+json\">" + json.dumps({
    "@context": "http://schema.org", "@type": "JobPosting", "title": "Machine Learning Engineer",
    "description": "<p>Job ID 2026-6233<br>Type: Full-Time</p>", "qualifications": f"<ul>{QUALIFICATIONS}</ul>",
    "skills": "UNAVAILABLE", "hiringOrganization": {"@type": "Organization", "name": "Mockbank of Florida"},
    "jobLocation": {"@type": "Place", "address": {"addressLocality": "Coral Gables", "addressRegion": "Florida"}},
}) + "</script><script>window._jibe = {\"apply_url\":\"https:\\/\\/careers-mockbank.icims.com\\/jobs\\/6233\\/login\"};"
    "</script></head><body><div ng-app></div></body></html>")


def test_a_page_s_jobposting_data_gives_the_employer_and_description() -> None:
    found = generic.job_posting_ld([JIBE_PAGE.split('ld+json">')[1].split("</script>")[0]])
    assert (found.title, found.company, found.location) == ("Machine Learning Engineer", "Mockbank of Florida",
                                                            "Coral Gables, Florida")
    assert "- Experience building retrieval services" in found.description and "UNAVAILABLE" not in found.description
    assert generic.job_posting_ld(['{"@type": "Organization", "name": "Mockco"}', "not json"]) is None
    remote = generic.job_posting_ld(['{"@type": "JobPosting", "title": "Intern", "jobLocation": {"address": '
                                     '{"addressLocality": "Remote", "addressRegion": "UNAVAILABLE"}}}'])
    assert remote.location == "Remote"  # iCIMS's placeholder isn't a place


def test_when_the_browser_is_turned_away_the_page_itself_is_read() -> None:
    session = FakeSession({JIBE_URL: FakePage(200, JIBE_PAGE)})
    job = generic.fetch_job(JIBE_URL, session, scan=lambda url: generic.Scan(title="403 Forbidden",
                                                                             description="403 Forbidden"))
    assert (job.company, job.role_title, job.location) == ("Mockbank of Florida", "Machine Learning Engineer",
                                                           "Coral Gables, Florida")
    assert job.apply_url == "https://careers-mockbank.icims.com/jobs/6233/login" and session.asked == [JIBE_URL]


def test_a_company_page_that_applies_on_a_stopped_site_gets_the_stop_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from helper import apply

    taleo_page = JIBE_PAGE.replace("https:\\/\\/careers-mockbank.icims.com\\/jobs\\/6233\\/login",
                                   "https:\\/\\/mockbank.taleo.net\\/careersection\\/2\\/jobapply.ftl?job=6233")
    assert taleo_page != JIBE_PAGE
    session, real = FakeSession({JIBE_URL: FakePage(200, taleo_page)}), generic.fetch_job
    turned_away = generic.Scan(description="403 Forbidden")
    monkeypatch.setattr(generic, "fetch_job", lambda url: real(url, session, scan=lambda u: turned_away))
    with pytest.raises(NeedsUser, match="helper doesn't fill Taleo") as stop:
        apply.fetch_job(JIBE_URL + "?jr_id=6adb96c6389e06bb41ad2d1f")
    assert f"helper resume {JIBE_URL}`" in str(stop.value)  # the tracking tag is gone
    assert apply.fetch_job(JIBE_URL, purpose="resume").company == "Mockbank of Florida"
    # the same page applying on iCIMS is an iCIMS run (helper signs in there; Vatsal, 2026-11-15)
    session.pages[JIBE_URL] = FakePage(200, JIBE_PAGE)
    assert apply.fetch_job(JIBE_URL).ats == "icims"


def test_an_apply_link_that_ends_at_a_robot_check_is_left_to_vatsal() -> None:
    with pytest.raises(NeedsUser, match="us.bigjobs.example.*check you are not a robot.*rule 6"):
        generic.fetch_job("https://jobs.example.com/x/1", FakeSession({}), scan=lambda url: generic.Scan(
            title="SQL Developer", description="x" * 600, robot_check="us.bigjobs.example"))


def test_a_robot_check_page_is_recognized(browser: Any) -> None:
    page = browser.new_page()
    page.set_content("<body><p>You are now being redirected.</p><p>First we just need to check you are not a robot"
                     "</p></body>")
    assert generic.robot_check(page)
    page.set_content("<body><p>We build robots for warehouses.</p></body>")
    assert not generic.robot_check(page)
    page.close()


def test_an_apply_link_that_opens_a_new_tab_is_followed(browser: Any) -> None:
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'generic_apply_new_tab.html').resolve()}")
    scan = generic.scan_page(page)
    assert scan.title == "Data Engineer Intern" and scan.apply_url == FORM
    assert [q.label for q in scan.questions][:3] == ["First name", "Last name", "Email address"]
    page.context.close()  # and the tab the Apply link opened
