"""Auto-submit (spec 10.5): off by default; helper submits by itself only when every condition holds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from helper.adapters.common import FieldResult
from helper.answers import PlannedAnswer
from helper.apply import fill_run
from helper.autosubmit import Gate, blockers, clean_review_runs, readiness, record_fill_run, run_problems
from helper.config import Paths, load_config
from helper.models import Job, RunState
from helper.runs import save_state, set_step, write_json
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
    ({"ats": "unknown"}, "unfamiliar forms always stop before Submit"),
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
        assert readiness(conn, 5, {"workday": 0}) == (
            "Auto-submit readiness (clean review-mode runs): greenhouse 1/5, lever 1/5, ashby 0/5, workday ready "
            "(no count needed); iCIMS and unfamiliar sites: review only")
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
