"""iCIMS (Vatsal, 2026-11-15): reading postings, signing in or creating the account with the .env login only inside
iCIMS's own frame, filling each page by its labels, and stopping at Submit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from helper.adapters import adapter_for, generic, icims
from helper.answers import AnswerContext, plan_answers
from helper.errors import HelperError
from helper.models import Config, Job, Profile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROFILE = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
LOGIN = icims.Credentials("new@mockmail.test", "not-a-real-password-1", "alexexample")
POSTING = "https://careers-mockco.icims.com/jobs/77/data-intern/job?mode=submit_apply"


def test_links_and_routing() -> None:
    ref = icims.parse_ref(POSTING)
    assert ref == icims.IcimsRef("careers-mockco.icims.com", "77")
    assert ref.job_url == "https://careers-mockco.icims.com/jobs/77/job?in_iframe=1"
    assert ref.apply_url == "https://careers-mockco.icims.com/jobs/77/login"
    assert icims.parse_ref("https://careers-mockco.icims.com/jobs/search?ss=1") is None
    assert icims.parse_ref("https://icims.com.evil.example/jobs/77/x") is None
    assert adapter_for(POSTING) is icims
    assert "password=***" in repr(LOGIN) and LOGIN.password not in repr(LOGIN)


class FakeResponse:
    def __init__(self, status: int, text: str = "", url: str = "") -> None:
        self.status_code, self.text, self.url = status, text, url


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response, self.asked = response, []

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
        self.asked.append(url)
        return self.response


def posting_html() -> str:
    data = {"@type": "JobPosting", "title": "Data Intern", "hiringOrganization": {"name": "Mockco Bank"},
            "description": "<p>" + "Build data pipelines in Python and SQL for our analysts. " * 12 + "</p>",
            "jobLocation": {"address": {"addressLocality": "Coral Gables", "addressRegion": "Florida"}}}
    return f'<html><script type="application/ld+json">{json.dumps(data)}</script></html>'


def test_reading_a_posting() -> None:
    session = FakeSession(FakeResponse(200, posting_html(), "https://careers-mockco.icims.com/jobs/77/job"))
    job = icims.fetch_job(POSTING, session)
    assert (job.ats, job.company, job.role_title, job.job_id) == ("icims", "Mockco Bank", "Data Intern", "77")
    assert job.apply_url == "https://careers-mockco.icims.com/jobs/77/login" and job.location == "Coral Gables, Florida"
    assert session.asked == ["https://careers-mockco.icims.com/jobs/77/job?in_iframe=1"]
    closed = FakeSession(FakeResponse(200, "<html></html>", "https://careers-mockco.icims.com/jobs/search?notFound=1"))
    with pytest.raises(HelperError, match="no open job 77"):
        icims.fetch_job(POSTING, closed)


def test_a_company_page_that_applies_on_icims_becomes_an_icims_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from helper import apply

    jibe = Job(url="https://mockbank.jibeapply.com/jobs/6233", ats="generic", company="Mockco Bank",
               role_title="Data Intern", description_text="x", source="page", fetched_at="now",
               apply_url="https://careers-mockco.icims.com/jobs/6233/login")
    monkeypatch.setattr(generic, "fetch_job", lambda url: jibe)
    assert apply.fetch_job("https://mockbank.jibeapply.com/jobs/6233").ats == "icims"


# --------------------------------------------------------------------------- the flow, on a stand-in site


playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def page() -> Iterator[Any]:
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium isn't installed: {exc}")
        context = browser.new_context()

        def serve(route: Any) -> None:  # the stand-in site at a real-looking icims.com address
            name = "icims_app.html" if "in_iframe=1" in route.request.url else "icims_outer.html"
            route.fulfill(status=200, content_type="text/html", body=(FIXTURES / name).read_text(encoding="utf-8"))

        context.route("https://careers-mockco.icims.com/**", serve)
        yield context.new_page()
        browser.close()


def flow(login: icims.Credentials, said: list[str], pdf: Path) -> icims.FlowContext:
    job = Job(company="Mockco", role_title="Data Intern", description_text="x", source="icims_page", fetched_at="now")
    actx = AnswerContext(profile=PROFILE, cfg=Config(), job=job, grad_date="May 2027", employment="internship",
                         season="summer", season_year=2027)

    def ask_claude(answers: list[Any], moved_on: Any) -> None:
        for a in answers:
            if a.label.startswith("Why"):
                a.kind, a.value, a.status = "auto", "Mockco's data tools are what I want to build.", "needs_review"

    return icims.FlowContext(company="Mockco", plan=lambda qs: plan_answers(qs, actx).answers, ask_claude=ask_claude,
                             resume_pdf=pdf, work=[], schools=[], say=said.append, pause=lambda: None,
                             credentials=login, wait_seconds=1, poll_seconds=0.2)


def test_a_new_account_is_created_and_every_page_filled_up_to_submit(page: Any, tmp_path: Path) -> None:
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    said: list[str] = []
    page.goto("https://careers-mockco.icims.com/jobs/77/login")
    result = icims.run_flow(page, flow(LOGIN, said, pdf))
    rec = page.evaluate("() => window.recorded")
    assert (rec["email"], rec["username"], rec["password"]) == (LOGIN.email, LOGIN.username, LOGIN.password)
    # the email step's phone, its country code and the privacy consent (Schwab, Medpace, 2026-11-24); never arbitration
    assert rec["phone"] == "555-010-0000" and rec["countryCode"] == "United States of America (+1)"
    assert rec["arbitration"] is False
    assert (rec["first"], rec["last"]) == (PROFILE.tier1.first_name, PROFILE.tier1.last_name)
    assert (rec["resume"], rec["auth"]) == (pdf.name, "Yes") and rec["why"].startswith("Mockco's data tools")
    assert result.account == "created" and result.reached_review
    assert rec["submitted"] is False  # helper stops at "Submit Application"
    assert any("click 'Submit Application' yourself" in s for s in said), said
    # the company's own banking sign-in around the frame was never touched
    assert page.input_value("#userid") == "" and page.input_value("#db-password") == ""


def test_a_known_account_signs_in(page: Any, tmp_path: Path) -> None:
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    page.goto("https://careers-mockco.icims.com/jobs/77/login")
    known = icims.Credentials("known@mockmail.test", "not-a-real-password-2")
    result = icims.run_flow(page, flow(known, [], pdf))
    rec = page.evaluate("() => window.recorded")
    assert rec.get("signedIn") and not rec.get("created") and rec["password"] == known.password
    assert result.account == "signed in" and result.reached_review and rec["submitted"] is False


def test_the_login_is_never_typed_outside_icims_s_frame(page: Any) -> None:
    page.goto("https://careers-mockco.icims.com/jobs/77/login")
    assert icims._wait_frame(page) is not None
    assert not icims._type_secret(page.main_frame, page.locator("#db-password"), "secret")
    assert page.input_value("#db-password") == ""


def test_a_replaced_frame_is_picked_up_instead_of_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def one_page(page: Any, frame: Any, ctx: Any, out: Any, refilled: Any) -> bool:
        calls.append(frame)
        if len(calls) == 1:
            raise RuntimeError("Locator.count: Frame was detached")  # iCIMS reloaded its frame (City National)
        return True

    class Page:
        frames: list[Any] = []

        def wait_for_timeout(self, ms: float) -> None:
            pass

    monkeypatch.setattr(icims, "_wait_frame", lambda page, seconds=25: "the frame")
    monkeypatch.setattr(icims, "app_frame", lambda page: "the new frame")
    monkeypatch.setattr(icims, "_one_page", one_page)
    icims.run_flow(Page(), flow(LOGIN, [], Path("x.pdf")))
    assert calls == ["the new frame", "the new frame"]
    assert not icims._frame_replaced(RuntimeError("Target page, context or browser has been closed"))
    monkeypatch.setattr(icims, "_one_page", lambda *a: (_ for _ in ()).throw(ValueError("a real bug")))
    with pytest.raises(ValueError):
        icims.run_flow(Page(), flow(LOGIN, [], Path("x.pdf")))


def test_a_captcha_inside_the_application_frame_is_seen(page: Any) -> None:
    from helper.browser import captcha_visible

    page.context.route("https://newassets.hcaptcha.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<p>Select all images with a bus</p>"))
    challenge = "https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=challenge"
    page.set_content(f'''<iframe width="700" height="700" srcdoc="<iframe src='{challenge}' width='400'
                       height='500'></iframe>"></iframe>''')
    page.wait_for_timeout(500)
    assert captcha_visible(page)
    page.set_content(f'''<iframe width="700" height="700" srcdoc="<iframe src='{challenge}' width='1'
                       height='1'></iframe>"></iframe>''')  # the invisible check's idle frame isn't a challenge
    page.wait_for_timeout(500)
    assert not captcha_visible(page)


def test_full_page_iforms_are_read_but_never_get_the_login(page: Any) -> None:
    page.context.route("https://careers-mockco.icims.com/jobs/77/x/form?form=Voluntary**", lambda route: route.fulfill(
        status=200, content_type="text/html", body="""<h2>Voluntary Self-Identification</h2>
        <label><input type="radio" name="vet" value="no"> I AM NOT A PROTECTED VETERAN</label>
        <label for="pw">Password</label><input id="pw" type="password"><button>Submit</button>"""))
    page.goto("https://careers-mockco.icims.com/jobs/77/x/form?form=Voluntary")
    assert icims.app_frame(page) is page.main_frame  # Cotiviti's veteran form opens as a whole page
    assert not icims._type_secret(page.main_frame, page.locator("#pw"), "secret")  # the login stays in the frame
    page.goto("https://careers-mockco.icims.com/jobs/77/login")  # the company's page around the frame is never used
    page.wait_for_timeout(300)
    frame = icims.app_frame(page)
    assert frame is not page.main_frame and "in_iframe=1" in frame.url


def test_the_icims_context_takes_everything_apply_passes() -> None:  # Constellation, 2026-11-18: on_account crashed
    import dataclasses

    from helper.adapters import icims, workday

    icims_fields = {f.name for f in dataclasses.fields(icims.FlowContext)}
    workday_fields = {f.name for f in dataclasses.fields(workday.FlowContext)}
    assert workday_fields <= icims_fields, workday_fields - icims_fields


def test_a_disabled_forward_button_is_handed_over_not_waited_on() -> None:  # Constellation, 2026-11-18
    from helper.adapters import icims

    class Button:
        def __init__(self) -> None:
            self.clicked = False

        @property
        def first(self) -> "Button":
            return self

        def is_enabled(self) -> bool:
            return False

        def click(self, timeout: int = 0) -> None:
            self.clicked = True

    button = Button()

    class Frame:
        def locator(self, selector: str) -> Button:
            return button

        def wait_for_timeout(self, ms: float) -> None:
            pass

    assert icims._click(Frame(), {"i": 0}) is False and not button.clicked


def test_the_privacy_notice_button_goes_on_from_the_email_step() -> None:
    # Charles Schwab (2026-11-24): the email step's only button reads "I Acknowledge the Privacy Notice"
    class Frame:
        def evaluate(self, js: str) -> list[dict[str, Any]]:
            return [{"i": 0, "text": "Search jobs"}, {"i": 1, "text": "I Acknowledge the Privacy Notice"}]

    button, says_submit = icims._forward_button(Frame())
    assert button == {"i": 1, "text": "I Acknowledge the Privacy Notice"} and not says_submit
    assert not icims._NEXT_RE.match("I acknowledge the arbitration agreement")
