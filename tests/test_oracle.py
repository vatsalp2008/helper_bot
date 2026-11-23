"""Oracle Recruiting Cloud (Vatsal, 2026-11-19): the email step with only the application email (never the honeypot
box), Oracle's emailed code left to Vatsal, each section filled by its labels, the resume through Oracle's upload
button, and a stop at Submit."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from helper.adapters import adapter_for, oracle
from helper.answers import AnswerContext, plan_answers
from helper.models import Config, FormQuestion, Job, Profile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROFILE = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
POSTING = "https://mockco.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/123"


def test_links_and_routing() -> None:
    ref = oracle.parse_ref(POSTING)
    assert ref == oracle.OracleRef("mockco.fa.us2.oraclecloud.com", "en", "CX", "123")
    assert ref.apply_url == POSTING + "/apply/email"
    assert oracle.parse_ref("https://edel.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001/job/24969"
                            "/apply/section/2").job_id == "24969"
    assert oracle.parse_ref("https://careers.example.com/hcmUI/CandidateExperience/en/sites/CX/job/1") is None
    assert adapter_for(POSTING) is oracle


class _Answer:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code, self.payload = status, payload

    def json(self) -> Any:
        return self.payload


class _Session:
    def __init__(self, status: int = 200, payload: Any = None) -> None:
        self.status, self.payload, self.calls = status, payload, []

    def get(self, url: str, headers: Any = None, timeout: Any = None) -> _Answer:
        self.calls.append((url, headers))
        return _Answer(self.status, self.payload)


def test_the_posting_comes_from_oracle_s_job_api_without_the_page_around_it(monkeypatch: Any) -> None:
    # Ralliant (2026-11-19): the page read took in the map credits ("OpenStreetMap" became a keyword), "Similar Jobs"
    # and "TRENDING", and found no location
    page_text = ("Software Engineering Intern (Summer 2027) TRENDING Our codebase is written in C++. "
                 "Similar Jobs APPLY NOW © MapTiler © OpenStreetMap contributors | © Oracle Corporation")
    # Honeywell (2026-11-20): the page showed Oracle's email step, whose boxes aren't the application's questions
    email_step = [FormQuestion(label="Email Address", field_name="generic:primary-email", type="text", required=True),
                  FormQuestion(label="honeypot", field_name="generic:honey-pot", type="text", required=False)]
    monkeypatch.setattr(oracle.generic, "fetch_job", lambda url, session=None: Job(
        url=url, ats="generic", company="Mockco", role_title="Software Engineering Intern (Summer 2027)",
        description_text=page_text, source="page", fetched_at="now", apply_url=url, questions=list(email_step)))
    details = {"items": [{
        "Title": "Software Engineering Intern (Summer 2027)", "PrimaryLocation": "Beaverton, OR, United States",
        "secondaryLocations": [], "ExternalDescriptionStr": "<p><strong>About the Role</strong></p><p>Our codebase is "
        "written in C++.</p>", "ExternalResponsibilitiesStr": "", "ExternalQualificationsStr": "<ul><li>Pursuing a "
        "degree in Computer Science</li></ul>", "OrganizationDescriptionStr": "<div>About Mockco</div>",
        "CorporateDescriptionStr": None}]}
    session = _Session(payload=details)
    job = oracle.fetch_job(POSTING, session)
    assert (job.company, job.location, job.ats) == ("Mockco", "Beaverton, OR, United States", "oracle")
    assert "C++" in job.description_text and "Pursuing a degree in Computer Science" in job.description_text
    assert not any(w in job.description_text for w in ("OpenStreetMap", "TRENDING", "Similar Jobs"))
    assert job.apply_url == POSTING + "/apply/email"
    assert job.questions == []  # so the run answers Oracle's questions page by page, like every Oracle run
    url, headers = session.calls[0]
    assert "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?" in url
    assert "finder=ById;Id=%22123%22,siteNumber=CX" in url and headers == {"ora-irc-language": "en"}
    # when the API doesn't answer, the page's own text stays
    for quiet in (_Session(status=500), _Session(payload={"items": []})):
        job = oracle.fetch_job(POSTING, quiet)
        assert job.description_text == page_text and job.location is None and job.ats == "oracle"


@pytest.fixture
def page() -> Iterator[Any]:
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium isn't installed: {exc}")
        context = browser.new_context()
        body = (FIXTURES / "oracle_apply.html").read_text(encoding="utf-8")
        context.route("https://mockco.fa.us2.oraclecloud.com/**",
                      lambda route: route.fulfill(status=200, content_type="text/html", body=body))
        yield context.new_page()
        browser.close()


def flow(said: list[str], pdf: Path, email: str = "alex@mockmail.test") -> Any:
    job = Job(company="Mockco", role_title="Data Intern", description_text="x", source="page", fetched_at="now")
    profile = PROFILE.model_copy(deep=True)
    profile.tier1.citizenship = "Canada"  # the fictional sample profile has none
    profile.tier1.address.postal_code, profile.tier1.address.state = "55555", "WA"  # a ZIP past the list's first places
    eeo = profile.tier2.eeo
    eeo.race_ethnicity, eeo.hispanic_latino, eeo.disability_status = "Asian", "No", "No, I do not have a disability"
    actx = AnswerContext(profile=profile, cfg=Config(), job=job, grad_date="May 2027", employment="internship",
                         season="summer", season_year=2027)

    def ask_claude(answers: list[Any], moved_on: Any) -> None:
        for a in answers:
            if a.label.startswith("Why"):
                a.kind, a.value, a.status = "auto", "Mockco's data tools are what I want to build.", "needs_review"

    return oracle.FlowContext(company="Mockco", plan=lambda qs: plan_answers(qs, actx).answers, ask_claude=ask_claude,
                              resume_pdf=pdf, work=[], schools=[], say=said.append, pause=lambda: None,
                              credentials=oracle.Credentials(email, ""), wait_seconds=1, poll_seconds=0.2)


def test_every_section_is_filled_up_to_submit(page: Any, tmp_path: Path) -> None:
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    said: list[str] = []
    page.goto(POSTING)  # the posting page: the flow goes to its apply page
    result = oracle.run_flow(page, flow(said, pdf))
    rec = page.evaluate("() => window.recorded")
    assert (rec["email"], rec["honeypot"], rec["terms"]) == ("alex@mockmail.test", "", True)
    assert (rec["first"], rec["last"]) == (PROFILE.tier1.first_name, PROFILE.tier1.last_name) and rec["phone"]
    # Vertiv (2026-11-19): the country-code dropdown shares the "Phone Number" label, and the profile import box
    # would have Oracle parse the resume over the typed answers
    assert rec["code"] == "United States (+1)" and not any(c.isdigit() for c in rec["code"].replace("+1", ""))
    assert rec["imported"] == 0
    assert rec["nationality"] == "Canada"  # picked from the cx-select's grid, not just typed
    # Fortinet and Ralliant (2026-11-20): the ZIP box shows only its list's first places until a search; the profile's
    # own town is picked, not another one with the same ZIP code
    assert rec["zip"] == "55555, Springfield, Example, WA"
    # Ralliant and Honeywell (2026-11-20): a State pick after the ZIP cleared it; the State goes first now
    assert rec["state"] == "WA"
    assert (rec["auth"], rec["why"]) == ("Yes", "Mockco's data tools are what I want to build.")
    # Honeywell (2026-11-20): the race boxes were read one by one and all left blank
    assert (rec["races"], rec["hispanic"]) == (["Asian"], False)
    assert rec["disability"] == ["No, I do not have a disability and have not had one in the past"]
    # Honeywell (2026-11-20): Oracle's multi-select pills were never read; every statement is one his rules say No to
    assert rec["statements"] == ["No statements apply to me"]
    assert rec["resume"] == pdf.name  # through the Upload Resume button's file picker
    assert result.reached_review and result.account == "email entered"
    assert page.input_value("#sig") == PROFILE.tier1.full_name  # the e-signature, typed; Submit is Vatsal's
    assert rec["submitted"] is False
    assert any("click 'Submit' yourself" in s for s in said), said


def test_work_and_education_history_and_links_are_filled(page: Any, tmp_path: Path) -> None:
    # Fortinet ("You must add at least one experience and one education"), Honeywell and Vertiv (2026-11-20): entries
    # come only through Oracle's Add buttons, which helper never clicked; and "Link 1" took no link
    from helper.adapters.workday_page import SchoolEntry, WorkEntry
    from helper.dates import MonthYear

    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    said: list[str] = []
    ctx = flow(said, pdf)
    ctx.work = [WorkEntry(title="Software Development Intern", company="Example Corp", location="Seattle, WA",
                          start=MonthYear(2024, 5), end=MonthYear(2024, 8), current=False, description="- built things"),
                WorkEntry(title="Teaching Assistant", company="Example University", location="Seattle, WA",
                          start=MonthYear(2026, 1), end=None, current=True, description="")]
    ctx.schools = [SchoolEntry(school="Example University", degree="Master of Science", field_of_study="Computer Science",
                               start=MonthYear(2025, 9), end=MonthYear(2027, 12))]
    ctx.websites = ["https://linkedin.com/in/alex-example", "https://github.com/alex-example", ""]
    page.goto(POSTING + "/apply/documents#documents")
    result = oracle.run_flow(page, ctx)
    rec = page.evaluate("() => window.recorded")
    assert rec["experiences"] == [
        {"employer": "Example Corp", "title": "Software Development Intern", "start": "May 2024", "end": "August 2024",
         "current": False},
        {"employer": "Example University", "title": "Teaching Assistant", "start": "January 2026", "end": "",
         "current": True}]
    # the school is added though a job at the same university is listed already
    assert rec["educations"] == [{"school": "Example University", "degree": "Master's Degree",
                                  "major": "Computer Science", "start": "September 2025", "end": "December 2027"}]
    assert rec["links"] == ["https://linkedin.com/in/alex-example", "https://github.com/alex-example"]
    assert result.reached_review and not any("couldn't finish" in s for s in said), said
    # a second pass over the same page adds nothing twice
    page.goto(POSTING + "/apply/documents#documents")
    page.wait_for_selector("#timeline-add-experience-button")
    first, again = oracle._fill_history(page, ctx), oracle._fill_history(page, ctx)
    rec = page.evaluate("() => window.recorded")
    assert [r.ok for r in first] == [True, True, True] and again == []
    assert (len(rec["experiences"]), len(rec["educations"])) == (2, 1)


def test_required_fields_left_empty_are_named_before_submit(page: Any) -> None:
    # Honeywell and Ralliant (2026-11-20): helper said "stops here" while City, County and ZIP were empty; Oracle named
    # them only after SUBMIT
    page.set_content("""<div class="input-row input-row--filled"><label class="input-row__label input-row__label--required">
        State</label><input value="WA"></div>
      <div class="input-row"><label class="input-row__label input-row__label--required">City</label><input></div>
      <div class="input-row"><label class="input-row__label">Address Line 3</label><input></div>
      <div class="input-row"><label class="input-row__label input-row__label--required">Pick one</label>
        <ul role="list"><li><button class="cx-select-pill-section cx-select-pill-section--selected">A</button></li></ul></div>""")
    assert oracle._required_gaps(page) == ["City"]


def test_a_box_oracle_locked_with_its_value_is_left_alone(page: Any) -> None:
    # Vertiv and Ralliant (2026-11-20): the email Oracle verified sits in a read-only box; helper waited 30 seconds to
    # type into it, then said "These required fields look empty: Email Address"
    page.set_content("""<div class="input-row" data-helper-key="generic:email"><label class="input-row__label
        input-row__label--required">Email Address</label><input type="email" readonly value="alex@mockmail.test"></div>
      <div class="input-row" data-helper-key="generic:city"><label class="input-row__label input-row__label--required">
        City</label><input></div>""")
    assert oracle._required_gaps(page) == ["City"]
    assert oracle._locked(page.main_frame, "generic:email") and not oracle._locked(page.main_frame, "generic:city")


def test_oracle_s_planned_outage_page_says_so(page: Any) -> None:
    # Honeywell (2026-11-20): the maintenance page was reported as "couldn't find a Next button"
    page.set_content("<h1>Planned Outage</h1><p>The Oracle Cloud Fusion Applications service you are trying to access "
                     "is currently undergoing scheduled maintenance. Rest assured, the service will be restored once "
                     "the maintenance is complete.</p>")
    assert oracle._stage(page) == "outage"
    # a posting that asks for outage experience is no outage
    page.set_content("<div class='apply-flow'><h2>Plant Engineer</h2><p>Support planned outage work and scheduled "
                     "maintenance.</p><input name='x'></div>")
    assert oracle._stage(page) != "outage"


def test_the_fill_summary_names_the_site_signed_in_to() -> None:
    from helper.apply import _flow_summary

    result = oracle.FlowResult()
    result.account = "email entered"
    assert "Oracle sign-in: email entered." in _flow_summary(result, [], 1, "oracle")  # Ralliant's said "Workday account"
    assert "Workday account: email entered." in _flow_summary(result, [], 1, "workday")
    result.reached_review = True  # Stantec, 2026-11-23: an Oracle run said "Reached Workday's Review page"
    assert "Workday" not in _flow_summary(result, [], 1, "oracle")
    assert "Reached Workday's Review page." in _flow_summary(result, [], 1, "workday")


def test_an_entry_oracle_marks_invalid_is_finished(page: Any, tmp_path: Path) -> None:
    # Vertiv (2026-11-20): an education entry carried over without its Degree: "The Degree field is required."
    from helper.adapters.workday_page import SchoolEntry
    from helper.dates import MonthYear

    page.set_content("""<div class="beautiful-timeline-item beautiful-timeline-item--educations__1
          beautiful-timeline-item--invalid">Example University<button class="beautiful-timeline-item__edit-icon"
          id="edit">edit</button></div><div id="form"></div>
      <script>
        document.getElementById('edit').addEventListener('click', () => {
          document.getElementById('form').innerHTML = `<div class="standard-apply-flow-timeline-form"><div
            class="timeline-form-content"><form><div class="input-row"><label for="deg">Degree *</label>
            <select id="deg"><option value=""></option><option>Bachelor's Degree</option><option>Master's Degree</option>
            </select></div><div class="input-row input-row--filled"><label for="sch">School</label>
            <input id="sch" value="Example University"></div></form></div><div class="timeline-form-footer"><button
            class="button save-btn" type="button">Done</button></div></div>`;
          document.querySelector('.save-btn').addEventListener('click', () => {
            if (!document.getElementById('deg').value) return;
            window.saved = {degree: document.getElementById('deg').value, school: document.getElementById('sch').value};
            document.getElementById('form').innerHTML = '';
          });
        });
      </script>""")
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    ctx = flow([], pdf)
    ctx.schools = [SchoolEntry(school="Example University", degree="Master of Science", field_of_study="Computer Science",
                               start=MonthYear(2025, 9), end=MonthYear(2027, 12))]
    results = oracle._fill_history(page, ctx)
    assert [(r.ok, r.value) for r in results] == [(True, "completed")], results
    assert page.evaluate("() => window.saved") == {"degree": "Master's Degree", "school": "Example University"}


def test_oracle_s_diversity_groups_are_one_question_each(page: Any) -> None:
    # Honeywell, Ralliant, Vertiv and Fortinet (2026-11-20): the race boxes have no name, so each race was its own
    # question; each disability radio names its own option, so the question read as its first option
    page.goto(POSTING + "/apply/email#questions")
    questions = {q.label: q for q in oracle.generic.discover(page.main_frame)}
    race = questions["Select the races you identify with."]
    assert (race.type, race.options) == ("multi_select", ["American Indian or Alaska Native", "Asian",
                                                          "Black or African American", "White"])
    disability = questions["Do you have a disability?"]
    assert disability.type == "radio" and disability.options[1].startswith("No, I do not have a disability")
    assert questions["I am Hispanic or Latino."].type == "boolean"  # a group of one box keeps the box's own words
    assert not {"Asian", "White", "Yes, I have a disability, or have had one in the past"} & set(questions)


def test_a_hidden_terms_box_is_ticked_through_oracle_s_styled_box(page: Any, tmp_path: Path) -> None:
    # Fortinet (2026-11-19): helper looked only at visible boxes, so Oracle answered "You need to agree to the terms
    # and conditions" and the window was handed over at the first step
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    said: list[str] = []
    page.goto(POSTING + "/apply/email#hidden-terms")
    result = oracle.run_flow(page, flow(said, pdf))
    rec = page.evaluate("() => window.recorded")
    assert rec["terms"] is True and not rec["menu"]  # the site menu's checkbox stays as it was
    assert not rec.get("dialog")  # the privacy statement link inside the label wasn't clicked
    assert result.reached_review and not any("didn't take the email" in s for s in said), said


def test_oracle_s_idle_check_is_answered_while_helper_waits(page: Any, tmp_path: Path) -> None:
    # Ralliant and Vertiv (2026-11-19): half an hour at Submit brought "Are You Still With Us?". helper took the dialog
    # for a new page and filled the form again, and Oracle signed out under it ("couldn't find a Next button")
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    said: list[str] = []
    ctx = flow(said, pdf)
    ctx.wait_seconds = 3

    def ask_claude(answers: list[Any], moved_on: Any) -> None:  # like apply.py's wait: the page is watched meanwhile
        if not any(a.label.startswith("Why") for a in answers):
            return
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            if moved_on():
                return
            time.sleep(0.2)
        for a in answers:
            if a.label.startswith("Why"):
                a.kind, a.value, a.status = "auto", "Mockco's data tools are what I want to build.", "needs_review"

    ctx.ask_claude = ask_claude
    page.goto(POSTING + "/apply/email#idle")
    result = oracle.run_flow(page, ctx)
    rec = page.evaluate("() => window.recorded")
    assert rec.get("kept") == 2 and not rec.get("signedOut"), rec  # once while Claude answered, once at Submit
    assert rec["why"] == "Mockco's data tools are what I want to build."
    assert result.reached_review and sum("helper stops here" in s for s in said) == 1, said
    assert not any("couldn't find a Next button" in s for s in said), said
    assert oracle._NEXT_RE.search("Continue") and not oracle._NEXT_RE.search("Continue Working")


def test_oracle_s_profile_page_after_submit_is_the_confirmation(page: Any, tmp_path: Path) -> None:
    # Ralliant (2026-11-20): after Vatsal's SUBMIT, Oracle showed "Saving job application info" and his profile page;
    # helper read neither as a confirmation and logged "not submitted" when the window closed
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    said: list[str] = []
    ctx = flow(said, pdf)
    ctx.wait_seconds = 12
    page.goto(POSTING + "/apply/email#profile")
    result = oracle.run_flow(page, ctx)
    assert page.evaluate("() => window.recorded.submitted") and page.url.endswith("/my-profile")
    assert result.reached_review and oracle.is_confirmation(page), said
    assert sum("helper stops here" in s for s in said) == 1, said
    assert not any("Paused for you" in s for s in said), said


def test_a_redrawn_submit_page_is_not_filled_again(page: Any, tmp_path: Path) -> None:
    # Vertiv (2026-11-20): Oracle redrew a section while Vatsal reviewed at SUBMIT; helper took it for a new page,
    # asked Claude again and filled it again, which could undo what he had changed
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    said: list[str] = []
    ctx = flow(said, pdf)
    ctx.wait_seconds = 3
    page.goto(POSTING + "/apply/email#redraw")
    result = oracle.run_flow(page, ctx)
    assert page.locator("h3", has_text="Disability Information").count() == 1  # the redraw happened
    assert result.reached_review and sum("helper stops here" in s for s in said) == 1, said
    assert not page.evaluate("() => window.recorded.submitted")  # and nobody clicked SUBMIT


def test_a_job_search_page_says_oracle_left_the_application(page: Any, tmp_path: Path) -> None:
    # Ralliant (2026-11-20): Oracle ended a 13-hour session and showed its job search; helper said "couldn't find a
    # Next button"
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    said: list[str] = []
    page.goto(POSTING + "/apply/email")
    page.evaluate("""() => { history.replaceState(null, '', '/hcmUI/CandidateExperience/en/sites/CX/requisitions');
      document.body.innerHTML = '<h1>Search Jobs</h1><input placeholder="Job title, skill, keyword"><p>378 Open Jobs</p>'; }""")
    oracle.run_flow(page, flow(said, pdf))
    assert any("Oracle left the application" in s for s in said), said
    assert not any("couldn't find a Next button" in s for s in said), said


def test_the_welcome_back_prompt_is_not_a_confirmation(page: Any) -> None:
    # Ralliant (2026-11-19): a returning candidate's form opens with this, long before anything is sent
    page.set_content("<h2>You're all set, Alex!</h2><p>Welcome Back</p><p>We've got your profile from 12/31/1969 and "
                     "we loaded it into this application form. Do you want to use this profile, or import your data "
                     "from Indeed?</p><button>Use My Profile</button><button>Import Profile from Indeed</button>")
    assert not oracle.is_confirmation(page)
    page.set_content("<h1>Thank you for applying</h1><p>Your application was submitted.</p>")
    assert oracle.is_confirmation(page)


def test_oracle_s_emailed_code_is_left_to_vatsal(page: Any, tmp_path: Path) -> None:
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    said: list[str] = []
    page.goto(POSTING + "/apply/email#code")
    oracle.run_flow(page, flow(said, pdf))
    assert any("Oracle emailed you a verification code" in s for s in said), said
    assert page.input_value("#pin") == ""  # helper never types the code
    assert page.evaluate("() => window.recorded.first") is None  # nothing past the code step


def test_the_email_is_typed_only_on_oracle_s_own_page(tmp_path: Path) -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium isn't installed: {exc}")
        page = browser.new_page()
        page.set_content((FIXTURES / "oracle_apply.html").read_text(encoding="utf-8"))  # not an oraclecloud.com page
        said: list[str] = []
        ctx = flow(said, tmp_path / "r.pdf")
        ctx.wait_seconds = 0.5
        oracle._email_step(page, ctx, oracle.FlowResult())
        assert page.input_value("#primary-email-0") == ""
        assert any("type your email in the window" in s for s in said), said
        browser.close()


def test_the_page_is_read_once_oracle_has_drawn_it(page: Any, tmp_path: Path) -> None:
    # Vertiv (2026-11-19): the first look found only Oracle's empty shell and handed the window over
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    said: list[str] = []
    page.goto(POSTING + "/apply/email#slow")
    result = oracle.run_flow(page, flow(said, pdf))
    assert result.reached_review and not any("couldn't find a Next button" in s for s in said), said


def test_a_site_named_for_a_division_takes_the_posting_s_own_employer_name() -> None:
    # Honeywell (2026-11-20): its Oracle site is "Aerospace", so the runs, tracker and list said "Aerospace", and the
    # earlier-applications note matched GE Aerospace and Heart Aerospace
    text = ("Join Honeywell Aerospace. At Honeywell Aerospace we build the future of flight. Honeywell Aerospace "
            "Technologies serves pilots. Interns at Honeywell Aerospace grow fast. About Aerospace programs.")
    assert oracle.fuller_employer("Aerospace", text) == "Honeywell Aerospace"
    # a sentence's own words are no part of a name, and a full name stays
    assert oracle.fuller_employer("Ralliant", "About Ralliant. At Ralliant we test. Ralliant builds. Join Ralliant.") \
        == "Ralliant"
    assert oracle.fuller_employer("Vertiv Holdings", "Vertiv Holdings and More Vertiv Holdings") == "Vertiv Holdings"
    assert oracle.fuller_employer("Fortinet", "Fortinet is hiring.") == "Fortinet"
