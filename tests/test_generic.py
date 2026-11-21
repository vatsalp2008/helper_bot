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


def test_a_video_player_and_a_get_notified_box_are_not_an_application_form(browser: Any) -> None:
    # Disney's posting page (2026-11-18): a "get notified" email box plus a video player's seek and volume sliders
    # passed for a form, and helper filled the job-alert sign-up and asked for Submit
    page = browser.new_page()
    page.set_content("<body><h1>Data Engineering Intern</h1><p>" + "Build data pipelines for content analytics. " * 20
                     + "</p><form><label>Get notified about similar jobs <input type=email id=notifiedEmail></label>"
                     "</form><div class=plyr><input type=range id=plyr-seek-1><input type=range id=plyr-volume-1>"
                     "</div></body>")
    assert generic._form_frame(page) is None
    assert generic.scan_page(page).questions == []
    page.close()


def test_an_oracle_posting_is_read_whole(browser: Any) -> None:
    # Atlantic Health (2026-11-19): the page's JobPosting data held only the description, so "PharmD student" and the
    # work authorization rule in its qualifications never reached the eligibility check
    description = ("<p>The ACO team is seeking a Pharmacy (Data Analytics) Intern.</p>"
                   + "<p>Analyze pharmacy and clinical data to find trends.</p>" * 6)
    ld = json.dumps({"@context": "http://schema.org", "@type": "JobPosting", "title": "Pharmacy (Data Analytics) Intern",
                     "description": description,
                     "hiringOrganization": {"@type": "Organization", "name": "Candidate Experience site"}})
    html = (f'<html><head><script type="application/ld+json">{ld}</script></head><body>'
            f'<h1>Pharmacy (Data Analytics) Intern</h1>{description}</body></html>')
    details = {"items": [{"Title": "Pharmacy (Data Analytics) Intern", "ExternalDescriptionStr": description,
                          "ExternalResponsibilitiesStr": None,
                          "ExternalQualificationsStr": "<ul><li>PharmD student entering the third professional year."
                          "</li><li>Must be legally authorized to work in the United States as a U.S. citizen, "
                          "national, asylee, refugee, or lawful permanent resident.</li></ul>"}]}
    asked: list[str] = []

    def serve(route: Any) -> None:
        if "recruitingCEJobRequisitionDetails" in route.request.url:
            asked.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(details))
        else:
            route.fulfill(status=200, content_type="text/html", body=html)

    page = browser.new_page()
    page.route("https://mockhealth.fa.us2.oraclecloud.com/**", serve)
    page.goto("https://mockhealth.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/34551")
    _, _, text = generic.read_posting(page)
    assert "PharmD student" in text and "asylee" in text and "clinical data" in text
    assert len(asked) == 1 and "siteNumber%3DCX_1001" in asked[0] and "Id%3D%2234551%22" in asked[0]
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


def test_a_generic_site_name_is_not_the_employer() -> None:  # Fortinet on Oracle, 2026-11-16
    from helper.adapters.generic import _GENERIC_SITE_NAME, copyright_owner
    assert _GENERIC_SITE_NAME.match("Candidate Experience site") and not _GENERIC_SITE_NAME.match("Fortinet")
    assert copyright_owner("Contact Us\nCopyright © 2023 Fortinet, Inc. All Rights Reserved. Terms") == "Fortinet"
    assert copyright_owner("© 2019-2026 Laborie Medical Technologies Corp.") == "Laborie Medical Technologies"
    assert copyright_owner("All rights reserved") == ""


def test_only_a_final_submit_button_counts_as_submitting() -> None:  # Fortinet on Oracle, 2026-11-16
    from playwright.sync_api import sync_playwright

    from helper.adapters.generic import SUBMIT_WATCH_JS, submit_clicked
    html = ("<button id=a>Apply With LinkedIn</button><button id=b>Apply Now</button><button id=c>Next</button>"
            "<button id=d>Submit Application</button>")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium isn't installed: {exc}")
        page = browser.new_page()
        page.set_content(html)
        page.evaluate(SUBMIT_WATCH_JS)
        for b in ("#a", "#b", "#c"):
            page.click(b)
        assert not submit_clicked(page)
        page.click("#d")
        assert submit_clicked(page)
        browser.close()


def test_a_company_sites_draft_saves_are_not_a_submit() -> None:  # Boston Scientific on Eightfold, 2026-11-17
    from helper.adapters.generic import is_submit_request
    assert not is_submit_request("https://bostonscientific.eightfold.ai/api/apply/v2/jobs/563602813862390", "POST")
    assert not is_submit_request("https://acme.com/careers/application/submit", "POST")  # the Submit click decides


def test_a_rippling_posting_names_the_employer_not_the_hiring_site() -> None:  # 2026-11-18
    from helper.adapters.generic import rippling_employer

    assert rippling_employer("https://ats.rippling.com/raptor-maps-inc/jobs/be25d98e") == "Raptor Maps"
    assert rippling_employer("https://ats.rippling.com/apexanalytix-careers/jobs/101ae05c").lower() == "apexanalytix"
    assert rippling_employer("https://ats.rippling.com/jobs/123") == ""


def test_a_ukg_posting_names_the_employer_by_its_own_site() -> None:  # CAS and Melaleuca, 2026-11-20
    from helper.adapters.generic import _GENERIC_SITE_NAME, ukg_employer

    # UKG's host gave "Unknown" (recruiting.ultipro.com) and "Recruiting2" (recruiting2.ultipro.com); the employer
    # is named only by the footer link to its own site, and spelled as its description spells it
    assert _GENERIC_SITE_NAME.match("Recruiting2")
    assert ukg_employer("https://www.cas.org/careers", "Posting Details ... CAS uses intuitive technology") == "CAS"
    assert ukg_employer("https://melaleucajobs.com/", "At Melaleuca, you can feel it immediately.") == "Melaleuca"
    assert ukg_employer("https://melaleucajobs.com/", "A wellness company in Idaho Falls.") == ""  # never a guess
    assert ukg_employer("", "CAS uses intuitive technology") == ""


_FABRIC_SELECT = """<form>
<div class="field"><label for="fab-select1">State *</label>
  <div class="fab-Select"><div><button data-menu-id="fab-menu1" aria-haspopup="true" type="button">–Select–</button></div>
  <select id="fab-select1" name="state.value" required
          style="opacity:0;position:absolute;width:0;height:0"><option value=""></option></select></div>
</div>
</form>
<div id="fab-menu1" role="menu" style="display:none">
  <div role="menuitem" id="1">Alabama</div><div role="menuitem" id="47">Virginia</div>
  <div role="menuitem" id="48">Washington</div>
</div>
<script>
document.querySelector('button').onclick = () => { document.getElementById('fab-menu1').style.display = 'block'; };
for (const item of document.querySelectorAll('[role=menuitem]')) item.onclick = () => {
  const select = document.getElementById('fab-select1');
  select.innerHTML = '<option value="' + item.id + '" selected>' + item.innerText + '</option>';
  document.querySelector('button').innerText = item.innerText;
  document.getElementById('fab-menu1').style.display = 'none';
};
</script>"""


def test_a_styled_dropdown_with_an_empty_select_is_picked_from_its_menu(browser: Any, tmp_path: Path) -> None:
    """BambooHR keeps a dropdown's choices in a pop-up menu, and its real <select> stays empty until a pick; its
    state list spells the names out (All Options, 2026-11-18)."""
    from helper.answers import PlannedAnswer

    page = browser.new_page()
    page.set_content(_FABRIC_SELECT)
    (question,) = generic.discover(page)
    assert (question.field_name, question.label, question.type, question.options) == (
        "generic:state.value", "State", "select", [])
    state = PlannedAnswer(field=question.field_name, label="State", type="select", required=True, kind="auto",
                          value="WA", confidence=1.0, source="profile address")
    (result,) = generic.fill_form(page, [state], tmp_path / "r.pdf", country=None, city=None, pause=lambda: None)
    assert (result.ok, result.value) == (True, "Washington")
    assert page.evaluate("() => document.getElementById('fab-select1').value") == "48"
    page.set_content(_FABRIC_SELECT)
    state.value = "Ontario"
    (result,) = generic.fill_form(page, [state], tmp_path / "r.pdf", country=None, city=None, pause=lambda: None)
    assert not result.ok and "no option matches 'Ontario'" in result.detail
    page.close()


def test_boxes_named_anew_on_every_load_are_found_by_their_label(browser: Any, tmp_path: Path) -> None:
    """Rippling gives its inputs random names on each page load, so the names helper planned with were gone by
    the fill (Raptor Maps, 2026-11-18)."""
    from helper.answers import PlannedAnswer

    page = browser.new_page()
    page.set_content("""<form><div><label for="a1">First name *</label><input id="a1" name="Zq81kd" required></div>
        <div><label for="a2">Email *</label><input id="a2" name="Pp0x7" required></div></form>""")
    first = PlannedAnswer(field="generic:HXr2ADQd0", label="First name", type="text", required=True, kind="auto",
                          value="Alex", confidence=1.0, source="profile")
    email = PlannedAnswer(field="generic:JvrSJysdPe", label="Email", type="text", required=True, kind="auto",
                          value="alex@example.com", confidence=1.0, source="profile")
    results = generic.fill_form(page, [first, email], tmp_path / "r.pdf", country=None, city=None, pause=lambda: None)
    assert [r.ok for r in results] == [True, True]
    assert page.evaluate("() => [document.getElementById('a1').value, document.getElementById('a2').value]") == [
        "Alex", "alex@example.com"]
    page.close()


def test_upload_boxes_named_file_input_take_the_words_beside_them(browser: Any) -> None:
    """BambooHR's upload boxes are both aria-label="file-input"; the cover letter box got the resume (All Options,
    2026-11-18)."""
    page = browser.new_page()
    page.set_content("""<form>
        <div><p>Cover Letter*</p><div><button type="button">Upload</button><input type="file" aria-label="file-input"></div></div>
        <div><p>Resume*</p><div><button type="button">Upload</button><input type="file" aria-label="file-input"></div></div>
        </form>""")
    assert [q.label for q in generic.discover(page)] == ["Cover Letter", "Resume"]
    page.close()


def test_the_country_is_picked_before_the_state(browser: Any, tmp_path: Path) -> None:
    """A country pick can reset the state list (All Options' BambooHR, 2026-11-18)."""
    from helper.answers import PlannedAnswer

    page = browser.new_page()
    page.set_content("""<form>
        <div><label for="st">State *</label><select id="st" name="state"><option></option><option>Washington</option></select></div>
        <div><label for="co">Country *</label><select id="co" name="country"><option></option><option>United States</option></select></div>
        </form>
        <script>document.getElementById('co').onchange = () => { document.getElementById('st').value = ''; };</script>""")

    def choice(field: str, label: str, key: str, value: str) -> PlannedAnswer:
        return PlannedAnswer(field=field, label=label, key=key, type="select", required=True, kind="auto", value=value,
                             confidence=1.0, source="profile address")

    results = generic.fill_form(page, [choice("generic:state", "State", "address_state", "Washington"),
                                       choice("generic:country", "Country", "address_country", "United States")],
                                tmp_path / "r.pdf", country=None, city=None, pause=lambda: None)
    assert all(r.ok for r in results)
    assert page.evaluate("() => [document.getElementById('st').value, document.getElementById('co').value]") == [
        "Washington", "United States"]
    page.close()


RIPPLING = f"file://{(FIXTURES / 'rippling_form.html').resolve()}"


def test_a_rippling_form_is_read_whole(browser: Any) -> None:
    """Rippling's uploads sit in drop-zone labels, its dropdowns are role=combobox, and its custom questions are
    role=radio divs with the question beside them: helper read the focus area as "Full Stack", asked Vatsal for the
    phone code's "Search" box, and never saw the sponsorship dropdowns (Raptor Maps, apexanalytix, 2026-11-18)."""
    page = browser.new_page()
    page.goto(RIPPLING)
    got = {q.label: q for q in generic.discover(page)}
    assert list(got) == ["Résumé", "First name", "Pronouns", "Phone number", "Cover letter",
                         "Do you currently require Visa Sponsorship?",
                         "Are you able to work in Greensboro, NC May 2027 through August 2027?", "Gender"]
    assert (got["Résumé"].type, got["Résumé"].required) == ("file", True)
    assert (got["Pronouns"].type, got["Pronouns"].options) == ("select", ["He/Him", "She/Her", "They/Them"])
    assert (got["Gender"].type, got["Gender"].options) == ("select", ["Male", "Female", "Decline to self-identify"])
    visa = got["Do you currently require Visa Sponsorship?"]
    assert (visa.type, visa.options) == ("radio", ["Strong No", "No", "Yes", "Strong Yes"])
    # Rippling draws the required mark with CSS (::after "*"), so it's in no text (apexanalytix, 2026-11-18)
    assert visa.required and not got["Gender"].required and not got["Pronouns"].required
    where = got["Are you able to work in Greensboro, NC May 2027 through August 2027?"]
    assert (where.type, where.options, where.required) == ("radio", ["Yes", "No", "I plan to relocate."], True)
    # reading the dropdowns' choices picked nothing
    assert page.evaluate("() => [document.getElementById('f20').value, document.getElementById('f78').innerText]") == [
        "", "Select..."]
    page.close()


def test_a_rippling_form_is_filled(browser: Any, tmp_path: Path) -> None:
    from helper.answers import PlannedAnswer

    page = browser.new_page()
    page.goto(RIPPLING)
    questions = {q.label: q for q in generic.discover(page)}

    def answer(label: str, value: str) -> PlannedAnswer:
        q = questions[label]
        return PlannedAnswer(field=q.field_name, label=label, type=q.type, required=q.required, kind="auto",
                             value=value, confidence=1.0, source="profile")

    page.goto(RIPPLING)  # a new load names the boxes anew, as Rippling does
    results = generic.fill_form(page, [answer("Do you currently require Visa Sponsorship?", "No"),
                                       answer("Are you able to work in Greensboro, NC May 2027 through August 2027?",
                                              "I plan to relocate."),
                                       answer("Gender", "Male"), answer("Pronouns", "He/Him")],
                                tmp_path / "r.pdf", country=None, city=None, pause=lambda: None)
    assert [(r.ok, r.value) for r in results] == [(True, "No"), (True, "I plan to relocate."), (True, "Male"),
                                                  (True, "He/Him")], [r.detail for r in results]
    assert page.evaluate("""() => [
        document.querySelector('[role=radio][aria-label=No]').getAttribute('aria-checked'),
        document.querySelector('input[value="I plan to relocate."]').checked,
        document.getElementById('f78').innerText, document.getElementById('f20').value]""") == [
        "true", True, "Male", "He/Him"]
    page.close()


_DROPZONE = """<body><form><h1>Tech Data Analytics Intern</h1>
<div class="phff-field"><label for="fn">First Name*</label><input id="fn" required></div>
<div class="phff-field"><label for="em">Email*</label><input id="em" type="email" required></div>
<div id="phff-field-cloudWidget" class="phff-resume-group"><p>Upload Resume*</p>
  <div class="dz-clickable"><span>Upload Resume</span> RESUME/CV<p>DOC, DOCX, HTML, PDF or TXT</p></div></div>
</form>
<input type="file" class="dz-hidden-input" style="visibility:hidden;position:absolute;top:0;left:0;height:0;width:0">
</body>"""


def test_a_dropzone_upload_box_is_found_and_takes_the_resume(browser: Any, tmp_path: Path) -> None:
    """Disney's careers site (2026-11-18): Dropzone's real file box sits at the end of the page with no name or label,
    so helper never uploaded the resume."""
    from helper.answers import PlannedAnswer

    page = browser.new_page()
    page.set_content(_DROPZONE)
    upload = next(q for q in generic.discover(page) if q.type == "file")
    assert (upload.label, upload.required) == ("Upload Resume", True)
    pdf = tmp_path / "VatsalPatel_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    results = generic.fill_form(page, [PlannedAnswer(field=upload.field_name, label=upload.label, type="file",
                                                     required=True, kind="auto", value=str(pdf), confidence=1.0,
                                                     source="resume")],
                                pdf, country=None, city=None, pause=lambda: None)
    assert [r.ok for r in results] == [True], [r.detail for r in results]
    assert page.evaluate("() => document.querySelector('input.dz-hidden-input').files[0].name") == pdf.name
    page.close()


def test_a_phenom_thank_you_page_is_a_confirmation(browser: Any) -> None:
    """Disney's careers site (2026-11-18): a finished application lands on the "apply-thankyou" page, which helper
    didn't know, so it kept waiting after Vatsal submitted."""
    page = browser.new_page()
    page.route("https://join.example.com/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<body><h1>You're all set</h1></body>"))
    for path, confirmed in [("global/en/apply-thankyou?jobSeqNo=X1&status=success", True),
                            ("global/en/applythankyou?jobSeqNo=X1", True),
                            ("global/en/hvhapply?jobSeqNo=X1", False),
                            ("global/en/job/10164672/Thank-You-Card-Designer", False)]:
        page.goto(f"https://join.example.com/{path}")
        assert generic.is_confirmation(page) is confirmed, path
    page.close()


def test_a_submit_button_the_site_keeps_turned_off_is_counted_apart(browser: Any) -> None:  # apexanalytix, 2026-11-18
    page = browser.new_page()
    page.set_content('<form><button type="submit" data-testid="Apply" aria-disabled="true">Apply</button></form>')
    assert generic.submit_buttons(page) == [] and generic.disabled_submit_buttons(page) == 1
    page.set_content('<form><button type="submit">Apply</button></form>')
    assert len(generic.submit_buttons(page)) == 1 and generic.disabled_submit_buttons(page) == 0
    page.close()
