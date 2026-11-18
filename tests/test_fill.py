"""Filling Greenhouse forms in a real (headless) browser: the saved Figma form and an interactive stand-in."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from helper.adapters.greenhouse import css_id, dom_id, fill_form, is_confirmation, job_from_api
from helper.answers import AnswerContext, plan_answers
from helper.apply import fill_run
from helper.config import Paths, load_config
from helper.models import Config, FormQuestion, Job, Profile, RunState
from helper.runs import save_state, set_step, write_json
from helper.tracker import repo as tracker_repo
from helper.tracker.commands import open_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIGMA_JSON = json.loads((FIXTURES / "greenhouse_figma_job.json").read_text(encoding="utf-8"))
MINI = (FIXTURES / "greenhouse_mini_form.html").resolve()
SAMPLE = (FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")
# The stand-in form's location search knows Seattle; the fictional profile gets a Seattle address for these tests.
SEATTLE = SAMPLE.replace("city: Springfield", "city: Seattle").replace("state: ST", "state: WA")
PROFILE = Profile.model_validate(yaml.safe_load(SEATTLE))

playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def browser() -> Iterator[Any]:  # per test: fill_run starts its own Playwright, which can't run inside another
    with playwright_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True)
        except Exception as exc:  # Chromium not downloaded
            pytest.skip(f"Chromium isn't installed: {exc}")
        yield b
        b.close()


MINI_QUESTIONS = [
    FormQuestion(label="First Name", field_name="first_name", required=True),
    FormQuestion(label="Last Name", field_name="last_name", required=True),
    FormQuestion(label="Email", field_name="email", required=True),
    FormQuestion(label="Phone", field_name="phone", required=True),
    FormQuestion(label="Location", field_name="location", required=True, section="location"),
    FormQuestion(label="Resume/CV", field_name="resume", type="file", required=True),
    FormQuestion(label="LinkedIn Profile", field_name="question_1"),
    FormQuestion(label="Are you authorized to work in the country for which you applied?", field_name="question_2",
                 type="select", options=["Yes", "No"], required=True),
    FormQuestion(label="If you are currently enrolled in university, what is your expected graduation date?",
                 field_name="question_3", type="select", options=["Spring 2027", "Fall 2027", "Spring 2028", "Other"],
                 required=True),
    FormQuestion(label="Why do you want to join Acme?", field_name="question_4", type="textarea", required=True),
    FormQuestion(label="Gender", field_name="gender", type="select", options=["Decline To Self Identify", "Female", "Male"],
                 section="eeoc"),
    FormQuestion(label="Are you Hispanic/Latino?", field_name="hispanic_ethnicity", type="select",
                 options=["Yes", "No", "Decline To Self Identify"], section="eeoc"),
]


def mini_plan() -> Any:
    job = Job(company="Acme", role_title="Software Engineer Intern", description_text="x", source="greenhouse_api",
              fetched_at="now", questions=MINI_QUESTIONS)
    ctx = AnswerContext(profile=PROFILE, cfg=Config(), job=job, grad_date="May 2027", employment="internship",
                        season="summer", season_year=2027)
    plan = plan_answers(MINI_QUESTIONS, ctx)
    why = plan.get("question_4")
    assert why is not None
    why.kind, why.value, why.status = "auto", "Acme builds the tools I use every day.", "approved"
    return plan


def test_every_planned_field_exists_on_the_real_figma_form(browser: Any) -> None:
    page = browser.new_page(java_script_enabled=False)
    page.goto(f"file://{(FIXTURES / 'greenhouse_figma_form.html').resolve()}")
    job = job_from_api(FIGMA_JSON, "https://job-boards.greenhouse.io/figma/jobs/6178851004", "figma")
    ctx = AnswerContext(profile=PROFILE, cfg=Config(), job=job, grad_date="May 2027")
    for a in plan_answers(job.questions, ctx).answers:
        if a.section == "eeoc" and a.field == "race":
            continue  # shown only after the Hispanic/Latino answer
        target = page.locator(css_id(dom_id(a.field)))
        assert target.count() == 1, a.field
        if a.type == "file":
            assert target.get_attribute("type") == "file"
        elif a.type in ("select", "multi_select") or a.field == "location":
            assert target.get_attribute("role") == "combobox", a.field
    assert page.locator("#country").get_attribute("role") == "combobox"  # the phone's country, filled too
    page.close()


def test_filling_the_form_never_submits_it(browser: Any, tmp_path: Path) -> None:
    pdf = tmp_path / "AlexExample_Resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    page = browser.new_page()
    page.goto(f"file://{MINI}")
    results = fill_form(page, mini_plan().answers, pdf, country="United States", city="Seattle", pause=lambda: None)
    failed = [(r.label, r.detail) for r in results if not r.ok]
    assert not failed, failed
    assert page.input_value("#first_name") == PROFILE.tier1.first_name
    assert page.input_value("#question_4") == "Acme builds the tools I use every day."
    selected = lambda i: page.get_attribute(f"#{i}", "data-selected")  # noqa: E731
    assert (selected("question_2"), selected("question_3"), selected("country")) == ("Yes", "Spring 2027",
                                                                                     "United States +1")
    assert selected("candidate-location") == "Seattle, Washington, United States"
    assert selected("hispanic_ethnicity") == "Decline To Self Identify"
    assert page.evaluate("() => document.getElementById('resume').files[0].name") == pdf.name
    assert page.evaluate("() => window.submitted === undefined")  # helper never clicks Submit
    assert not is_confirmation(page)
    page.close()


@pytest.mark.parametrize("page_file", ["greenhouse_mini_form.html#autosubmit", "greenhouse_company_page.html",
                                       "greenhouse_mini_form.html#autosubmit-code"],
                         ids=["greenhouse page", "form embedded in a company page", "emailed code after Submit"])
def test_fill_run_waits_for_the_submit_and_logs_it(repo: Path, page_file: str) -> None:
    (repo / "data" / "profile.yaml").write_text(SEATTLE, encoding="utf-8")
    paths = Paths(repo)
    cfg = load_config(paths)
    run_id = "20261114-200000-acme-swe-intern"
    folder = paths.runs / run_id
    folder.mkdir(parents=True)
    pdf = paths.resumes / run_id / "AlexExample_Resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 test")
    job = Job(url="https://job-boards.greenhouse.io/acme/jobs/1", ats="greenhouse", company="Acme",
              role_title="Software Engineer Intern", description_text="x", source="greenhouse_api", fetched_at="now",
              questions=MINI_QUESTIONS, apply_url=f"file://{FIXTURES.resolve()}/{page_file}")
    write_json(folder / "job.json", job.model_dump(mode="json"))
    write_json(folder / "answers_plan.json", mini_plan().model_dump(mode="json"))
    conn = open_db(paths, cfg)
    app_id = tracker_repo.insert(conn, company="Acme", role_title="Software Engineer Intern", job_url=job.url, run_id=run_id,
                         resume_path=f"resumes/{run_id}/AlexExample_Resume.pdf")
    conn.commit()
    conn.close()
    state = RunState(run_id=run_id, created_at="now", command="apply", url=job.url, application_id=app_id,
                     options={"run": {"projects_per_resume": 5, "bullets_per_item": 2, "grad_date": "May 2027"},
                              "result": {"pdf": f"resumes/{run_id}/AlexExample_Resume.pdf"}})
    set_step(state, "answers", "done")
    save_state(paths, state)

    outcome = fill_run(paths, cfg, run_id, headless=True, wait_minutes=0.5, poll_seconds=0.3)
    assert outcome.status == "applied", outcome.message
    assert "Notion: not set up" in outcome.message
    conn = open_db(paths, cfg)
    clean = conn.execute("SELECT clean FROM fill_runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    conn.close()
    assert clean == 1  # an emailed code at Submit doesn't make the run unclean (Vatsal, 2026-11-16)
    conn = open_db(paths, cfg)
    row = tracker_repo.get(conn, app_id)
    conn.close()
    assert (row["status"], row["confirmation_screenshot"]) == ("applied", f"runs/{run_id}/confirmation.png")
    assert [h for h in ("filled.png", "confirmation.png", "fill_log.json") if not (folder / h).exists()] == []
    assert (paths.exports / "applications.csv").is_file()
    saved = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    assert saved["steps"]["submit"]["message"] == "applied"


def test_dry_run_opens_no_browser(repo: Path) -> None:
    paths = Paths(repo)
    run_id = "20261114-200001-acme"
    folder = paths.runs / run_id
    folder.mkdir(parents=True)
    write_json(folder / "job.json", Job(url="https://job-boards.greenhouse.io/acme/jobs/1", company="Acme",
                                        role_title="SWE Intern", description_text="x", source="greenhouse_api",
                                        fetched_at="now", apply_url="https://job-boards.greenhouse.io/acme/jobs/1")
               .model_dump(mode="json"))
    write_json(folder / "answers_plan.json", mini_plan().model_dump(mode="json"))
    state = RunState(run_id=run_id, created_at="now", command="apply", options={"result": {"pdf": "x.pdf"}})
    set_step(state, "answers", "done")
    save_state(paths, state)
    outcome = fill_run(paths, load_config(paths), run_id, dry_run=True)
    assert outcome.status == "dry_run" and "Would fill https://job-boards.greenhouse.io/acme/jobs/1" in outcome.message


def test_greenhouse_demographic_questions_use_their_bare_number_as_the_id() -> None:  # Soros, 2026-11-17
    from helper.adapters.greenhouse import css_id, dom_id

    assert dom_id("demographic_4000319003") == "4000319003"  # <input id="4000319003" role="combobox">
    assert css_id(dom_id("demographic_4000319003")) == "[id='4000319003']"  # "#4000319003" isn't valid CSS
    assert css_id(dom_id("question_123")) == "#question_123"
    assert dom_id("location") == "candidate-location"


def test_replan_uses_todays_rules_and_keeps_claudes_answers(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scout, 2026-11-18: a refill reused the first fill's plan, so a rule fixed since ("highest level completed" is
    the bachelor's) never reached the form."""
    import helper.apply as ap
    from helper.answers import AnswerPlan, PlannedAnswer
    from helper.runs import read_json

    paths = Paths(repo)
    cfg = load_config(paths)
    run_id = "20261118-000000-acme"
    folder = paths.runs / run_id
    folder.mkdir(parents=True)
    questions = [FormQuestion(label="Highest level completed", field_name="q1", type="select",
                              options=["Bachelor's", "Master's"]),
                 FormQuestion(label="Any conflicts of interest?", field_name="q2", type="textarea")]
    write_json(folder / "job.json", Job(url="https://job-boards.greenhouse.io/acme/jobs/1", company="Acme",
                                        role_title="Associate", description_text="x", source="greenhouse_api",
                                        fetched_at="now", questions=questions).model_dump(mode="json"))

    def answer(field: str, label: str, value: Any, source: str, kind: str = "auto", status: str = "ready") -> Any:
        return PlannedAnswer(field=field, label=label, type="select" if field == "q1" else "textarea", required=True,
                             kind=kind, value=value, confidence=1.0, source=source, status=status)

    old = AnswerPlan(answers=[answer("q1", "Highest level completed", "Master's", "profile degree"),
                              answer("q2", "Any conflicts of interest?", "No conflicts.", "Claude draft",
                                     status="approved")])
    write_json(folder / "answers_plan.json", old.model_dump(mode="json"))
    new = AnswerPlan(answers=[answer("q1", "Highest level completed", "Bachelor's", "profile education"),
                              answer("q2", "Any conflicts of interest?", None, "", kind="free_text")])
    monkeypatch.setattr(ap, "_context", lambda *args: None)
    monkeypatch.setattr(ap, "plan_answers", lambda qs, ctx: new)
    changes = ap.replan(paths, cfg, run_id)
    saved = AnswerPlan.model_validate(read_json(folder / "answers_plan.json"))
    assert [(a.value, a.status) for a in saved.answers] == [("Bachelor's", "ready"), ("No conflicts.", "approved")]
    assert changes == ["Highest level completed: \"Master's\" -> \"Bachelor's\""]


def test_rescan_reads_a_company_form_again_and_hands_new_questions_to_claude(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """apexanalytix on Rippling, 2026-11-18: the first read missed the form's dropdowns and rating questions, so a
    refill needed the questions read again, not only the answers planned again."""
    import helper.apply as ap
    from helper.adapters import generic
    from helper.answers import AnswerPlan, PlannedAnswer
    from helper.errors import HelperError, WaitingForClaude
    from helper.runs import read_json

    paths = Paths(repo)
    cfg = load_config(paths)
    run_id = "20261118-000000-rippling"
    folder = paths.runs / run_id
    folder.mkdir(parents=True)
    link = "https://ats.rippling.com/acme/jobs/1/apply"
    write_json(folder / "job.json", Job(url="https://ats.rippling.com/acme/jobs/1", apply_url=link, company="Acme",
                                        role_title="Intern", description_text="x", source="page", fetched_at="now",
                                        questions=[FormQuestion(label="Search", field_name="generic:x", type="text",
                                                                required=True)]).model_dump(mode="json"))
    write_json(folder / "answers_plan.json", AnswerPlan(answers=[PlannedAnswer(
        field="generic:x", label="Search", type="text", required=True, kind="unknown", value=None, confidence=0.0,
        source="")]).model_dump(mode="json"))
    state = RunState(run_id=run_id, created_at="now", command="apply", url=link)
    set_step(state, "answers", "done")
    save_state(paths, state)
    new = [FormQuestion(label="What interests you about working at Acme?", field_name="generic:why",
                        type="textarea", required=True)]
    planned = AnswerPlan(answers=[PlannedAnswer(field="generic:why", label=new[0].label, type="textarea",
                                                required=True, kind="free_text", value=None, confidence=0.0,
                                                source="")])
    monkeypatch.setattr(ap, "_context", lambda *args: None)
    monkeypatch.setattr(ap, "plan_answers", lambda qs, ctx: planned)
    monkeypatch.setattr(ap, "_needed", lambda *args: {"questions": [new[0].label]})
    seen: list[str] = []
    form = "https://ats.rippling.com/acme/jobs/1/apply?step=form"  # where the scan found the form (Disney, 2026-11-18)
    with pytest.raises(WaitingForClaude):
        ap.rescan(paths, cfg, run_id, scan=lambda url: seen.append(url) or generic.Scan(questions=new, apply_url=form))
    assert seen == [link]
    saved = Job.model_validate(read_json(folder / "job.json"))
    assert [q.label for q in saved.questions] == [new[0].label] and saved.apply_url == form
    assert (folder / "answers_plan.before-rescan.json").is_file() and (folder / "questions_needed.json").is_file()
    # a form the page no longer shows leaves everything as it was
    with pytest.raises(HelperError, match="couldn't read the form's questions again"):
        ap.rescan(paths, cfg, run_id, scan=lambda url: generic.Scan())
    # forms from a known system are read from its own data, not rescanned
    job = Job.model_validate(read_json(folder / "job.json"))
    job.url = job.apply_url = "https://job-boards.greenhouse.io/acme/jobs/1"
    write_json(folder / "job.json", job.model_dump(mode="json"))
    with pytest.raises(HelperError, match="--rescan is for company-site forms"):
        ap.rescan(paths, cfg, run_id, scan=lambda url: generic.Scan(questions=new))
