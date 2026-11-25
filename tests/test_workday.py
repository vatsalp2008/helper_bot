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
    ("Company 19 - John Hancock Life Insurance Company", "John Hancock Life Insurance Company"),
    ("GAIC Great American Insurance Company", "Great American Insurance Company"), ("C01 Carrier", "Carrier"),
    ("LAB Labcorp Early Development Laboratories", "Labcorp Early Development Laboratories"),
    ("LS3002 GE Vernova Operations", "GE Vernova Operations"), ("GE Vernova Operations", "GE Vernova Operations"),
    ("01 & 04 Woodward, Inc.", "Woodward"),  # two company codes (2026-11-21: the list showed "& 04 Woodward")
    ("3M Company", "3M Company"), ("7-Eleven, Inc.", "7-Eleven"),
    ("(0207) Sanofi US Services Inc.", "Sanofi US Services"),  # a code in front, in brackets (2026-11-24)
    ("NADQ Nasdaq, Inc.", "Nasdaq"),  # the code abbreviates the next word (2026-11-24)
    ("5674085 Merrill Lynch, Pierce, Fenner & Smith", "Merrill Lynch, Pierce, Fenner & Smith"),  # a 7-digit code
])
def test_company_names(name: str, expected: str) -> None:
    ref = wd.parse_ref(URL)
    assert wd.company_name({"hiringOrganization": {"name": name}}, ref) == expected  # type: ignore[arg-type]


def test_the_tenants_brand_beats_an_entity_the_posting_never_names() -> None:  # Signet, 2026-11-25
    ref = wd.parse_ref("https://signetjewelers.wd1.myworkdayjobs.com/SignetCorporateCareers/job/Support-Center---Irving-TX/"
                       "Software-Engineering-Intern--Digital-Commerce_REQ_84709")
    text = "<p>Signet Jewelers is the world's largest retailer of diamond jewelry, with Kay Jewelers and Zales.</p>"
    data = {"hiringOrganization": {"name": "Zale Delaware, Inc."}, "jobPostingInfo": {"jobDescription": text}}
    assert wd.company_name(data, ref) == "Signet Jewelers"  # type: ignore[arg-type]
    named = {"hiringOrganization": {"name": "Zale Delaware, Inc."},
             "jobPostingInfo": {"jobDescription": "<p>Zale Delaware and Signet Jewelers...</p>"}}
    assert wd.company_name(named, ref) == "Zale Delaware"  # type: ignore[arg-type]


def test_bank_of_americas_tenant_is_bank_of_america() -> None:  # 2026-11-25: read as "5674085 Merrill Lynch..."
    ref = wd.parse_ref("https://ghr.wd1.myworkdayjobs.com/en-US/lateral-us/job/Pennington/Software-Engineer-I--Java_26043787")
    data = {"hiringOrganization": {"name": "5674085 Merrill Lynch, Pierce, Fenner & Smith"}}
    assert wd.company_name(data, ref) == "Bank of America"  # type: ignore[arg-type]


def test_a_short_tenant_without_an_organization_is_an_acronym() -> None:  # HPE, 2026-11-24
    ref = wd.parse_ref("https://hpe.wd5.myworkdayjobs.com/Jobsathpe/job/Spring-Texas/Cloud-Security-Engagement-Intern_1216079")
    assert wd.company_name({"hiringOrganization": {"name": ""}}, ref) == "HPE"


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


ATTACHMENT_QUESTIONS = """
<div data-automation-id="applyFlowPage"><div role="group" aria-labelledby="primaryQuestionnaire-section">
 <div data-automation-id="formField-c3922fcc203c1001a836c6384e3a0009">
  <label id="label301"></label>
  <div id="rich-label301"><div data-automation-id="richText"><p><b>Please upload a current unofficial transcript.</b>
   Colorado applicants may redact information that would identify age, date of birth and/or dates of attendance at or
   graduation from an educational institution.<abbr title="required" class="requiredAsterisk">*</abbr></p></div></div>
  <div data-automation-id="attachments-FileUpload" aria-labelledby="rich-label301">
   <div data-automation-id="file-upload-drop-zone"><input type="file" data-automation-id="file-upload-input-ref">
   <button data-automation-id="select-files">Select files</button></div></div>
 </div>
 <div id="rich-label302"><div data-automation-id="richText"><p><b>Upload your undergraduate transcript.</b>
  <abbr title="required" class="requiredAsterisk">*</abbr></p></div></div>
 <div data-automation-id="formField-c3922fcc203c1001a836c6384e3a0010">
  <label id="label302"></label>
  <div data-automation-id="attachments-FileUpload" aria-labelledby="rich-label302"><input type="file"></div>
 </div>
 <div data-automation-id="formField-" data-fkit-id="resumeAttachments--attachments"><label id="label176"></label>
  <div data-automation-id="attachments-FileUpload" aria-labelledby="label176"><input type="file"></div></div>
</div></div>
"""


def test_an_attachment_question_is_read_by_its_rich_text_label(browser: Any) -> None:
    # The Aerospace Corporation, 2026-11-20: the transcript upload had an empty <label>, so it was never read and
    # Workday refused the page with the transcript missing
    page = browser.new_page(java_script_enabled=False)
    page.set_content(ATTACHMENT_QUESTIONS)
    found = {f["key"]: f for f in page.evaluate(wp.DISCOVER_JS)}
    page.close()
    first = found["workday:c3922fcc203c1001a836c6384e3a0009"]
    assert first["type"] == "file" and first["required"]
    assert first["label"].startswith("Please upload a current unofficial transcript.")
    second = found["workday:c3922fcc203c1001a836c6384e3a0010"]  # named only through aria-labelledby
    assert (second["type"], second["required"], second["label"]) == ("file", True, "Upload your undergraduate transcript.")
    assert len(found) == 2  # an upload with no words at all (the resume's) is still left alone
    docs = {"transcript": "/docs/Transcript NEU.pdf", "undergrad_transcript": "/docs/Transcript.pdf"}
    questions = [FormQuestion(label=f["label"], field_name=f["key"], type=f["type"], required=f["required"])
                 for f in found.values()]
    job = Job(company="The Aerospace Corporation", role_title="Grad Intern", description_text="x", source="test",
              fetched_at="now")
    plan = plan_answers(questions, AnswerContext(profile=profile(), cfg=Config(), job=job, grad_date="December 2027",
                                                 documents=docs))
    assert [(a.kind, a.key, a.value) for a in plan.answers] == [
        ("file", "transcript", "/docs/Transcript NEU.pdf"), ("file", "transcript", "/docs/Transcript.pdf")]


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


def test_education_with_a_school_box_named_school(browser: Any) -> None:  # Visa, Analog Devices, 2026-11-16
    from helper.dates import MonthYear
    assert wp.school_search_terms("Northeastern University") == ["Northeastern", "Northeastern University"]
    assert wp.school_search_terms("University of Mumbai") == ["Mumbai", "University of Mumbai"]
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'workday_mock_education.html').resolve()}")
    schools = [wp.SchoolEntry("Northeastern University", "Master of Science", "Computer Science",
                              MonthYear(2025, 9), MonthYear(2027, 5)),
               wp.SchoolEntry("University of Mumbai", "Bachelor of Engineering", "", MonthYear(2021, 8), MonthYear(2025, 5))]
    results = wp.fill_education(page, schools, lambda: None)
    assert all(r.ok for r in results), [(r.label, r.detail) for r in results]
    assert wp.selected_items(page.locator("[data-automation-id=formField-school]").nth(0)) == ["Northeastern University"]
    # not in this site's list: "Other", as the page itself asks
    assert wp.selected_items(page.locator("[data-automation-id=formField-school]").nth(1)) == ["Other"]
    assert "Other (as the page asks)" in results[1].detail
    # never the MBA for a Master of Science
    assert page.inner_text("#education-7--degree").startswith("Master’s or Equivalent")
    assert page.inner_text("#education-9--degree").startswith("Bachelor’s or Equivalent")
    assert wp.selected_items(page.locator("[data-automation-id=formField-fieldOfStudy]")) == ["Computer Science"]
    page.close()


def test_a_page_without_work_or_education_sections_is_not_a_miss(browser: Any) -> None:
    # DraftKings' campus portal (2026-11-19): My Experience had only the resume and LinkedIn, and helper listed both
    # jobs and both schools as "NOT filled: no room to add it"
    from helper.dates import MonthYear
    work = [wp.WorkEntry("Machine Learning Intern", "Kalini Industries", None, MonthYear(2024, 1), MonthYear(2024, 6),
                         False, "")]
    schools = [wp.SchoolEntry("Northeastern University", "Master of Science", "Computer Science",
                              MonthYear(2025, 9), MonthYear(2027, 5))]
    page = browser.new_page()
    page.set_content("<h3 id='Resume-section'>Resume/CV</h3><div role='group' aria-labelledby='Resume-section'>"
                     "<input type='file'></div>")
    assert wp.fill_work(page, work, lambda: None) == []
    assert wp.fill_education(page, schools, lambda: None) == []
    # a section that is there but offers no Add button still says so
    page.set_content("<h3>Work Experience</h3><div>Add your jobs</div>")
    results = wp.fill_work(page, work, lambda: None)
    assert [(r.ok, r.detail) for r in results] == [(False, "no room to add it")]
    page.close()


WORK_OR_OTHER = """
<div data-automation-id="applyFlowPage">
<div role="group" aria-labelledby="Work-or-Other-Experience-section">
  <h4 id="Work-or-Other-Experience-section">Work or Other Experience</h4>
  <div id="entries">
  <div role="group" aria-labelledby="Work-or-Other-Experience-1-panel">
    <h5 id="Work-or-Other-Experience-1-panel">Work or Other Experience 1</h5>
    <div data-automation-id="formField-jobTitle"><label for="workExperience-5--jobTitle">Job Title
      <abbr title="required">*</abbr></label><input id="workExperience-5--jobTitle" type="text"></div>
    <div data-automation-id="formField-companyName"><label for="workExperience-5--companyName">Company</label>
      <input id="workExperience-5--companyName" type="text"></div>
    <div data-automation-id="formField-location"><label for="workExperience-5--location">Location</label>
      <input id="workExperience-5--location" type="text"></div>
  </div></div>
  <button data-automation-id="add-button" onclick="add()">Add Another</button>
</div>
<div role="group" aria-labelledby="primaryQuestionnaire-section"><h4 id="primaryQuestionnaire-section">Questions</h4>
  <div data-automation-id="formField-why"><label for="why">Why Jabil?</label><input id="why" type="text"></div>
</div></div>
<script>
function add() {
  const n = document.querySelectorAll('#entries > [role=group]').length + 1, i = 5 + n;
  const g = document.createElement('div');
  g.setAttribute('role', 'group'); g.setAttribute('aria-labelledby', `Work-or-Other-Experience-${n}-panel`);
  g.innerHTML = `<h5 id="Work-or-Other-Experience-${n}-panel">Work or Other Experience ${n}</h5>` +
    ['jobTitle', 'companyName', 'location'].map(k => `<div data-automation-id="formField-${k}"><label ` +
      `for="workExperience-${i}--${k}">${k}</label><input id="workExperience-${i}--${k}" type="text"></div>`).join('');
  document.getElementById('entries').appendChild(g);
}
</script>"""


def test_a_work_or_other_experience_section_is_the_jobs_section(browser: Any) -> None:
    # Jabil (2026-11-20): helper looked for "Work Experience" only, so the jobs came to Claude as loose questions,
    # the planner typed the home address in as the job's location, the second job had "no room", and Workday refused
    # the page with eight "Page Error"s
    from helper.dates import MonthYear
    work = [wp.WorkEntry("Software Development Intern", "Kalini Industries", "Springfield, IL", MonthYear(2024, 1),
                         MonthYear(2024, 4), False, ""),
            wp.WorkEntry("Teaching Assistant", "Northeastern University", "Boston, MA", MonthYear(2026, 1),
                         MonthYear(2026, 4), False, "")]
    page = browser.new_page()
    page.set_content(WORK_OR_OTHER)
    assert wp.section_id(page, "Work-Experience-section", "workExperience", wp.WORK_TITLE) == \
        "Work-or-Other-Experience-section"
    questions, _ = wp.discover(page, skip_special=True)
    assert [q.label for q in questions] == ["Why Jabil?"]  # the job's fields are fill_work's
    results = wp.fill_work(page, work, lambda: None)
    assert all(r.ok for r in results), [(r.label, r.detail) for r in results]
    assert [page.input_value(f"#workExperience-{i}--companyName") for i in (5, 7)] == ["Kalini Industries",
                                                                                     "Northeastern University"]
    assert page.input_value("#workExperience-5--location") == "Springfield, IL"
    page.close()


def test_field_of_study_tries_a_shorter_search_and_says_what_the_list_showed(browser: Any) -> None:
    # Cadence (2026-11-19): 'Computer Science' found nothing in its list, and the note said only that
    assert wp.study_search_terms("Computer Science") == ["Computer Science", "Computer", "Science"]
    assert wp.study_search_terms("Computer Engineering") == ["Computer Engineering", "Computer", "Engineering"]
    assert wp.study_search_terms("Mathematics") == ["Mathematics"]
    assert wp.study_score("Computer Science", "Computer & Information Sciences") >= 0.85
    assert wp.study_score("Computer Science", "Computer Engineering") < 0.85
    fixture = (FIXTURES / "workday_mock_education.html").read_text(encoding="utf-8")
    listed = 'data-choices="Computer Science|Computer Engineering|Computer and Information Science"'
    assert listed in fixture

    def study_box(choices: str) -> Any:
        page = browser.new_page()
        page.set_content(fixture.replace(listed, f'data-choices="{choices}"'))
        return page, page.locator("[data-automation-id=formField-fieldOfStudy]")

    def accept(t: str) -> float:
        return wp.study_score("Computer Science", t)

    page, box = study_box("Computer &amp; Information Sciences|Computer Engineering|Information Technology")
    ok, chosen, _ = wp.choose_prompt(page, box, wp.study_search_terms("Computer Science"), accept, threshold=0.85,
                                     keep_existing=False)
    assert ok and wp.selected_items(box) == ["Computer & Information Sciences"]
    page.close()
    page, box = study_box("Computer Engineering|Electrical Engineering")
    ok, _, why = wp.choose_prompt(page, box, wp.study_search_terms("Computer Science"), accept, threshold=0.85,
                                  keep_existing=False)
    assert not ok and wp.selected_items(box) == [] and "(it listed: Computer Engineering)" in why
    page.close()
    page, box = study_box("Biology|Chemistry")
    ok, _, why = wp.choose_prompt(page, box, wp.study_search_terms("Computer Science"), accept, threshold=0.85,
                                  keep_existing=False)
    assert not ok and why.endswith("(the search listed nothing)")
    page.close()


def test_field_of_study_searches_the_distinctive_word_before_a_long_generic_list(browser: Any) -> None:
    # Signet (2026-11-19): the list matched any typed word and showed only its first entries, so 'Computer
    # Engineering' and then 'Engineering' listed Aerospace to Ceramic Engineering and never reached his field
    fixture = (FIXTURES / "workday_mock_education.html").read_text(encoding="utf-8")
    listed = 'data-choices="Computer Science|Computer Engineering|Computer and Information Science"'
    engineering = ("Aerospace Engineering|Agricultural Engineering|Architectural Engineering|Automotive Engineering|"
                   "Biomedical Engineering|Ceramic Engineering|Chemical Engineering|Civil Engineering|"
                   "Computer Engineering|Computer Science|Electrical Engineering")
    page = browser.new_page()
    page.set_content(fixture.replace(listed, f'data-choices="{engineering}" data-match="any-word" data-max="6"'))
    box = page.locator("[data-automation-id=formField-fieldOfStudy]")
    ok, chosen, why = wp.choose_prompt(page, box, wp.study_search_terms("Computer Engineering"),
                                       lambda t: wp.study_score("Computer Engineering", t), threshold=0.85,
                                       keep_existing=False)
    assert ok and wp.selected_items(box) == ["Computer Engineering"], why
    page.close()


def test_degree_choices_never_pick_another_named_degree() -> None:
    visa = ["Master of Business Administration - MBA ( (ISCED 7)",
            "Master’s or Equivalent (ISCED 7: MA, MSc, MEng, MBA, LLM, et cetera)", "Doctorate (ISCED 8)"]
    best = max(visa, key=lambda o: (wp.degree_score("Master of Science", o), -len(o)))
    assert best.startswith("Master’s or Equivalent")
    assert wp.degree_score("Master of Science", "Master of Science (MS)") > wp.degree_score("Master of Science",
                                                                                            "Master's Degree")
    assert wp.degree_score("Bachelor of Engineering", "Bachelor of Arts") < wp.degree_score("Bachelor of Engineering",
                                                                                         "Bachelor's Degree")
    # KLA offers no Master of Science: the closest master's still wins over nothing, never an MBA
    kla = ["Associates", "Masters of Technology (Masters)", "Bachelors of Arts or Science (Bachelors)",
           "Master of Business Administration"]
    pick = lambda d: max(kla, key=lambda o: (wp.degree_score(d, o), -len(o)))  # noqa: E731
    assert pick("Master of Science") == "Masters of Technology (Masters)"
    assert wp.degree_score("Master of Science", "Masters of Technology (Masters)") >= 0.85
    assert pick("Bachelor of Engineering") == "Bachelors of Arts or Science (Bachelors)"
    assert wp.degree_score("Master of Science", "Master of Business Administration") < 0.85
    # other common names for the two levels
    for name in ("Postgraduate", "M.Sc.", "Post Graduate Degree"):
        assert wp.degree_score("Master of Science", name) >= 0.85, name
    assert wp.degree_score("Bachelor of Engineering", "4 Year Degree") >= 0.85
    assert wp.degree_score("Bachelor of Engineering", "Postgraduate") < 0.85


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
    said: list[str] = []
    ctx = flow_context(tmp_path, credentials=wd.Credentials("alex@example.com", "right-pass"), has_account=has_account,
                       say=said.append)
    assert wd.handle_account(page, ctx, out) and out.account == expected
    assert page.evaluate("() => window.signInAttempts") == attempts
    assert page.evaluate("() => window.honeypot") == ""  # the hidden bot trap stays empty
    # the login is never printed (2026-11-20: "Workday account: signed in (<his email>)" went to the fill's output)
    assert f"Workday account: {expected}." in said and not any("alex@example.com" in s for s in said), said
    page.close()


LANGUAGES_PAGE = """
<div data-automation-id="applyFlowPage">
  <div role="group" aria-labelledby="Work-Experience-section"><h3 id="Work-Experience-section">Work Experience</h3>
    <div data-automation-id="formField-jobTitle"><label for="workExperience-1--jobTitle">Job Title</label>
      <input type="text" id="workExperience-1--jobTitle"></div></div>
  <div role="group" aria-labelledby="Languages-section"><h3 id="Languages-section">Languages</h3>
    <div data-automation-id="formField-language"><label>Language<abbr title="required">*</abbr></label>
      <div data-automation-id="multiselectInputContainer"><input type="text"></div></div>
    <div data-automation-id="formField-native"><label for="fluent">I am fluent in this language.</label>
      <input id="fluent" type="checkbox"></div>
    <div data-automation-id="formField-overall"><label>Overall<abbr title="required">*</abbr></label>
      <button aria-haspopup="listbox" onclick="document.getElementById('levels').hidden = false">Select One</button>
      <ul role="listbox" id="levels" hidden><li role="option">Beginner</li><li role="option">Fluent</li></ul></div>
  </div>
</div>
"""


def test_a_languages_entry_is_read_as_questions(browser: Any) -> None:
    # Advance Auto (2026-11-21): the Languages section was skipped like the job and school entries, so its required
    # Language and Overall stayed empty and Workday refused My Experience; now the profile's spoken_languages answer it
    page = browser.new_page()
    page.set_content(LANGUAGES_PAGE)
    questions, _ = wp.discover(page, skip_special=True)
    got = {q.label: (q.type, q.required, q.description, q.options) for q in questions}
    assert got == {
        "Language": ("prompt", True, "Languages section", []),
        "I am fluent in this language.": ("boolean", False, "Languages section", ["I am fluent in this language."]),
        "Overall": ("select", True, "Languages section", ["Beginner", "Fluent"]),
    }, got  # the job entry is still fill_work's
    page.close()


def test_a_verification_email_never_prints_the_login(browser: Any, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # Advance Auto (2026-11-21): "Workday sent a verification email to <his email>" went to the fill's output
    monkeypatch.setattr(wd, "is_workday_host", lambda url: True)
    outcome = wd._account_outcome
    monkeypatch.setattr(wd, "_account_outcome", lambda page, ctx, seconds=2: outcome(page, ctx, seconds))
    said: list[str] = []
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'workday_mock_account.html').resolve()}#verify")
    ctx = flow_context(tmp_path, credentials=wd.Credentials("alex@example.com", "right-pass"), has_account=False,
                       say=said.append, wait_seconds=2)
    assert not wd.handle_account(page, ctx, wd.FlowResult())
    assert page.evaluate("() => window.created") and page.evaluate("() => window.signInAttempts") == 0
    assert any("Workday sent a verification email" in s for s in said), said
    assert not any("alex@example.com" in s for s in said), said
    page.close()


def test_a_sign_in_form_that_shows_late_is_still_filled(browser: Any, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    # Symbotic (2026-11-20, 15 windows open): the form came after helper's wait, and helper then waited for Vatsal alone
    monkeypatch.setattr(wd, "is_workday_host", lambda url: True)
    monkeypatch.setattr(wd, "ACCOUNT_FORM_WAIT", 1)
    said: list[str] = []
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'workday_mock_account.html').resolve()}#late")
    ctx = flow_context(tmp_path, credentials=wd.Credentials("alex@example.com", "right-pass"), has_account=False,
                       say=said.append, wait_seconds=8)
    out = wd.run_flow(page, ctx)
    assert any("hasn't shown its sign-in form" in s for s in said), said
    assert page.evaluate("() => window.created") and out.account == "created"
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


@pytest.mark.parametrize("mode,reason", [
    ("locked", 'Workday says the account is locked (Workday: "Your account has been locked.")'),
    ("blocked", "Workday said: Your sign in request was blocked for <email>. Try again later."),
])
def test_a_blocked_sign_in_is_no_locked_account(browser: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                mode: str, reason: str) -> None:
    # Rockwell and Symbotic (2026-11-20) both said "the account is locked": "blocked" matched the pattern's letters,
    # and the pause never showed Workday's own words
    monkeypatch.setattr(wd, "is_workday_host", lambda url: True)
    said: list[str] = []
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'workday_mock_account.html').resolve()}#{mode}")
    ctx = flow_context(tmp_path, credentials=wd.Credentials("alex@example.com", "right-pass"), has_account=True,
                       say=said.append, wait_seconds=2)
    assert not wd.handle_account(page, ctx, wd.FlowResult())
    assert page.evaluate("() => window.signInAttempts") == 1  # never retried
    assert any(reason in s for s in said), said
    assert not any("alex@example.com" in s for s in said)  # the login is never printed
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

    # Workday makes helper fill the page again: the same question is answered from this run, without asking twice
    (folder / "questions_needed.json").unlink()
    again = [PlannedAnswer(field="workday:q3", label="Why Mockco?", type="textarea", required=True, kind="free_text")]
    _ask_claude_midfill(paths, state, job, again, lambda: False, wait_seconds=1, poll=0.1)
    assert again[0].value == "Because I like data tools." and not (folder / "questions_needed.json").exists()

    # Oracle draws the page anew after Submit and numbers its fields again (Ralliant, 2026-11-19): a long question is
    # found by its words; a short, common one isn't
    dod = "Are you, or have you been in the last 2 years, a “Covered DoD official”?"
    first = [PlannedAnswer(field="generic:field-11", label=dod, type="radio", required=True, kind="unknown",
                           options=["Yes", "No"])]
    provided.unlink()

    def claude_dod() -> None:
        time.sleep(0.3)
        provided.write_text(json.dumps({"answers": [{"field": "generic:field-11", "answer": "No"}]}))

    writer = threading.Thread(target=claude_dod)
    writer.start()
    _ask_claude_midfill(paths, state, job, first, lambda: False, wait_seconds=10, poll=0.1)
    writer.join()
    assert first[0].value == "No"
    (folder / "questions_needed.json").unlink()
    provided.unlink()
    redrawn = [PlannedAnswer(field="generic:field-17", label=dod, type="radio", required=True, kind="unknown",
                             options=["Yes", "No"]),
               PlannedAnswer(field="workday:q9", label="Why Mockco?", type="textarea", required=True, kind="free_text")]
    _ask_claude_midfill(paths, state, job, redrawn, lambda: False, wait_seconds=0.5, poll=0.1)
    assert redrawn[0].value == "No"  # found by its words under its new number
    assert redrawn[1].value is None  # "Why Mockco?" under another field is asked again, never guessed
    assert [x["field"] for x in json.loads((folder / "questions_needed.json").read_text())["questions"]] == ["workday:q9"]

    # Oracle keeps step 1's fields on later steps (JPMC, 2026-11-24): a best guess Claude confirmed with the same
    # value is settled for the rest of the run
    (folder / "questions_needed.json").unlink()
    race = "Select the races you identify with."
    guess = [PlannedAnswer(field="generic:dq-option-7", label=race, type="multi_select", required=False, kind="confirm",
                           value="Asian", options=["Asian", "White"])]

    def claude_race() -> None:
        time.sleep(0.3)
        provided.write_text(json.dumps({"answers": [{"field": "generic:dq-option-7", "answer": "Asian"}]}))

    writer = threading.Thread(target=claude_race)
    writer.start()
    _ask_claude_midfill(paths, state, job, guess, lambda: False, wait_seconds=10, poll=0.1)
    writer.join()
    assert guess[0].kind == "auto"
    (folder / "questions_needed.json").unlink()
    next_step = [PlannedAnswer(field="generic:dq-option-7", label=race, type="multi_select", required=False,
                               kind="confirm", value="Asian", options=["Asian", "White"])]
    _ask_claude_midfill(paths, state, job, next_step, lambda: False, wait_seconds=0.5, poll=0.1)
    assert next_step[0].kind == "auto" and not (folder / "questions_needed.json").exists()


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


def test_skills_and_certifications_match_only_the_same_thing(tmp_path: Path) -> None:
    from helper.apply import _certifications, _resume_skills
    from helper.config import Paths

    assert wp._skill_score("Python", "Python (Programming Language)") > 0.9 > wp._skill_score("Python", "Python Requests")
    assert wp._skill_score("Kubernetes", "Kubernetes") == 1.0 and wp._skill_score("Java", "JavaScript") < 0.9
    assert wp._skill_score("C", "C++") < 0.9 and wp._skill_score("C++", "C++ (Programming Language)") > 0.9
    assert wp._cert_date("June 26, 2026") == {"Month": "06", "Day": "26", "Year": "2026"} and wp._cert_date(None) == {}
    run = tmp_path / "run"
    run.mkdir()
    (run / "tailor_output.json").write_text(json.dumps({"skills_lines": [{"label": "Languages", "items": ["Java", "Go"]},
                                                                         {"label": "Cloud", "items": ["AWS", "Go"]}]}))
    assert _resume_skills(run) == ["Java", "Go", "AWS"]
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "certifications.yaml").write_text(
        "certifications:\n  - name: Example Cert\n    issuer: Example\n  - issuer: no name\n")
    assert [c["name"] for c in _certifications(Paths(tmp_path))] == ["Example Cert"]
    assert _certifications(Paths(tmp_path / "nowhere")) == []


def test_company_names_lose_codes_and_legal_suffixes() -> None:  # Dexcom, Rocket, 2026-11-16
    from helper.adapters.workday import company_name, parse_ref

    ref = parse_ref("https://dexcom.wd1.myworkdayjobs.com/Dexcom/job/Remote-California/SW-Development-Engineer-1_JR121398")
    for raw, clean in (("1 DexCom", "DexCom"), ("Rocket Limited Partnership", "Rocket"), ("100 Salesforce, Inc.", "Salesforce")):
        assert company_name({"hiringOrganization": {"name": raw}}, ref) == clean


def test_the_brand_in_the_title_beats_a_legal_entity_name() -> None:  # GE Vernova, 2026-11-24
    from helper.adapters.workday import company_name, parse_ref

    ref = parse_ref("https://gevernova.wd5.myworkdayjobs.com/Vernova_ExternalSite/job/Charleroi-USA/"
                    "GE-Vernova-Engineering-Automation-Internship---Summer-2027_R5059697-2")
    data = {"hiringOrganization": {"name": "GE Renewables Grid"},
            "jobPostingInfo": {"title": "GE Vernova Engineering Automation Internship - Summer 2027"}}
    assert company_name(data, ref) == "GE Vernova"
    # a legal name that already holds the brand, or a title that doesn't start with it, keeps the legal name
    data["hiringOrganization"]["name"] = "LS3002 GE Vernova Operations"
    assert company_name(data, ref) == "GE Vernova Operations"
    data = {"hiringOrganization": {"name": "GE Renewables Grid"}, "jobPostingInfo": {"title": "Software Intern"}}
    assert company_name(data, ref) == "GE Renewables Grid"


def test_a_remote_posting_names_its_country() -> None:  # GE Aerospace, 2026-11-17: "Remote", Germany only
    url = "https://geaerospace.wd5.myworkdayjobs.com/ge_externalsite/job/Remote/Applied-AI-Engineer-Intern_R5043333-1"
    data: dict[str, Any] = {"jobPostingInfo": {"title": "Applied AI Engineer Intern (m/f/d)", "location": "Remote",
                                               "country": {"descriptor": "Germany"}, "jobDescription": "<p>Python</p>"}}
    assert wd.job_from_api(data, url, wd.parse_ref(url)).location == "Remote, Germany"
    data["jobPostingInfo"]["country"] = {"descriptor": "United States of America"}
    assert wd.job_from_api(data, url, wd.parse_ref(url)).location == "Remote"
    data["jobPostingInfo"].update(location="Berlin, Germany", country={"descriptor": "Germany"})
    assert wd.job_from_api(data, url, wd.parse_ref(url)).location == "Berlin, Germany"


def test_a_site_that_needs_a_longer_password_gets_the_long_one() -> None:  # Visa, 2026-11-16
    from helper.adapters.workday import Credentials, _min_password_length

    class Body:
        def __init__(self, text: str) -> None:
            self.text = text

        def inner_text(self, timeout: int = 0) -> str:
            return self.text

    class Page:
        def __init__(self, text: str) -> None:
            self.body = Body(text)

        def locator(self, selector: str) -> Body:
            return self.body

    assert _min_password_length(Page("Password Requirements: A minimum of 12 characters. A numeric character")) == 12
    assert _min_password_length(Page("Create Account")) is None
    creds = Credentials("x@mockmail.test", "short-pw", long_password="a-much-longer-password")
    assert "longer" not in repr(creds) and "short" not in repr(creds)


def test_a_prompt_category_is_opened_and_a_real_choice_sticks() -> None:  # Samsung, Bloomberg, 2026-11-17
    # "How Did You Hear About Us?": clicking "Job Board" opens its own list; nothing is selected until a leaf is picked
    from helper.adapters.workday import any_source_but_referral

    class Page:
        def __init__(self) -> None:
            self.top = ["Job Board", "Social Media"]
            self.tree = {"Job Board": ["Employee Referral", "Indeed"], "Social Media": ["LinkedIn"]}
            self.shown: list[str] = []
            self.selected: list[str] = []
            page = self

            class Keyboard:
                def press(self, key: str) -> None:
                    page.shown = list(page.top) if key == "Enter" else []

            self.keyboard = Keyboard()

        def wait_for_timeout(self, ms: float) -> None:
            pass

        def locator(self, selector: str) -> Any:
            page = self
            if selector == wp.PROMPT_OPTIONS:
                class Options:
                    def all_inner_texts(self) -> list[str]:
                        return list(page.shown)

                    def nth(self, i: int) -> Any:
                        class Option:
                            def click(self) -> None:
                                label = page.shown[i]
                                if label in page.tree:
                                    page.shown = list(page.tree[label])
                                else:
                                    page.selected.append(label)
                                    page.shown = []
                        return Option()
                return Options()
            return SimpleNamespace(count=lambda: 1 if page.shown else 0)

    class Where:
        def __init__(self, page: Page) -> None:
            self.page = page

        def locator(self, selector: str) -> Any:
            page = self.page
            if selector == wp.SELECTED_ITEMS:
                return SimpleNamespace(evaluate_all=lambda js: list(page.selected))
            box = SimpleNamespace(click=lambda: None, fill=lambda text: None, type=lambda text, delay=0: None)
            return SimpleNamespace(first=box, last=SimpleNamespace(click=lambda: page.selected.pop()))

    page = Page()
    ok, shown, detail = wp.choose_prompt(page, Where(page), ["Social Media", "Other"], any_source_but_referral)
    assert (ok, shown) == (True, "Indeed") and page.selected == ["Indeed"]  # never the referral


def test_next_is_found_by_its_words_and_never_submit() -> None:  # Samsung, 2026-11-18
    clicked: list[str] = []

    class Button:
        def __init__(self, name: str, found: bool) -> None:
            self.name, self.found = name, found

        @property
        def first(self) -> "Button":
            return self

        def count(self) -> int:
            return 1 if self.found else 0

        def click(self, timeout: int = 0) -> None:
            clicked.append(self.name)

    class Page:
        def locator(self, selector: str) -> Button:
            return Button("by id", False)

        def get_by_role(self, role: str, name: Any = None) -> Button:
            return Button("Save and Continue", bool(name.match("Save and Continue")) and not name.match("Submit"))

    import helper.adapters.workday_page as page_mod

    original = (page_mod.next_is_submit, page_mod.current_step)
    page_mod.next_is_submit, page_mod.current_step = (lambda page: False), (lambda page: "My Information")
    try:
        page_mod.click_next(Page())
    finally:
        page_mod.next_is_submit, page_mod.current_step = original
    assert clicked == ["Save and Continue"]
    assert not page_mod.NEXT_WORDS.match("Submit")


def test_next_words_work_as_a_javascript_regex() -> None:  # Samsung, 2026-11-18: "(?i)" broke the selector
    assert "(?i)" not in wp.NEXT_WORDS.pattern and wp.NEXT_WORDS.flags & __import__("re").IGNORECASE
    assert wp.NEXT_WORDS.match("Save and Continue") and wp.NEXT_WORDS.match("NEXT") and not wp.NEXT_WORDS.match("Submit")


def test_a_next_button_that_arrives_late_is_waited_for() -> None:  # HPE, 2026-11-18: My Information right after sign-in
    import helper.adapters.workday_page as page_mod

    clicked: list[str] = []
    checks = {"n": 0}

    class Button:
        def __init__(self, there: bool) -> None:
            self.there = there
            self.first = self

        def count(self) -> int:
            return 1 if self.there else 0

        def click(self, timeout: int = 0) -> None:
            clicked.append("Next")

    class Page:
        def locator(self, selector: str) -> Button:
            checks["n"] += 1
            return Button(checks["n"] >= 3)

        def get_by_role(self, role: str, name: Any = None) -> Button:
            return Button(False)

        def wait_for_timeout(self, ms: int) -> None:
            pass

    original = (page_mod.next_is_submit, page_mod.current_step)
    page_mod.next_is_submit, page_mod.current_step = (lambda page: False), (lambda page: "My Information")
    try:
        page_mod.click_next(Page())
        assert clicked == ["Next"] and checks["n"] == 3
        checks["n"] = -100  # never arrives: helper says so instead of clicking
        with pytest.raises(LookupError):
            page_mod.click_next(Page(), wait_seconds=3)
    finally:
        page_mod.next_is_submit, page_mod.current_step = original


def test_follow_up_questions_that_appear_after_answers_are_filled_too(monkeypatch: Any, tmp_path: Path) -> None:
    """Marvell, 2026-11-18: each answer revealed another required question, three deep, and helper left the last."""
    import helper.adapters.workday as wd
    from helper.answers import PlannedAnswer
    from helper.models import FormQuestion

    def question(key: str, label: str) -> FormQuestion:
        return FormQuestion(label=label, field_name=key, type="select", required=True, options=["Yes", "No"])

    shown = [question("workday:a", "Are you a U.S. person?")]
    follow_ups = [question("workday:b", "Are you a citizen of Cuba, Iran, North Korea or Syria?"),
                  question("workday:c", "Select all countries in which you hold citizenship")]
    filled: list[str] = []

    def discover(page: Any, skip_special: bool = False, section: str = "") -> tuple[list[Any], dict[str, Any]]:
        return list(shown), {q.field_name: wp.Found() for q in shown}

    def fill_answer(page: Any, a: Any, found: Any, ctx: Any) -> Any:
        filled.append(a.field)
        if follow_ups:
            shown.append(follow_ups.pop(0))  # answering this one reveals the next
        return wd.FieldResult(a.field, a.label, True, a.value)

    def plan(questions: list[Any]) -> list[Any]:
        return [PlannedAnswer(field=q.field_name, label=q.label, type=q.type, required=True, kind="auto", value="No",
                              confidence=1.0, source="x") for q in questions]

    class Page:
        def wait_for_timeout(self, ms: int) -> None:
            pass

    said: list[str] = []
    monkeypatch.setattr(wd.wp, "discover", discover)
    monkeypatch.setattr(wd, "fill_answer", fill_answer)
    monkeypatch.setattr(wd, "_wait_for_fields", lambda page, seconds=20: 0)
    ctx = wd.FlowContext(company="Example", plan=plan, ask_claude=lambda answers, gone: None,
                         resume_pdf=tmp_path / "r.pdf", work=[], schools=[], say=said.append, pause=lambda: None)
    missing = wd._fill_step(Page(), "questions", "Application Questions", ctx, wd.FlowResult())
    assert missing == [] and filled == ["workday:a", "workday:b", "workday:c"]
    assert said[0] == "Application Questions: filling 1 question(s)." and "appeared" in said[1]


def test_workdays_something_went_wrong_page_is_recognized(browser: Any) -> None:  # HPE, Marvell, 2026-11-18
    page = browser.new_page()
    page.set_content("<div data-automation-id='progressBarActiveStep'>current step 1 of 5 My Information</div>"
                     "<h2>Something went wrong</h2><p>Please refresh the page and then try again.</p>")
    assert wp.went_wrong(page)
    page.set_content("<div data-automation-id='progressBarActiveStep'>current step 1 of 5 My Information</div>"
                     "<label>First Name</label><input>")
    assert not wp.went_wrong(page)
    page.close()


def test_a_closed_posting_ends_the_flow_at_once(browser: Any, tmp_path: Path) -> None:
    # HPE (2026-11-19): the reopened window found "The page you are looking for doesn't exist.", said "fill the
    # application in the window yourself", and would have waited a day
    said: list[str] = []
    page = browser.new_page()
    page.set_content("<header>HPE <a>Sign In</a></header><main><h2>The page you are looking for doesn't exist.</h2>"
                     "<button>Search for Jobs</button></main>")
    start = time.monotonic()
    result = wd.run_flow(page, flow_context(tmp_path, say=said.append))
    assert result.closed == "The page you are looking for doesn't exist." and not result.reached_review
    assert time.monotonic() - start < 10 and said == [
        "This posting is closed: Workday says 'The page you are looking for doesn't exist.'. Nothing was sent."]
    page.set_content("<h2>Software Engineer Intern</h2><p>This job posting is no longer available.</p>")
    assert wp.posting_gone(page) == "This job posting is no longer available."
    page.close()


def test_degree_choices_with_a_note_in_brackets() -> None:  # HPE, 2026-11-18: the required Degree stayed empty
    hpe = ["Other (other type of degree or diploma)", "High School or GED (±11 years of education)",
           "Associates (±13 years of education)", "Bachelors (±16 years of education)",
           "Masters (±18 years of education)", "Doctorate (±20 years of education)"]
    pick = lambda d: max(hpe, key=lambda o: (wp.degree_score(d, o), -len(o)))  # noqa: E731
    assert pick("Master of Science") == "Masters (±18 years of education)"
    assert wp.degree_score("Master of Science", "Masters (±18 years of education)") >= 0.85
    assert pick("Bachelor of Engineering") == "Bachelors (±16 years of education)"
    assert wp.degree_score("Bachelor of Engineering", "Bachelors (±16 years of education)") >= 0.85
    assert wp.degree_score("Master of Science", "Master of Education (MEd)") < 0.85  # a real other degree still loses


_WEBSITES = """<div role="group" aria-labelledby="Websites-section"><h3 id="Websites-section">Websites</h3>
  <div id="rows">ROWS</div>
  <button data-automation-id="add-button" type="button" onclick="addRow('')">Add Another</button>
</div>
<script>
let n = 0;
function addRow(value) {
  n += 1;
  const row = document.createElement('div');
  row.setAttribute('role', 'group');
  row.innerHTML = '<h4>Websites ' + n + '</h4><button type="button" aria-label="Delete Websites ' + n + '">Delete</button>'
    + '<label>URL*<input type="text"></label>';
  row.querySelector('input').value = value;
  row.querySelector('button').onclick = () => row.remove();
  document.getElementById('rows').appendChild(row);
}
</script>"""


def _websites_page(browser: Any, values: list[str]) -> Any:
    page = browser.new_page()
    page.set_content(_WEBSITES.replace("ROWS", ""))
    for v in values:
        page.evaluate("v => addRow(v)", v)
    return page


def test_blank_website_rows_take_the_links_and_leftovers_go(browser: Any) -> None:  # FreedomRoads, 2026-11-18
    """A page that opened while the account's rows were still loading kept a blank URL row, and Workday refused it."""
    links = ["https://www.linkedin.com/in/example/", "https://github.com/example", "https://example.github.io/"]
    values = lambda p: p.locator("[role=group] [role=group] input").evaluate_all("els => els.map(e => e.value)")  # noqa: E731
    page = _websites_page(browser, [""])
    (result,) = wp.fill_websites(page, links, pause=lambda: None)
    assert result.ok and values(page) == links  # the blank row took the first link, then two were added
    page.close()
    page = _websites_page(browser, [links[0], "", ""])
    (result,) = wp.fill_websites(page, links[:2], pause=lambda: None)
    assert result.ok and values(page) == links[:2] and "deleted 1 blank row" in result.detail
    page.close()
    page = _websites_page(browser, links)
    (result,) = wp.fill_websites(page, links, pause=lambda: None)
    assert (result.value, result.detail) == ("already listed", "")  # a second pass adds and deletes nothing
    page.close()


def test_a_first_add_that_does_nothing_is_clicked_again(browser: Any) -> None:  # Zillow, Bose, 2026-11-18
    """The first Add on a fresh page brought no row, so LinkedIn (always first) was reported as couldn't add."""
    links = ["https://www.linkedin.com/in/example/", "https://github.com/example", "https://example.github.io/"]
    page = _websites_page(browser, [])
    page.evaluate("""() => { const b = document.querySelector('[data-automation-id=add-button]'); let clicks = 0;
                             b.onclick = () => { clicks += 1; if (clicks > 1) addRow(''); }; }""")
    (result,) = wp.fill_websites(page, links, pause=lambda: None)
    values = page.locator("[role=group] [role=group] input").evaluate_all("els => els.map(e => e.value)")
    assert result.ok and values == links and result.detail == ""
    page.close()


def test_review_submit_waits_for_a_late_button_without_the_usual_ids(browser: Any) -> None:
    """Bloomberg Industry Group, 2026-11-18: helper reached Review, found no Submit button and clicked nothing."""
    page = browser.new_page()
    page.set_content("<div data-automation-id='progressBarActiveStep'>current step 5 of 5 Review</div><div id='f'></div>"
                     "<script>setTimeout(() => { document.getElementById('f').innerHTML = "
                     "'<button onclick=\"window.sent = (window.sent || 0) + 1\">Submit</button>'; }, 1500);</script>")
    assert wp.submit_review(page, wait_seconds=6)
    assert page.evaluate("() => window.sent") == 1  # clicked once
    page.close()
    page = browser.new_page()
    page.set_content("<div data-automation-id='progressBarActiveStep'>current step 3 of 5 Application Questions</div>"
                     "<button onclick='window.sent = 1'>Submit</button>")
    assert not wp.submit_review(page, wait_seconds=2)  # never off the Review page
    assert page.evaluate("() => window.sent") is None
    page.close()
