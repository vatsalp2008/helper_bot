"""Greenhouse intake (spec 9 step 2) and which sites helper takes on (spec 10.2, rules 7 and 8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from helper.adapters import adapter_for
from helper.adapters import greenhouse as gh
from helper.errors import HelperError, NeedsUser

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIGMA = json.loads((FIXTURES / "greenhouse_figma_job.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "url,board,job_id",
    [
        ("https://job-boards.greenhouse.io/figma/jobs/6178851004", "figma", "6178851004"),
        ("https://boards.greenhouse.io/figma/jobs/6178851004?gh_jid=6178851004", "figma", "6178851004"),
        ("https://boards.greenhouse.io/embed/job_app?for=abacus&token=7654321", "abacus", "7654321"),
        ("https://careers.airbnb.com/positions/8380007?gh_jid=8380007", None, "8380007"),
    ],
)
def test_links_are_parsed(url: str, board: Optional[str], job_id: str) -> None:
    ref = gh.parse_ref(url)
    assert ref is not None and (ref.board, ref.job_id) == (board, job_id)


def test_non_greenhouse_links() -> None:
    assert gh.parse_ref("https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b") is None


def test_job_from_the_api() -> None:
    job = gh.job_from_api(FIGMA, "https://boards.greenhouse.io/figma/jobs/6178851004", "figma")
    assert (job.company, job.role_title, job.ats, job.job_id) == ("Figma", "Data Engineer Intern (2027)", "greenhouse",
                                                                  "6178851004")
    assert job.apply_url == "https://job-boards.greenhouse.io/figma/jobs/6178851004"
    assert "What you'll do\n\n- Build data pipelines with Python and SQL\n- Work with Spark and Airflow" in job.description_text
    by_field = {q.field_name: q for q in job.questions}
    assert by_field["first_name"].required and by_field["first_name"].type == "text"
    assert by_field["resume"].type == "file"
    assert by_field["question_19835368004"].type == "textarea"
    assert by_field["question_19835369004"].options == ["Yes", "No"]
    assert by_field["location"].section == "location"
    assert {"latitude", "longitude"}.isdisjoint(by_field)  # hidden fields are not questions
    eeoc = [q.field_name for q in job.questions if q.section == "eeoc"]
    assert eeoc.index("hispanic_ethnicity") == eeoc.index("race") - 1  # asked first on the form


class FakeResponse:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code, self._body = status, body

    def json(self) -> Any:
        return self._body


class FakeSession:
    def __init__(self, boards: dict[str, Any]) -> None:
        self.boards, self.asked = boards, []

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
        self.asked.append(url)
        board = url.split("/boards/")[1].split("/")[0]
        return FakeResponse(200, self.boards[board]) if board in self.boards else FakeResponse(404, {})


def test_company_career_links_find_their_board() -> None:
    session = FakeSession({"airbnb": {**FIGMA, "company_name": "Airbnb"}})
    job = gh.fetch_job("https://careers.airbnb.com/positions/6178851004?gh_jid=6178851004", session)
    assert job.company == "Airbnb" and job.apply_url.startswith("https://job-boards.greenhouse.io/airbnb/jobs/")


def test_a_closed_posting_says_so() -> None:
    with pytest.raises(HelperError, match="no open job 1"):
        gh.fetch_job("https://job-boards.greenhouse.io/acme/jobs/1", FakeSession({}))


def test_which_sites_helper_takes_on() -> None:
    assert adapter_for("https://job-boards.greenhouse.io/figma/jobs/1") is gh
    with pytest.raises(NeedsUser, match="never automates LinkedIn Easy Apply"):
        adapter_for("https://www.linkedin.com/jobs/view/123")
    with pytest.raises(NeedsUser, match="Handshake"):
        adapter_for("https://app.joinhandshake.com/jobs/123")
    with pytest.raises(NeedsUser, match="never creates"):
        adapter_for("https://acme.taleo.net/careersection/2/jobdetail.ftl?job=1234")
    from helper.adapters import workday

    assert adapter_for("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Seattle/X_R1") is workday
    with pytest.raises(NeedsUser, match="isn't a job posting"):
        adapter_for("https://acme.wd5.myworkdayjobs.com/en-US/Careers")
    from helper.adapters import generic

    assert adapter_for("https://careers.example.com/jobs/123") is generic  # milestone 10: the best-effort adapter


def test_tracking_parameters_are_dropped() -> None:
    from helper.ats import clean_url

    url = "https://www.nuro.ai/careersitem?gh_jid=8360415&fbclid=PAdGRleAVkcVpl&utm_source=x#top"
    assert clean_url(url) == "https://www.nuro.ai/careersitem?gh_jid=8360415"
    assert clean_url("https://job-boards.greenhouse.io/embed/job_app?for=lyft&token=8796124002&jr_id=6aa46ddd1d92e2d0") \
        == "https://job-boards.greenhouse.io/embed/job_app?for=lyft&token=8796124002"
    assert gh.parse_ref(clean_url(url)) == gh.GreenhouseRef(None, "8360415")


def test_a_company_page_names_its_own_board() -> None:
    page = ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=current81"></script>'
            "<script>fetch(`https://boards-api.greenhouse.io/v1/boards/current81/jobs/${id}`)</script>"
            '<a href="https://job-boards.greenhouse.io/embed/job_app?for=current81&amp;token=1">x</a>')
    assert gh.boards_on_page(page) == ["current81"]
    assert gh.boards_on_page('<a href="https://job-boards.greenhouse.io/acme/jobs/12">Apply</a>') == ["acme"]

    class Page(FakeResponse):
        text = page

    class Session(FakeSession):
        def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
            if "/boards/" not in url:  # the company's own careers page
                self.asked.append(url)
                return Page(200)
            return super().get(url, params, timeout)

    session = Session({"current81": {**FIGMA, "company_name": "Current"}})
    job = gh.fetch_job("https://current.com/careers/open-positions/?id=6178851004&gh_jid=6178851004", session)
    assert job.company == "Current" and job.apply_url == "https://job-boards.greenhouse.io/current81/jobs/6178851004"
    assert [u.split("/boards/")[-1] for u in session.asked] == [
        "current/jobs/6178851004", "https://current.com/careers/open-positions/?id=6178851004&gh_jid=6178851004",
        "current81/jobs/6178851004"]


# --------------------------------------------------------------------------- the Employment section (a live form)


playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def browser() -> Any:
    with playwright_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium isn't installed: {exc}")
        yield b
        b.close()


def test_the_jobs_on_the_resume_fill_the_employment_section(browser: Any) -> None:
    from helper.adapters.workday_page import WorkEntry
    from helper.dates import MonthYear

    work = [WorkEntry(title="Teaching Assistant", company="Example University", location="Seattle, WA",
                      start=MonthYear(2026, 1), end=MonthYear(2026, 5), current=False, description=""),
            WorkEntry(title="Data Intern", company="Mockco", location=None, start=MonthYear(2024, 12), end=None,
                      current=True, description="")]
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'greenhouse_employment.html').resolve()}")
    ok, chosen = gh.choose_option(page, "country", "United States", search="United St")  # ignores the first try
    assert (ok, chosen) == (True, "United States +1")
    results = gh.fill_employment(page, work, lambda: None)
    assert [(r.ok, r.value) for r in results] == [(True, "Jan. 2026 to May 2026"), (True, "Dec. 2024 to present")]
    filled = page.evaluate("""() => [0, 1].map(i => [
        document.getElementById('company-name-' + i).value, document.getElementById('title-' + i).value,
        document.getElementById('start-date-month-' + i).dataset.selected || '',
        document.getElementById('start-date-year-' + i).value,
        document.getElementById('end-date-month-' + i).dataset.selected || '',
        document.getElementById('end-date-year-' + i).value,
        document.getElementById('current-role-' + i + '_1').checked])""")
    assert filled == [["Example University", "Teaching Assistant", "January", "2026", "May", "2026", False],
                      ["Mockco", "Data Intern", "December", "2024", "", "", True]]
    assert page.evaluate("() => window.schoolsAdded") == 0  # Education's "Add another" was left alone
    assert page.evaluate("() => window.submitted") is False
    assert gh.fill_employment(page, [], lambda: None) == []
    page.close()


def test_a_hidden_follow_up_question_is_not_a_failure(browser: Any, tmp_path: Path) -> None:  # SharkNinja
    from helper.answers import PlannedAnswer

    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    page = browser.new_page()
    page.goto(f"file://{(FIXTURES / 'greenhouse_employment.html').resolve()}")
    follow_up = PlannedAnswer(field="question_999", label="If you selected other, please specify which one", type="text",
                              required=True, kind="auto", value="Northeastern University", confidence=1.0, source="x")
    missing = PlannedAnswer(field="question_998", label="Why do you want to work here?", type="textarea", required=True,
                            kind="auto", value="Because.", confidence=1.0, source="x")
    results = {r.field: r for r in gh.fill_form(page, [follow_up, missing], pdf, country=None, city=None,
                                                 pause=lambda: None)}
    assert results["question_999"].ok and results["question_999"].detail == "not shown on this form"
    assert not results["question_998"].ok  # a required question that's really missing still counts
    page.close()


_CHECKBOX_FORM = """<form id="application-form">
<fieldset class="checkbox" id="question_701[]" aria-required="true">
  <legend>Please acknowledge the privacy policy. *</legend>
  <div class="checkbox__wrapper"><div class="checkbox__input">
    <input required type="checkbox" id="question_701[]_9001" name="question_701[]" value="9001"
           style="opacity:0;position:absolute"><svg width="16" height="16"></svg></div>
    <label for="question_701[]_9001">Acknowledge/Confirm</label></div>
</fieldset>
<fieldset class="checkbox" id="question_702[]">
  <legend>Do any of these apply to you? Select all that apply.</legend>
  <div class="checkbox__wrapper"><input type="checkbox" id="question_702[]_1" name="question_702[]" value="1">
    <label for="question_702[]_1">Resident of a sanctioned region</label></div>
  <div class="checkbox__wrapper"><input type="checkbox" id="question_702[]_2" name="question_702[]" value="2">
    <label for="question_702[]_2">TikTok</label></div>
  <div class="checkbox__wrapper"><input type="checkbox" id="question_702[]_3" name="question_702[]" value="3">
    <label for="question_702[]_3">YouTube</label></div>
  <div class="checkbox__wrapper"><input type="checkbox" id="question_702[]_4" name="question_702[]" value="4">
    <label for="question_702[]_4">None of the above</label></div>
</fieldset>
</form>"""


def test_select_all_that_apply_questions_tick_their_boxes(browser: Any, tmp_path: Path) -> None:
    """Greenhouse's new forms show multi-select questions as a fieldset of checkboxes, not a dropdown; typing into
    the fieldset failed (Databricks, Twitch, Cloudflare, 2026-11-18)."""
    from helper.answers import PlannedAnswer

    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    page = browser.new_page()
    page.set_content(_CHECKBOX_FORM)

    def multi(field: str, value: str) -> PlannedAnswer:
        return PlannedAnswer(field=field, label=field, type="multi_select", required=True, kind="auto", value=value,
                             confidence=1.0, source="x")

    results = gh.fill_form(page, [multi("question_701[]", "Acknowledge/Confirm"),
                                  multi("question_702[]", "TikTok; YouTube")], pdf,
                           country=None, city=None, pause=lambda: None)
    assert [(r.ok, r.value) for r in results] == [(True, "Acknowledge/Confirm"), (True, "TikTok; YouTube")]
    ticked = page.evaluate("() => [...document.querySelectorAll('input:checked')].map(i => i.id)")
    assert ticked == ["question_701[]_9001", "question_702[]_2", "question_702[]_3"]
    again = gh.fill_form(page, [multi("question_702[]", "TikTok")], pdf, country=None, city=None, pause=lambda: None)
    assert again[0].ok and page.locator("[id='question_702[]_2']").is_checked()  # ticking twice doesn't untick it
    wrong = gh.fill_form(page, [multi("question_702[]", "Instagram")], pdf, country=None, city=None, pause=lambda: None)
    assert not wrong[0].ok and "no option matches 'Instagram'" in wrong[0].detail
    page.close()


def test_an_embedded_form_opens_on_its_own_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # Akuna, 2026-11-18: the job-boards link forwarded to akunacapital.com, where the embedded form sat under a cookie
    # banner and every dropdown click timed out; the window now opens the embedded form's own page
    embed = "https://job-boards.greenhouse.io/embed/job_app?for=akunacapital&token=8018853"

    class Frame:
        def __init__(self, url: str) -> None:
            self.url = url

    class Page:
        def __init__(self) -> None:
            self.main_frame = Frame("https://akunacapital.com/careers/job/8018853/?gh_jid=8018853")
            self.visits: list[str] = []

        def goto(self, url: str, wait_until: str = "") -> None:
            self.visits.append(url)
            self.main_frame = Frame(url)

    page = Page()
    finds = iter([Frame(embed), None])
    monkeypatch.setattr(gh.common, "find_form", lambda p, *a, **k: next(finds) or p.main_frame)
    form = gh.find_form(page)
    assert page.visits == [embed] and form is page.main_frame and form.url == embed
    # a form on Greenhouse's own page is filled where it is
    own = Page()
    own.main_frame = Frame("https://job-boards.greenhouse.io/acme/jobs/1")
    monkeypatch.setattr(gh.common, "find_form", lambda p, *a, **k: p.main_frame)
    assert gh.find_form(own) is own.main_frame and own.visits == []
