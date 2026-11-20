"""Lever and Ashby (spec 10.2): links, intake, reading the real saved forms, and filling them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from helper.adapters import adapter_for, ashby, dom, lever
from helper.answers import AnswerContext, plan_answers
from helper.models import Config, FormQuestion, Job, Profile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LEVER_FORM = (FIXTURES / "lever_zoox_form.html").resolve()
ASHBY_FORM = (FIXTURES / "ashby_notion_form.html").resolve()
SAMPLE = (FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")
PROFILE = Profile.model_validate(yaml.safe_load(SAMPLE.replace("city: Springfield", "city: Seattle")
                                                .replace("state: ST", "state: WA")))
PROFILE.tier1.how_did_you_hear = "Career site"
PROFILE.tier1.work_location_preferences = "Open to all locations"
PROFILE.tier1.willing_to_relocate = "Yes"
PROFILE.tier1.current_school = "Northeastern University"
PROFILE.tier1.degree = "Master of Science"
PROFILE.tier2.eeo.race_ethnicity = "Asian (South Asian)"  # the sample leaves it blank

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


def scan(browser: Any, path: Path) -> tuple[Any, list[FormQuestion]]:
    page = browser.new_page(java_script_enabled=False)  # the saved forms are static: no site scripts run
    page.goto(f"file://{path}")
    return page, dom.discover(page.main_frame)


# --------------------------------------------------------------------------- links and intake


def test_links() -> None:
    assert lever.parse_ref("https://jobs.lever.co/zoox/85c6aec7-8da2-445d-9c4d-812365dbce97/apply") == (
        "zoox", "85c6aec7-8da2-445d-9c4d-812365dbce97", False)
    assert lever.parse_ref("https://jobs.eu.lever.co/acme/85c6aec7-8da2-445d-9c4d-812365dbce97")[2] is True
    assert ashby.parse_ref("https://jobs.ashbyhq.com/notion/3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c/application") == (
        "notion", "3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c")
    assert ashby.parse_ref("https://www.notion.com/careers?ashby_jid=3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c") == (
        None, "3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c")
    assert adapter_for("https://jobs.lever.co/zoox/85c6aec7-8da2-445d-9c4d-812365dbce97") is lever
    assert adapter_for("https://jobs.ashbyhq.com/notion/3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c") is ashby
    assert lever.is_submit_request("https://jobs.lever.co/zoox/85c6aec7-8da2-445d-9c4d-812365dbce97/apply", "POST")
    assert ashby.is_submit_request("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction",
                                   "POST")
    assert not ashby.is_submit_request("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting", "POST")
    assert ashby.date_text("May 2027") == "05/01/2027"


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code, self._body = status, body

    def json(self) -> Any:
        return self._body


class FakeSession:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                return FakeResponse(200, body)
        return FakeResponse(404, {})


def fake_scan(questions: list[FormQuestion], title: str):  # type: ignore[no-untyped-def]
    return lambda url, anchors, hosts: (questions, title)


def test_lever_intake() -> None:
    posting = json.loads((FIXTURES / "lever_zoox_posting.json").read_text(encoding="utf-8"))
    session = FakeSession({"https://api.lever.co/v0/postings/zoox/": posting})
    job = lever.fetch_job("https://jobs.lever.co/zoox/85c6aec7-8da2-445d-9c4d-812365dbce97", session,
                          scan=fake_scan([], "Zoox - Data Science Intern [Summer 2027]"))
    assert (job.company, job.role_title, job.ats) == ("Zoox", "Data Science Intern [Summer 2027]", "lever")
    assert job.apply_url.endswith("/apply")
    assert "What you'll do\n\n- Analyze autonomy data with Python and SQL" in job.description_text
    assert "Graduating between September 2027 and August 2028." in job.description_text


def test_ashby_intake_including_a_company_page_link() -> None:
    board = {"jobs": [json.loads((FIXTURES / "ashby_notion_job.json").read_text(encoding="utf-8"))]}
    session = FakeSession({"https://api.ashbyhq.com/posting-api/job-board/notion": board})
    scan_ = fake_scan([], "Software Engineer Intern (Summer 2027) @ Notion")
    job = ashby.fetch_job("https://jobs.ashbyhq.com/notion/3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c", session, scan=scan_)
    assert (job.company, job.role_title, job.ats) == ("Notion", "Software Engineer Intern (Summer 2027)", "ashby")
    assert job.apply_url == "https://jobs.ashbyhq.com/notion/3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c/application"
    page_link = ashby.fetch_job("https://www.notion.com/careers?ashby_jid=3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c",
                                session, scan=scan_)
    assert page_link.job_id == "3fba1c39-c5cb-47d7-9ad2-1cec4d7e9d0c"


# --------------------------------------------------------------------------- reading the real forms


def test_every_question_on_the_saved_ashby_form(browser: Any) -> None:
    page, questions = scan(browser, ASHBY_FORM)
    by_label = {q.label: q for q in questions}
    assert len(questions) == 25
    assert by_label["Full Name"].field_name == "ashby:_systemfield_name" and by_label["Full Name"].required
    assert by_label["Resume"].type == "file"
    assert by_label["Current Location"].type == "combobox"
    assert by_label["Are you authorized to work lawfully in the United States?"].type == "yesno"
    assert by_label["How many prior internships have you had?"].options == ["0", "1", "2", "3+"]
    assert by_label["Degree Type"].type == "multi_select"
    assert by_label["Graduation Date"].type == "date"
    assert by_label["Gender"].section == "eeoc" and by_label["Gender"].type == "radio"
    page.close()


def test_every_question_on_the_saved_lever_form(browser: Any) -> None:
    page, questions = scan(browser, LEVER_FORM)
    by_label = {q.label: q for q in questions}
    assert len(questions) == 18
    assert by_label["Full name"].field_name == "lever:name"
    assert by_label["Resume/CV"].type == "file"
    assert by_label["How did you hear about us?"].type == "radio"
    assert "Northeastern University" in by_label["Which school do you attend?"].options
    assert by_label["Veteran status"].section == "eeoc" and by_label["Veteran status"].type == "select"
    page.close()


def plan_for(questions: list[FormQuestion], company: str) -> Any:
    job = Job(company=company, role_title="Intern", description_text="x", source="test", fetched_at="now",
              questions=questions)
    return plan_answers(questions, AnswerContext(profile=PROFILE, cfg=Config(), job=job, grad_date="May 2027",
                                                 employment="internship", season="summer", season_year=2027))


def test_the_ashby_plan(browser: Any) -> None:
    page, questions = scan(browser, ASHBY_FORM)
    plan = {a.label: a for a in plan_for(questions, "Notion").answers}
    page.close()
    assert plan["Degree Type"].value == "Master's" and plan["Degree Type"].kind == "auto"
    hear = plan["How did you hear about this opportunity? (select all that apply)"]
    assert (hear.kind, hear.value) == ("auto", "Notion Website")
    places = plan["Please indicate all of the locations that you would be interested in relocating to for this position."]
    assert places.value == "New York, NY; San Francisco, CA"  # open to all locations
    sponsorship = plan["For this specific internship, will you require any of the below sponsorship for employment?"]
    assert sponsorship.kind == "unknown"  # its choices name F1: Vatsal answers
    assert plan["Race"].value.startswith("Asian (Not Hispanic or Latino)") and plan["Race"].kind == "auto"


def test_filling_the_saved_forms(browser: Any, tmp_path: Path) -> None:
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    for form, company in ((LEVER_FORM, "Zoox"), (ASHBY_FORM, "Notion")):
        page, questions = scan(browser, form)
        plan = plan_for(questions, company)
        results = dom.fill_dom_form(page.main_frame, plan.answers, pdf, city="Seattle", pause=lambda: None,
                                    date_text=ashby.date_text)
        failed = [(r.label, r.detail) for r in results if not r.ok]
        assert not failed, (company, failed)
        if company == "Zoox":
            assert page.input_value("input[name=name]") == PROFILE.tier1.full_name
            assert page.locator("select[name='eeo[gender]']").input_value() == ""  # the fictional profile has no gender
            assert page.evaluate("() => [...document.querySelectorAll('input[type=radio]')].filter(r => r.checked)"
                                 ".map(r => r.value)") == ["Zoox Careers Page"]
            assert page.evaluate("() => document.querySelector('input[name=resume]').files[0].name") == pdf.name
        else:
            assert page.input_value("#_systemfield_email") == PROFILE.tier1.email
            checked = page.evaluate("() => [...document.querySelectorAll('input[type=checkbox]:checked')].map(c => c.name)")
            assert {"Notion Website", "Master's", "New York, NY", "San Francisco, CA"} <= set(checked)
        page.close()


def test_a_numbers_only_box_gets_the_number(browser: Any) -> None:
    # Snowflake's "number of hours per week you can work ... in the office" (2026-11-19): the on-site rule's "Yes" made
    # Playwright fail with "Cannot type text into input[type=number]"
    from types import SimpleNamespace

    page = browser.new_page()
    page.set_content("<div id='b'><label>Hours per week</label><input type='number'></div>")
    block = page.locator("#b")

    def fill(value: str):  # type: ignore[no-untyped-def]
        a = SimpleNamespace(type="text", value=value, key="hours_per_week")
        return dom._fill_one(page.main_frame, block, a, Path("r.pdf"), None, lambda s: s)

    assert fill("40") == (True, "40", "") and page.input_value("#b input") == "40"
    assert fill("40 hours per week") == (True, "40", "")
    assert fill("Yes") == (False, "Yes", "a numbers-only box can't take 'Yes'")
    page.close()
