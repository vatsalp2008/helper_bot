"""Auto-submit (spec 10.5): off by default; helper submits by itself only when every condition holds."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from helper.adapters.common import FieldResult
from helper.answers import PlannedAnswer
from helper.apply import fill_run
from helper.autosubmit import Gate, blockers, clean_review_runs, readiness, record_fill_run, run_problems
from helper.config import Paths, load_config
from helper.models import Job, RunState
from helper.runs import load_state, save_state, set_step, write_json
from helper.tracker import repo as tracker_repo
from helper.tracker.commands import open_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def answer(field: str, **kw: Any) -> PlannedAnswer:
    base: dict[str, Any] = dict(field=field, label=field.replace("_", " ").title(), type="text", required=True,
                                kind="auto", value="x", confidence=1.0, source="profile")
    base.update(kw)
    return PlannedAnswer(**base)


def gate(**kw: Any) -> Gate:
    answers = [answer("first_name"), answer("resume", type="file", kind="file", value=None)]
    base: dict[str, Any] = dict(ats="greenhouse", clean_runs=5, min_clean_runs=5, answers=answers,
                                results=[FieldResult(a.field, a.label, True, "x") for a in answers])
    base.update(kw)
    return Gate(**base)


def test_every_condition_holds() -> None:
    assert blockers(gate()) == []


@pytest.mark.parametrize("change,reason", [
    ({"clean_runs": 3}, "greenhouse has 3 clean review-mode run(s) so far; auto-submit needs 5"),
    ({"ats": "icims"}, "iCIMS runs always stop before Submit for you"),
    ({"ats": "unknown"}, "unknown runs stop before Submit for you"),
    ({"answers": [answer("why", status="needs_review")]}, "'Why' still needs your review"),
    ({"answers": [answer("why", kind="confirm")]}, "'Why' wasn't answered from your profile or an approved answer"),
    ({"answers": [answer("gpa", kind="unknown", value=None)]}, "wasn't answered from your profile"),
    ({"results": [FieldResult("first_name", "First Name", False, None, "boom")]}, "'First Name' wasn't filled"),
    ({"eligibility": {"flags": [{"level": "warning", "message": "It won't sponsor visas."}]}},
     "eligibility flags: It won't sponsor visas."),
    ({"eligibility": {"duplicates": [{"kind": "possible"}]}}, "the duplicate check found a matching application"),
    ({"captcha_seen": True}, "a CAPTCHA or verification step appeared"),
    ({"page_errors": ["Phone is invalid"]}, "the form shows errors: Phone is invalid"),
    ({"has_submit_button": False}, "couldn't find exactly one Submit button"),
])
def test_each_condition_blocks_on_its_own(change: dict[str, Any], reason: str) -> None:
    found = blockers(gate(**change))
    assert any(reason in r for r in found), found


def test_notes_and_optional_questions_dont_block() -> None:
    g = gate(eligibility={"flags": [{"level": "note", "message": "Graduation date: May 2027 (default)."}]},
             answers=[answer("first_name"), answer("pronouns", required=False, kind="skip", value=None)])
    assert blockers(g) == []


def test_clean_runs_are_counted_per_site(repo: Path) -> None:
    paths = Paths(repo)
    conn = open_db(paths, load_config(paths))
    try:
        assert record_fill_run(conn, "r1", "greenhouse", "review", "applied", [])
        assert not record_fill_run(conn, "r2", "greenhouse", "review", "applied", ["not filled: Phone"])
        assert not record_fill_run(conn, "r3", "greenhouse", "review", "filled", [])  # never submitted
        assert record_fill_run(conn, "r4", "greenhouse", "auto", "applied", [])  # auto runs don't add to the count
        assert record_fill_run(conn, "r5", "lever", "review", "applied", [])
        assert (clean_review_runs(conn, "greenhouse"), clean_review_runs(conn, "lever"),
                clean_review_runs(conn, "ashby")) == (1, 1, 0)
        assert readiness(conn, 5, {"workday": 0, "generic": 0}) == (
            "Auto-submit readiness (clean review-mode runs): greenhouse 1/5, lever 1/5, ashby 0/5, workday ready "
            "(no count needed), generic ready (no count needed); iCIMS and unfamiliar sites: review only")
    finally:
        conn.close()
    assert run_problems([FieldResult("a", "Phone", False, None)], True, ["Answer these yourself"]) == [
        "not filled: Phone", "a CAPTCHA or verification step appeared", "Answer these yourself"]


# --------------------------------------------------------------------------- the whole fill, on the stand-in form


def start(repo: Path, clean_runs: int) -> tuple[Paths, str, int]:
    import importlib.util

    spec = importlib.util.spec_from_file_location("stand_in", Path(__file__).with_name("test_fill.py"))
    stand_in = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(stand_in)  # type: ignore[union-attr]
    MINI_QUESTIONS, SEATTLE, mini_plan = stand_in.MINI_QUESTIONS, stand_in.SEATTLE, stand_in.mini_plan

    (repo / "data" / "profile.yaml").write_text(SEATTLE, encoding="utf-8")
    import re as _re

    config = repo / "config.yaml"  # the spec's threshold, whatever the local config says
    config.write_text(_re.sub(r"(?m)^auto_submit_min_clean_runs:.*$", "auto_submit_min_clean_runs: 5",
                              config.read_text(encoding="utf-8")), encoding="utf-8")
    paths = Paths(repo)
    cfg = load_config(paths)
    run_id = "20261115-100000-acme-swe-intern"
    folder = paths.runs / run_id
    folder.mkdir(parents=True)
    pdf = paths.resumes / run_id / "AlexExample_Resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 test")
    job = Job(url="https://job-boards.greenhouse.io/acme/jobs/1", ats="greenhouse", company="Acme",
              role_title="Software Engineer Intern", description_text="x", source="greenhouse_api", fetched_at="now",
              questions=MINI_QUESTIONS, apply_url=f"file://{(FIXTURES / 'greenhouse_mini_form.html').resolve()}")
    write_json(folder / "job.json", job.model_dump(mode="json"))
    write_json(folder / "answers_plan.json", mini_plan().model_dump(mode="json"))
    write_json(folder / "eligibility.json", {"flags": [{"level": "note", "message": "Graduation date: May 2027."}],
                                             "duplicates": []})
    conn = open_db(paths, cfg)
    app_id = tracker_repo.insert(conn, company="Acme", role_title="Software Engineer Intern", job_url=job.url,
                                 run_id=run_id, resume_path=f"resumes/{run_id}/AlexExample_Resume.pdf")
    for n in range(clean_runs):
        record_fill_run(conn, f"earlier-{n}", "greenhouse", "review", "applied", [])
    conn.commit()
    conn.close()
    state = RunState(run_id=run_id, created_at="now", command="apply", url=job.url, application_id=app_id,
                     options={"run": {"projects_per_resume": 5, "bullets_per_item": 2, "grad_date": "May 2027"},
                              "result": {"pdf": f"resumes/{run_id}/AlexExample_Resume.pdf"}, "submit_mode": "auto"})
    set_step(state, "answers", "done")
    save_state(paths, state)
    return paths, run_id, app_id


def test_auto_mode_submits_once_when_every_condition_holds(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths, run_id, app_id = start(repo, clean_runs=5)
    outcome = fill_run(paths, load_config(paths), run_id, headless=True, wait_minutes=0.5, poll_seconds=0.3)
    printed = capsys.readouterr().out
    assert outcome.status == "applied", outcome.message
    assert "Clicking Submit once" in printed and "helper clicked Submit once" in printed
    conn = open_db(paths, load_config(paths))
    try:
        assert tracker_repo.get(conn, app_id)["status"] == "applied"
        rows = conn.execute("SELECT mode, outcome, clean FROM fill_runs WHERE run_id = ?", (run_id,)).fetchall()
        assert [tuple(r) for r in rows] == [("auto", "applied", 1)]
    finally:
        conn.close()
    saved = json.loads((paths.runs / run_id / "state.json").read_text(encoding="utf-8"))
    assert saved["options"]["helper_clicked_submit"]  # kept, so no refill of this run clicks Submit (rule 10)


def test_a_refill_never_clicks_submit_again(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Formlabs (2026-11-20): helper clicked Submit and Greenhouse held it for an emailed code; a refill with every
    # condition still holding would have clicked Submit a second time (rule 10)
    paths, run_id, app_id = start(repo, clean_runs=5)
    state = load_state(paths, run_id)
    state.options["helper_clicked_submit"] = "2026-11-19T22:41:00"
    save_state(paths, state)
    outcome = fill_run(paths, load_config(paths), run_id, headless=True, wait_minutes=0.05, poll_seconds=0.2)
    printed = capsys.readouterr().out
    assert "never clicks it again (rule 10)" in printed and "Clicking Submit once" not in printed
    assert outcome.status == "filled"  # filled, and nobody clicked Submit


def test_auto_mode_falls_back_to_review_and_says_why(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    paths, run_id, app_id = start(repo, clean_runs=2)
    outcome = fill_run(paths, load_config(paths), run_id, headless=True, wait_minutes=0.05, poll_seconds=0.2)
    printed = capsys.readouterr().out
    assert outcome.status == "filled"  # nobody clicked Submit: helper didn't, and the wait ran out
    assert "Auto-submit is off for this run" in printed
    assert "greenhouse has 2 clean review-mode run(s) so far; auto-submit needs 5" in printed
    assert "click Submit yourself" in printed
    conn = open_db(paths, load_config(paths))
    try:
        assert tracker_repo.get(conn, app_id)["status"] == "filled"
        row = conn.execute("SELECT mode, clean FROM fill_runs WHERE run_id = ?", (run_id,)).fetchone()
        assert tuple(row) == ("review", 0)
    finally:
        conn.close()
    saved = json.loads((paths.runs / run_id / "state.json").read_text(encoding="utf-8"))
    assert saved["options"]["submit_mode"] == "auto"


def test_workday_can_submit_with_best_guesses_but_not_with_gaps() -> None:  # Vatsal, 2026-11-16
    guess = answer("disability", kind="confirm", value="No, I do not have a disability")
    ok = gate(ats="workday", clean_runs=0, min_clean_runs=0, best_guesses_ok=True,
              answers=[answer("first_name"), guess], results=[FieldResult("first_name", "First Name", True, "x"),
                                                              FieldResult("disability", "Disability", True, "x")])
    assert blockers(ok) == []
    assert blockers(gate(ats="workday", clean_runs=0, min_clean_runs=0, answers=[guess],
                         results=[FieldResult("disability", "Disability", True, "x")]))  # Greenhouse-style: blocks


def test_company_sites_named_by_their_system_follow_the_company_site_rule() -> None:  # Laborie, Fortinet, 2026-11-16
    from helper.autosubmit import site_kind
    assert site_kind("jobvite") == "generic" and site_kind("rippling") == "generic" and site_kind("generic") == "generic"
    assert site_kind("workday") == "workday" and site_kind("icims") == "icims" and site_kind("taleo") == "taleo"
    assert site_kind("oracle") == "oracle"  # its own adapter since 2026-11-19


def test_a_page_without_its_form_is_reloaded_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # TRI, 2026-11-17: Lever answered with "Something went wrong on our end. Please try reloading your browser."
    from helper import apply as ap

    calls: list[str] = []

    class Page:
        def goto(self, url: str, wait_until: str = "") -> None:
            calls.append("goto")

        def reload(self, wait_until: str = "") -> None:
            calls.append("reload")

        def wait_for_load_state(self, *a: object, **k: object) -> None:
            pass

    class Adapter:
        tries = 0

        def find_form(self, page: object) -> object:
            Adapter.tries += 1
            return "form" if Adapter.tries == 2 else None

    monkeypatch.setattr(ap, "_wait_out_captcha", lambda page, wait, poll: False)
    seen = {"captcha": False}
    assert ap._open_form(Page(), Adapter(), "https://jobs.lever.co/tri/x/apply", 1, 0.1, seen) == "form"
    assert calls == ["goto", "reload"]
    Adapter.tries = -5  # never shows: one reload, then give up
    calls.clear()
    assert ap._open_form(Page(), Adapter(), "https://jobs.lever.co/tri/x/apply", 1, 0.1, seen) is None
    assert calls == ["goto", "reload"]


def test_a_submit_the_site_rejects_is_not_sent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # TRI, 2026-11-17: Lever answered Submit with "There was an error verifying your application. Please try again."
    from helper import apply as ap

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

        def is_closed(self) -> bool:
            return False

        def wait_for_timeout(self, ms: float) -> None:
            pass

    class Adapter:
        def is_confirmation(self, page: object, form: object = None) -> bool:
            return False

    monkeypatch.setattr(ap, "captcha_visible", lambda page: False)
    monkeypatch.setattr(ap, "asks_for_emailed_code", lambda page, form=None: False)
    clock = iter(range(0, 10_000, 2))
    monkeypatch.setattr(ap.time, "monotonic", lambda: next(clock))
    rejected = Page("Automated Driving Intern\n✱ There was an error verifying your application. Please try again.")
    assert ap._wait_for_submit(rejected, Adapter(), {"value": True}, 5, 0.01) == "rejected"
    assert ap._submit_rejected(Page("Thanks for applying! We received your application.")) is None
    # Benchling's Ashby, 2026-11-23: the spam check refuses the submit outright
    ashby = Page("We couldn't submit your application\nYour application submission was flagged as possible spam.")
    assert ap._submit_rejected(ashby)
    assert ap._submit_rejected(Page("Submission flagged as potential spam")) is not None


def test_page_messages_are_shortened_at_a_word() -> None:
    from helper.apply import _clip

    text = "We couldn't submit your application Your application submission was flagged as possible spam. " * 3
    short = _clip(text)
    assert len(short) <= 163 and short.endswith("...") and not short.endswith(" p...")
    assert short.rsplit(" ", 1)[-1].rstrip(".") in text.split()
    assert _clip("Please make a selection.") == "Please make a selection."


_REFUSING_FORM = """<form>
  <label for="s">State *</label>
  <select required id="s"><option value="">-Select-</option><option>Washington</option></select>
  <button type="button" id="go">Submit Application</button>
</form>
<script>
document.getElementById('go').onclick = () => {
  const s = document.getElementById('s');
  if (!s.value) {
    s.setAttribute('aria-invalid', 'true');
    const note = document.createElement('div');
    note.className = 'fab-FormNote--error';
    note.innerText = 'Please make a selection.';
    s.parentElement.appendChild(note);
  }
};
</script>"""


def test_a_submit_the_form_refuses_is_not_logged_as_sent(monkeypatch: Any) -> None:
    """All Options' BambooHR (2026-11-18): Submit was clicked, the form showed "Please make a selection." under a
    State box that had reset, and helper logged it as submitted_unverified (and put it in Notion). A form still
    showing its Submit button with new field errors sent nothing: say so and keep watching."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    import helper.apply as ap
    from helper.adapters import generic

    said: list[str] = []
    monkeypatch.setattr(ap, "_say", said.append)
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium isn't installed: {exc}")
        page = browser.new_page()
        page.set_content(_REFUSING_FORM)
        page.evaluate(generic.SUBMIT_WATCH_JS)
        before = ap.visible_errors(page.main_frame)
        assert before == []
        page.click("#go")  # helper's own click comes before the wait, so the wait gets the errors from before it
        outcome = ap._wait_for_submit(page, generic, {"value": False}, 0.12, 0.25, page.main_frame,
                                      errors_before=before)
        browser.close()
    assert outcome == "timeout"  # never "unverified": nothing was sent, and helper doesn't click Submit again
    assert any("didn't take the submit; nothing was sent" in s and "Please make a selection." in s for s in said)
    assert not any(".." in s for s in said)  # a message ending in "." gets no second one (Serko, 2026-11-24)


_ASHBY_REFUSAL = """<form>
  <div class="_box_1x2y" id="box" style="display:none"><p>Your form needs corrections</p>
    <ul><li>Missing entry for required field: A U.S. person is a citizen ...</li></ul></div>
  <button type="button" class="ashby-application-form-submit-button">Submit Application</button>
</form>
<script>
document.querySelector('button').onclick = () => { document.getElementById('box').style.display = 'block'; };
</script>"""


def test_an_ashby_form_that_needs_corrections_is_not_logged_as_sent(monkeypatch: Any) -> None:
    """Snowflake (2026-11-19): after Vatsal's Submit, Ashby's box said "Your form needs corrections. Missing entry for
    required field: ...", and helper logged submitted_unverified and added a Notion row. Its POST went out, but the
    form is still there with its Submit button and that box: nothing was sent."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    import helper.apply as ap
    from helper.adapters import ashby

    said: list[str] = []
    monkeypatch.setattr(ap, "_say", said.append)
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium isn't installed: {exc}")
        page = browser.new_page()
        page.set_content(_ASHBY_REFUSAL)
        before = ap.visible_errors(page.main_frame)
        assert before == []
        page.click("button")
        outcome = ap._wait_for_submit(page, ashby, {"value": True}, 0.12, 0.25, page.main_frame, errors_before=before)
        browser.close()
    assert outcome == "timeout"  # never "unverified"
    assert any("didn't take the submit; nothing was sent" in s and "Your form needs corrections" in s for s in said)


def test_a_half_filled_school_entry_blocks_auto_submit() -> None:
    # Lucid (2026-11-19): the second school's Discipline found no match, the entry's boxes are required once it is
    # there, and auto-submit clicked anyway, so the form stopped the submit
    answers = [answer("first_name")]
    results = [FieldResult("first_name", "First Name", True, "x"),
               FieldResult("education-1", "Education 2: Bachelor of Engineering, University of Mumbai", False,
                           "Oct. 2021 to May 2025", "discipline: no option matches 'Computer Engineering'")]
    reasons = blockers(gate(answers=answers, results=results))
    assert any("Education 2: Bachelor of Engineering" in r and "wasn't filled completely" in r for r in reasons)


_STEP_FORM = """<form id="f"><h3>Page 1 of 2 - Basic Information</h3>
  <label for="e">Email *</label><input id="e" name="email" required>
  <button type="button" id="next">NEXT</button>
</form>
<script>
const page2 = '<h3>Page 2 of 2</h3><label for="c">City *</label><input id="c" name="city" required>'
  + '<button type="submit">Submit</button>';
document.getElementById('next').onclick = () => {
  if (location.hash === '#popup') {
    const w = window.open('', '_blank');
    w.document.write('<form>' + page2 + '</form>');
  } else {
    document.getElementById('f').innerHTML = page2;
  }
};
</script>"""


@pytest.mark.parametrize("where", ["same", "popup"])
def test_a_company_form_s_next_pages_are_followed_and_filled(monkeypatch: Any, tmp_path: Path, where: str) -> None:
    """Veeva (2026-11-23): "Page 1 of 3" with NEXT. helper filled page 1, never followed Next, and the next page opened
    in a window it didn't watch. Now it clicks Next, follows the page (or the new window) and fills the next page."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    import helper.apply as ap
    from helper.adapters import generic
    from helper.answers import AnswerPlan, PlannedAnswer

    def planned(questions: list[Any], ctx: Any) -> Any:
        values = {"Email": "alex@example.com", "City": "Seattle"}
        return AnswerPlan(answers=[PlannedAnswer(field=q.field_name, label=q.label, type=q.type, required=q.required,
                                                 kind="auto", value=values.get(q.label.rstrip(" *"), "x"))
                                   for q in questions])

    said: list[str] = []
    monkeypatch.setattr(ap, "_say", said.append)
    monkeypatch.setattr(ap, "_context", lambda *a, **k: SimpleNamespace(approved=None))
    monkeypatch.setattr(ap, "_approved", lambda paths: None)
    monkeypatch.setattr(ap, "plan_answers", planned)
    monkeypatch.setattr(ap, "_ask_claude_midfill", lambda *a, **k: None)
    monkeypatch.setattr(ap, "run_dir", lambda paths, run_id: tmp_path)
    cfg = SimpleNamespace(drafts_need_review=False, human_pause_seconds=[0, 0])
    monkeypatch.setattr(ap, "make_pause", lambda cfg: (lambda: None))
    profile = SimpleNamespace(tier1=SimpleNamespace(address=SimpleNamespace(country=None, city=None)))
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Chromium isn't installed: {exc}")
        context = browser.new_context()
        page = context.new_page()
        page.goto("about:blank#" + where)
        page.set_content(_STEP_FORM)
        page.evaluate(f"() => {{ location.hash = '{where}' }}")
        form = page.main_frame
        plan = planned(generic.discover(form), None)
        results = generic.fill_form(form, plan.answers, tmp_path / "r.pdf", country=None, city=None,
                                    pause=lambda: None)
        assert generic.next_button(form) is not None and not generic.submit_buttons(form)
        state = SimpleNamespace(run_id="r1")
        page2, form2, results = ap._more_pages(SimpleNamespace(), cfg, state, SimpleNamespace(), context, page, form,
                                               generic, plan, results, tmp_path / "r.pdf", profile, 0.05, 0.1)
        city = form2.locator("input[name=city]").input_value()
        finals = len(generic.submit_buttons(form2))
        browser.close()
    assert city == "Seattle" and finals == 1, said
    assert (page2 is not page) == (where == "popup")
    assert [a.label.rstrip(" *") for a in plan.answers] == ["Email", "City"]
    assert any("Next page: filling 1 question(s)" in s for s in said), said


def test_a_captcha_after_submit_holds_the_unverified_clock(monkeypatch: Any) -> None:  # Veeva, 2026-11-23
    # helper clicked Submit, a CAPTCHA appeared, Vatsal solved it after a few minutes and the thank-you page showed;
    # helper had given up after 60 seconds and logged submitted_unverified
    from helper import apply as ap

    calls = {"captcha": 0, "confirm": 0}

    def captcha(page: object) -> bool:
        calls["captcha"] += 1
        return calls["captcha"] <= 40  # about 200 simulated seconds of CAPTCHA

    class Page:
        def is_closed(self) -> bool:
            return False

        def wait_for_timeout(self, ms: float) -> None:
            pass

        def locator(self, selector: str) -> Any:
            return SimpleNamespace(inner_text=lambda timeout=0: "")

    class Adapter:
        def is_confirmation(self, page: object, form: object = None) -> bool:
            calls["confirm"] += 1
            return calls["captcha"] > 41  # the thank-you page right after the CAPTCHA

    monkeypatch.setattr(ap, "captcha_visible", captcha)
    monkeypatch.setattr(ap, "asks_for_emailed_code", lambda page, form=None: False)
    monkeypatch.setattr(ap, "_say", lambda text: None)
    clock = iter(range(0, 100_000, 5))
    monkeypatch.setattr(ap.time, "monotonic", lambda: next(clock))
    assert ap._wait_for_submit(Page(), Adapter(), {"value": True}, 60, 0.01) == "confirmed"


def test_the_request_counted_as_a_submit_is_logged(tmp_path: Path) -> None:  # Illumio, 2026-11-23
    from helper import apply as ap
    from helper.adapters import ashby

    seen = {"value": False}
    watch = ap._submit_watcher(ashby, seen, tmp_path)
    watch(SimpleNamespace(url="https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams", method="POST"))
    assert not seen["value"] and not (tmp_path / "submit_requests.log").exists()
    url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction"
    watch(SimpleNamespace(url=url, method="POST"))
    assert seen["value"] and url in (tmp_path / "submit_requests.log").read_text(encoding="utf-8")
