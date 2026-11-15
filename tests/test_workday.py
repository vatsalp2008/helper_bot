"""Workday: links, intake, the real saved pages, a mock apply flow, the account step, and Claude's mid-fill answers."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import yaml

from helper.adapters import adapter_for
from helper.adapters import workday as wd
from helper.adapters import workday_page as wp
from helper.answers import AnswerContext, PlannedAnswer, plan_answers
from helper.ats import detect_ats, job_id_from_url, normalize_url
from helper.errors import HelperError
from helper.models import Config, FormQuestion, Job, Profile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced/job/San-Jose/2027-University-Graduate---Data-Scientist_R173462"
SITE_URL = "https://wd1.myworkdaysite.com/en-US/recruiting/snapchat/snap/job/Seattle-Washington/Software-Engineer_R0042"


def profile() -> Profile:
    p = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    p.tier1.how_did_you_hear = "Career site"
    p.tier1.address.unit = "Apt 1"
    p.tier1.address.state = "WA"
    e = p.tier2.eeo
    e.gender, e.race_ethnicity, e.veteran_status, e.disability_status = ("Male", "Asian (South Asian)",
                                                                          "Not a protected veteran", "No disability")
    return p


# --------------------------------------------------------------------------- links and intake


def test_links() -> None:
    ref = wd.parse_ref(URL + "/apply/applyManually")
    assert ref is not None and (ref.tenant, ref.site) == ("adobe", "external_experienced")
    assert ref.api_url == ("https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/external_experienced/job/San-Jose/"
                           "2027-University-Graduate---Data-Scientist_R173462")
    assert ref.apply_url.endswith("/external_experienced/job/San-Jose/2027-University-Graduate---Data-Scientist_R173462"
                                  "/apply/applyManually")
    assert ref.account_key == "adobe.wd5.myworkdayjobs.com"
    site = wd.parse_ref(SITE_URL)
    assert site is not None and site.recruiting and site.api_url.startswith("https://wd1.myworkdaysite.com/wday/cxs/snapchat/snap/")
    assert site.account_key == "wd1.myworkdaysite.com/recruiting/snapchat/"
    assert wd.parse_ref("https://adobe.wd5.myworkdayjobs.com/en-US/external_experienced") is None
    assert detect_ats(SITE_URL) == "workday" and job_id_from_url(URL) == "r173462"
    assert normalize_url(URL + "/apply/applyManually") == normalize_url(URL)
    assert adapter_for(URL) is wd and adapter_for(SITE_URL) is wd
    assert wd.is_workday_host(URL) and not wd.is_workday_host("https://evil.example.com/myworkdayjobs.com")
    assert wd.is_submit_request("https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/x/apply/submit", "POST")
    assert not wd.is_submit_request("https://adobe.wd5.myworkdayjobs.com/wday/cxs/adobe/x/job/y", "GET")


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code, self._body = status, body

    def json(self) -> Any:
        return self._body


class FakeSession:
    def __init__(self, status: int, body: Any) -> None:
        self.status, self.body, self.urls = status, body, []

    def get(self, url: str, headers: Any = None, timeout: int = 0) -> FakeResponse:
        self.urls.append(url)
        return FakeResponse(self.status, self.body)


def test_intake() -> None:
    data = json.loads((FIXTURES / "workday_adobe_job.json").read_text(encoding="utf-8"))
    session = FakeSession(200, data)
    job = wd.fetch_job(URL, session)
    assert session.urls == [wd.parse_ref(URL).api_url]  # type: ignore[union-attr]
    assert (job.company, job.role_title, job.ats, job.job_id) == ("Adobe", "2027 University Graduate - Data Scientist",
                                                                   "workday", "R173462")
    assert "Graduating between December 2026 and June 2027" in job.description_text
    assert job.apply_url and job.apply_url.endswith("/apply/applyManually") and job.questions == []
    with pytest.raises(HelperError, match="--jd-file"):
        wd.fetch_job(URL, FakeSession(403, {"errorCode": "S22", "message": "permission denied"}))
    closed = json.loads(json.dumps(data))
    closed["jobPostingInfo"]["canApply"] = False
    with pytest.raises(HelperError, match="no longer takes applications"):
        wd.fetch_job(URL, FakeSession(200, closed))


@pytest.mark.parametrize("name,expected", [
    ("ADUS-Adobe Inc.", "Adobe"), ("100 Salesforce", "Salesforce"), ("ASML US, LLC. (US01)", "ASML"),
    ("Snap Inc.", "Snap"), ("NVIDIA", "NVIDIA"), ("", "Adobe"),
])
def test_company_names(name: str, expected: str) -> None:
    ref = wd.parse_ref(URL)
    assert wd.company_name({"hiringOrganization": {"name": name}}, ref) == expected  # type: ignore[arg-type]


def test_small_conversions() -> None:
    assert wd.phone_digits("+12062800258") == "2062800258" and wd.phone_digits("(206) 280-0258") == "2062800258"
    assert wp.date_digits("May 1, 2027", ["Month", "Day", "Year"]) == {"Month": "05", "Day": "01", "Year": "2027"}
    assert wp.date_digits("05/01/2027", ["Month", "Year"]) == {"Month": "05", "Year": "2027"}
    assert wp.date_digits("May 2027", ["Year"]) == {"Year": "2027"}
    assert wd.how_did_you_hear_terms("Career site", "Adobe")[:2] == ["Career site", "Adobe.com"]
    assert "masters" in wp.degree_aliases("Master of Science") and "bachelors" in wp.degree_aliases("Bachelor of Engineering")
    creds = wd.Credentials("alex@example.com", "secret-value")
    assert "secret-value" not in repr(creds) and "secret-value" not in str(creds)
    assert wp.step_kind("Create Account/Sign In") == "account" and wp.step_kind("Review") == "review"
    assert wp.step_kind("Application Questions 1 of 2") == "questions" and wp.step_kind("Self Identify") == "self_identify"


# --------------------------------------------------------------------------- the real pages, saved (scripts removed)

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


def open_saved(browser: Any, name: str) -> Any:
    page = browser.new_page(java_script_enabled=False)
    page.goto(f"file://{(FIXTURES / name).resolve()}")
    return page


def test_reading_the_saved_pages(browser: Any) -> None:
    page = open_saved(browser, "workday_my_information.html")
    found = {f["key"]: f for f in page.evaluate(wp.DISCOVER_JS)}
    assert wp.current_step(page) == "My Information" and not wp.next_is_submit(page)
    assert found["workday:source"]["type"] == "prompt" and found["workday:source"]["required"]
    assert found["workday:candidateIsPreviousWorker"]["options"] == ["Yes", "No"]
    assert found["workday:country"]["type"] == "select" and found["workday:countryRegion"]["label"] == "State"
    assert not found["workday:addressLine2"]["required"] and found["workday:phoneNumber"]["required"]
    page.close()

    page = open_saved(browser, "workday_questions.html")
    found = {f["label"]: f for f in page.evaluate(wp.DISCOVER_JS)}
    worked = found["Have you ever worked at Adobe in the following capacity:"]
    assert worked["type"] == "multi_select" and "I have not worked for Adobe in the past." in worked["options"]
    assert len(found) == 6 and all(f["required"] for f in found.values())
    page.close()

    page = open_saved(browser, "workday_disclosures.html")
    found = {f["key"]: f for f in page.evaluate(wp.DISCOVER_JS)}
    assert wp.step_kind(wp.current_step(page)) == "disclosures"
    assert found["workday:acceptTermsAndAgreements"]["type"] == "boolean"
    page.close()

    page = open_saved(browser, "workday_review.html")
    assert wp.current_step(page) == "Review" and wp.next_is_submit(page)
    with pytest.raises(RuntimeError, match="refusing to click Submit"):
        wp.click_next(page)
    page.close()

    for name, screen in (("workday_create_account.html", "create"), ("workday_sign_in.html", "sign_in"),
                         ("workday_sign_in_choices.html", "chooser"), ("workday_my_information.html", None)):
        page = open_saved(browser, name)
        assert wd.account_screen(page) == screen, name
        page.close()


def q(key: str, label: str, type_: str = "text", options: list[str] | None = None, required: bool = True,
      section: str = "questions") -> FormQuestion:
    return FormQuestion(label=label, field_name=f"workday:{key}", type=type_, options=options or [], required=required,
                        section=section)


def test_planning_workday_questions() -> None:
    job = Job(company="Adobe", role_title="Data Scientist", description_text="x", source="test", fetched_at="now")
    ctx = AnswerContext(profile=profile(), cfg=Config(), job=job, grad_date="May 2027", employment="full_time")
    questions = [
        q("source", "How Did You Hear About Us?", "prompt"),
        q("candidateIsPreviousWorker", "Have you been employed by Adobe in the past?", "radio", ["Yes", "No"]),
        q("country", "Country", "select", ["Canada", "United States Minor Outlying Islands", "United States of America"]),
        q("addressLine1", "Address Line 1", required=False), q("addressLine2", "Address Line 2", required=False),
        q("countryRegion", "State", "select", ["Alabama", "Washington"], required=False),
        q("phoneType", "Phone Device Type", "select", ["Mobile", "Personal"]),
        q("countryPhoneCode", "Country Phone Code", "prompt"), q("phoneNumber", "Phone Number"),
        q("extension", "Phone Extension", required=False),
        q("worked", "Have you ever worked at Adobe in the following capacity:", "multi_select",
          ["Employee", "Intern", "Other", "I have not worked for Adobe in the past."]),
        q("veteranStatus", "Please select your veteran status.", "select",
          ["I IDENTIFY WITH ONE OR MORE OF THE CLASSIFICATIONS OF PROTECTED VETERANS LISTED ABOVE",
           "I IDENTIFY AS A VETERAN, JUST NOT A PROTECTED VETERAN", "I AM NOT A VETERAN", "I DO NOT WISH TO SELF-IDENTIFY"],
          section="eeoc"),
        q("acceptTermsAndAgreements", "Check this box to confirm the statement above.", "boolean",
          ["Check this box to confirm the statement above."]),
        q("disability", "Please check one of the boxes below:", "multi_select",
          ["Yes, I have a disability (or previously had a disability)",
           "No, I do not have a disability and have not had one in the past", "I do not want to answer"], section="eeoc"),
        q("signed", "Date", "date"),
    ]
    plan = {a.field.removeprefix("workday:"): a for a in plan_answers(questions, ctx).answers}
    assert (plan["source"].kind, plan["source"].value) == ("auto", "Career site")
    assert plan["candidateIsPreviousWorker"].value == "No"
    assert plan["country"].value == "United States of America"  # not "United States Minor Outlying Islands"
    assert (plan["addressLine1"].value, plan["addressLine2"].value) == ("1 Example Way", "Apt 1")
    assert plan["countryRegion"].value == "Washington" and plan["phoneType"].value == "Mobile"
    assert plan["countryPhoneCode"].value == "United States of America (+1)" and plan["extension"].kind == "skip"
    assert plan["worked"].value == "I have not worked for Adobe in the past."
    assert plan["veteranStatus"].value == "I AM NOT A VETERAN"  # never "I identify as a veteran..."
    assert (plan["acceptTermsAndAgreements"].kind, plan["acceptTermsAndAgreements"].value) == ("auto", "Yes")
    assert plan["disability"].key == "eeo_disability"
    assert plan["disability"].value == "No, I do not have a disability and have not had one in the past"
    assert plan["signed"].key == "signature_date" and len(plan["signed"].value or "") == 10


# --------------------------------------------------------------------------- the whole flow, on a mock form


def flow_context(tmp_path: Path, **kw: Any) -> wd.FlowContext:
    job = Job(company="Mockco", role_title="Intern", description_text="x", source="test", fetched_at="now")
    ctx = AnswerContext(profile=profile(), cfg=Config(), job=job, grad_date="May 2027", employment="internship",
                        season="summer", season_year=2027)
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    base: dict[str, Any] = dict(company="Mockco", plan=lambda qs: plan_answers(qs, ctx).answers,
                                ask_claude=lambda answers, moved_on: None, resume_pdf=pdf, work=[], schools=[],
                                say=lambda m: None, pause=lambda: None, wait_seconds=20, poll_seconds=0.2)
    base.update(kw)
    return wd.FlowContext(**base)


def test_the_flow_fills_every_page_and_stops_on_review(browser: Any, tmp_path: Path) -> None:
    asked: list[str] = []

    def claude(answers: list[PlannedAnswer], moved_on: Any) -> None:
        for a in answers:
            if a.kind == "free_text":
                a.value, a.kind, a.status = "I like building tools people use every day.", "auto", "needs_review"
                asked.append(a.label)

    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'workday_mock_flow.html').resolve()}")
    result = wd.run_flow(page, flow_context(tmp_path, ask_claude=claude))
    assert result.reached_review and wp.current_step(page) == "Review"
    assert page.evaluate("() => window.submitted") is False  # Submit is Vatsal's
    assert page.evaluate("() => window.pagesSeen") == ["My Information", "Application Questions",
                                                        "Voluntary Disclosures", "Review"]
    assert asked == ["Why do you want to work at Mockco?"]
    assert wp.selected_items(page.locator("[data-automation-id=formField-source]")) == ["Mockco.com"]
    assert page.is_checked("#pw-no") and page.inner_text("#country") == "United States of America"
    assert page.input_value("#ph") == "5550100000" and page.input_value("#ext") == ""
    assert page.inner_text("#sponsor") == "No" and page.is_checked("#c2") and not page.is_checked("#c1")
    assert page.input_value("#why").startswith("I like building")
    assert [page.input_value(s) for s in ("#mm", "#dd", "#yy")] == ["05", "01", "2027"]
    assert page.inner_text("#gender") == "Male" and page.is_checked("#terms")
    assert all(r.ok for r in result.results), [r for r in result.results if not r.ok]
    page.close()


def test_a_question_only_vatsal_can_answer_waits_for_him(browser: Any, tmp_path: Path) -> None:
    said: list[str] = []
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'workday_mock_flow.html').resolve()}")
    page.click("#next")  # straight to Application Questions; nobody answers the written question
    result = wd.run_flow(page, flow_context(tmp_path, say=said.append, wait_seconds=3))
    assert not result.reached_review and wp.current_step(page) == "Application Questions"
    assert any("Answer these yourself" in s and "Why do you want to work at Mockco?" in s for s in said)
    page.close()


# --------------------------------------------------------------------------- the account step


@pytest.mark.parametrize("mode,has_account,expected,attempts", [
    ("new", False, "created", 0),
    ("exists", False, "signed in", 1),  # create first, Workday says it exists, sign in
    ("exists", True, "signed in", 1),  # an earlier application: sign in first
])
def test_account_step(browser: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, has_account: bool,
                      expected: str, attempts: int) -> None:
    monkeypatch.setattr(wd, "is_workday_host", lambda url: True)  # the mock is a local file
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'workday_mock_account.html').resolve()}#{mode}")
    out = wd.FlowResult()
    ctx = flow_context(tmp_path, credentials=wd.Credentials("alex@example.com", "right-pass"), has_account=has_account)
    assert wd.handle_account(page, ctx, out) and out.account == expected
    assert page.evaluate("() => window.signInAttempts") == attempts
    assert page.evaluate("() => window.honeypot") == ""  # the hidden bot trap stays empty
    page.close()


def test_a_wrong_password_is_never_retried(browser: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd, "is_workday_host", lambda url: True)
    said: list[str] = []
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'workday_mock_account.html').resolve()}#exists")
    ctx = flow_context(tmp_path, credentials=wd.Credentials("alex@example.com", "not-it"), has_account=True,
                       say=said.append, wait_seconds=2)
    assert not wd.handle_account(page, ctx, wd.FlowResult())
    assert page.evaluate("() => window.signInAttempts") == 1
    assert any("never retries a sign-in" in s for s in said)
    page.close()


def test_the_login_is_typed_only_on_workday(browser: Any) -> None:
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'workday_mock_account.html').resolve()}#new")
    with pytest.raises(RuntimeError, match="only typed on Workday"):
        wd._type_secret(page, "[data-automation-id=password]:visible", "right-pass")
    assert page.input_value("#create [data-automation-id=password]") == ""
    page.close()


# --------------------------------------------------------------------------- Claude's answers while the form is open


def test_claude_answers_while_the_form_waits(repo: Path) -> None:
    from helper.apply import _ask_claude_midfill
    from helper.config import Paths

    paths = Paths(repo)
    folder = paths.runs / "r1"
    folder.mkdir(parents=True)
    state = SimpleNamespace(run_id="r1", options={})
    job = Job(company="Mockco", role_title="Intern", description_text="x", source="test", fetched_at="now")
    answers = [PlannedAnswer(field="workday:q3", label="Why Mockco?", type="textarea", required=True, kind="free_text"),
               PlannedAnswer(field="workday:q1", label="Sponsorship?", type="select", required=True, kind="auto",
                             value="No", options=["Yes", "No"])]
    provided = folder / "answers_provided.json"

    def claude() -> None:
        time.sleep(0.4)
        provided.write_text(json.dumps({"answers": [{"field": "workday:q3", "answer": "Because — data"}]}))
        time.sleep(0.6)
        provided.write_text(json.dumps({"answers": [{"field": "workday:q3", "answer": "Because I like data tools."}]}))

    writer = threading.Thread(target=claude)
    writer.start()
    _ask_claude_midfill(paths, state, job, answers, lambda: False, wait_seconds=10, poll=0.1)
    writer.join()
    assert (answers[0].value, answers[0].status) == ("Because I like data tools.", "needs_review")
    needed = json.loads((folder / "questions_needed.json").read_text(encoding="utf-8"))
    assert [x["field"] for x in needed["questions"]] == ["workday:q3"] and "continues by itself" in needed["then_run"]


# --------------------------------------------------------------------------- `helper apply` on a Workday link


def test_apply_on_workday_goes_from_the_resume_to_the_fill(repo: Path, sample_profile_text: str,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    from typer.testing import CliRunner

    from helper.cli import app
    from helper.config import Paths, load_config
    from helper.convert import run_convert
    from helper.errors import EXIT_WAITING_FOR_CLAUDE
    from helper.latex import find_binary

    if find_binary("pdflatex") is None:
        pytest.skip("needs pdflatex")
    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    run_convert(Paths(repo), load_config(Paths(repo)))
    data = json.loads((FIXTURES / "workday_adobe_job.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(wd, "fetch_job", lambda url, session=None: wd.job_from_api(data, url, wd.parse_ref(url)))
    runner = CliRunner()
    started = runner.invoke(app, ["apply", URL])
    assert started.exit_code == EXIT_WAITING_FOR_CLAUDE, started.output
    (run_id,) = [p.name for p in (repo / "runs").iterdir()]
    shutil.copy(FIXTURES / "tailor_output_northwind.json", repo / "runs" / run_id / "tailor_output.json")
    assert runner.invoke(app, ["continue", run_id]).exit_code == EXIT_WAITING_FOR_CLAUDE  # the cover letter
    shutil.copy(FIXTURES / "cover_letter_northwind.json", repo / "runs" / run_id / "cover_letter_output.json")
    built = runner.invoke(app, ["continue", run_id])
    assert built.exit_code == 0, built.output
    assert "Workday shows its questions page by page" in built.output and f"helper fill {run_id}" in built.output
    dry = runner.invoke(app, ["fill", run_id, "--dry-run"])
    assert dry.exit_code == 0 and "page by page" in dry.output and "/apply/applyManually" in dry.output


def test_helpers_browser_never_offers_to_save_passwords(tmp_path: Path) -> None:
    from helper.browser import no_password_saving

    (tmp_path / "Default").mkdir()
    (tmp_path / "Default" / "Preferences").write_text(json.dumps({"profile": {"name": "x"}, "other": 1}))
    no_password_saving(tmp_path)
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text())
    assert prefs["credentials_enable_service"] is False and prefs["profile"] == {"name": "x", "password_manager_enabled": False}
    assert prefs["other"] == 1
    fresh = tmp_path / "fresh"
    no_password_saving(fresh)
    assert json.loads((fresh / "Default" / "Preferences").read_text())["credentials_enable_service"] is False
