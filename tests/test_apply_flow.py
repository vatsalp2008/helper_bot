"""`helper apply` from the link to approved form answers, through the CLI (the Greenhouse API is the saved fixture)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from helper.adapters import greenhouse
from helper.cli import app
from helper.config import Paths, load_config
from helper.convert import run_convert
from helper.errors import EXIT_NEEDS_USER, EXIT_WAITING_FOR_CLAUDE
from helper.latex import find_binary

FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://job-boards.greenhouse.io/figma/jobs/6178851004"
runner = CliRunner()


@pytest.fixture
def ready(repo: Path, sample_profile_text: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    config = repo / "config.yaml"  # spec 10.4: drafts wait for Vatsal's OK (the in_form setting is tested below)
    config.write_text(re.sub(r"(?m)^answer_review:.*$", "answer_review: before_fill", config.read_text(encoding="utf-8")),
                      encoding="utf-8")
    run_convert(Paths(repo), load_config(Paths(repo)))
    data = json.loads((FIXTURES / "greenhouse_figma_job.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(greenhouse, "fetch_job", lambda url, session=None: greenhouse.job_from_api(data, url, "figma"))
    return repo


def answer_everything(needed: dict[str, Any]) -> dict[str, Any]:
    out = []
    for q in needed["questions"]:
        if q["options"]:
            out.append({"field": q["field"], "answer": q["suggested"] or q["options"][-1]})
        else:
            out.append({"field": q["field"], "answer": "I use Figma every day and want to build its data platform."})
    return {"answers": out}


def test_dry_run_and_auto_mode(ready: Path) -> None:
    preview = runner.invoke(app, ["apply", URL, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert "Figma: Data Engineer Intern (2027)" in preview.output and "Form answers (dry run" in preview.output
    assert not (ready / "runs").exists() or not any((ready / "runs").iterdir())
    auto = runner.invoke(app, ["apply", URL, "--mode", "auto"])
    assert auto.exit_code == EXIT_WAITING_FOR_CLAUDE and "Submit mode: auto" in auto.output
    (run_id,) = [p.name for p in (ready / "runs").iterdir()]
    state = json.loads((ready / "runs" / run_id / "state.json").read_text(encoding="utf-8"))
    assert state["options"]["submit_mode"] == "auto"


@pytest.mark.skipif(find_binary("pdflatex") is None, reason="needs pdflatex")
def test_apply_until_the_answers_are_approved(ready: Path) -> None:
    started = runner.invoke(app, ["apply", URL])
    assert started.exit_code == EXIT_WAITING_FOR_CLAUDE, started.output
    (run_id,) = [p.name for p in (ready / "runs").iterdir()]
    folder = ready / "runs" / run_id
    shutil.copy(FIXTURES / "tailor_output_northwind.json", folder / "tailor_output.json")

    built = runner.invoke(app, ["continue", run_id])
    assert built.exit_code == EXIT_WAITING_FOR_CLAUDE, built.output
    assert f"Resume: resumes/{run_id}/AlexExample_Resume.pdf" in built.output
    assert f"read runs/{run_id}/cover_letter_input.json" in built.output
    shutil.copy(FIXTURES / "cover_letter_northwind.json", folder / "cover_letter_output.json")
    lettered = runner.invoke(app, ["continue", run_id])
    assert lettered.exit_code == EXIT_WAITING_FOR_CLAUDE, lettered.output
    assert f"Cover letter: resumes/{run_id}/AlexExample_CoverLetter.pdf" in lettered.output
    assert f"read runs/{run_id}/questions_needed.json and write runs/{run_id}/answers_provided.json" in lettered.output
    needed = json.loads((folder / "questions_needed.json").read_text(encoding="utf-8"))
    labels = {q["label"] for q in needed["questions"]}
    assert "Why do you want to join Figma?" in labels

    bad = answer_everything(needed)
    bad["answers"][0]["answer"] = bad["answers"][0]["answer"] + " — truly"
    (folder / "answers_provided.json").write_text(json.dumps(bad), encoding="utf-8")
    rejected = runner.invoke(app, ["continue", run_id])
    assert rejected.exit_code == EXIT_WAITING_FOR_CLAUDE and "no em dashes" in rejected.output

    everything = answer_everything(needed)  # one required question only Vatsal can answer is left out at first
    own = next(q for q in needed["questions"] if q["required"] and q["options"])
    first = {"answers": [a for a in everything["answers"] if a["field"] != own["field"]]}
    (folder / "answers_provided.json").write_text(json.dumps(first), encoding="utf-8")
    review = runner.invoke(app, ["continue", run_id])
    assert review.exit_code == EXIT_NEEDS_USER, review.output
    assert "review the form answers" in review.output and "--approve-answers" in review.output
    assert "I use Figma every day" in review.output
    assert "only you can answer" in review.output and own["label"] in review.output.split("only you can answer")[1]

    (folder / "answers_provided.json").write_text(json.dumps(everything), encoding="utf-8")  # his answer, added
    review = runner.invoke(app, ["continue", run_id])
    assert review.exit_code == EXIT_NEEDS_USER and "only you can answer" not in review.output, review.output

    approved = runner.invoke(app, ["continue", run_id, "--approve-answers"])
    assert approved.exit_code == 0, approved.output
    assert f"helper fill {run_id}" in approved.output
    plan = json.loads((folder / "answers_plan.json").read_text(encoding="utf-8"))
    assert all(a["status"] != "needs_review" for a in plan["answers"])
    dry = runner.invoke(app, ["fill", run_id, "--dry-run"])
    assert dry.exit_code == 0 and "Would fill https://job-boards.greenhouse.io/figma/jobs/6178851004" in dry.output



@pytest.mark.skipif(find_binary("pdflatex") is None, reason="needs pdflatex")
def test_with_in_form_review_drafts_go_straight_into_the_form(ready: Path) -> None:  # Vatsal, 2026-11-15
    config = ready / "config.yaml"
    text = re.sub(r"(?m)^answer_review:.*$", "answer_review: in_form", config.read_text(encoding="utf-8"))
    config.write_text(re.sub(r"(?m)^drafts_need_review:.*$", "drafts_need_review: true", text), encoding="utf-8")
    assert runner.invoke(app, ["apply", URL]).exit_code == EXIT_WAITING_FOR_CLAUDE
    (run_id,) = [p.name for p in (ready / "runs").iterdir()]
    folder = ready / "runs" / run_id
    shutil.copy(FIXTURES / "tailor_output_northwind.json", folder / "tailor_output.json")
    runner.invoke(app, ["continue", run_id])
    shutil.copy(FIXTURES / "cover_letter_northwind.json", folder / "cover_letter_output.json")
    runner.invoke(app, ["continue", run_id])
    needed = json.loads((folder / "questions_needed.json").read_text(encoding="utf-8"))
    everything = answer_everything(needed)
    own = next(q for q in needed["questions"] if q["required"] and q["options"])
    first = {"answers": [a for a in everything["answers"] if a["field"] != own["field"]]}
    (folder / "answers_provided.json").write_text(json.dumps(first), encoding="utf-8")
    ready_to_fill = runner.invoke(app, ["continue", run_id])
    assert ready_to_fill.exit_code == 0, ready_to_fill.output  # no approval pause
    assert "go into the form for you to check before Submit" in ready_to_fill.output
    assert "I use Figma every day" in ready_to_fill.output
    assert "Only you can answer these" in ready_to_fill.output and own["label"] in ready_to_fill.output
    plan = json.loads((folder / "answers_plan.json").read_text(encoding="utf-8"))
    drafted = [a for a in plan["answers"] if a["source"] == "Claude draft"]
    assert drafted and all(a["status"] == "needs_review" for a in drafted)  # auto-submit still won't send them


@pytest.mark.skipif(find_binary("pdflatex") is None, reason="needs pdflatex")
def test_drafts_can_count_as_approved_for_auto_submit(ready: Path) -> None:  # Vatsal, 2026-11-15
    config = ready / "config.yaml"
    text = re.sub(r"(?m)^answer_review:.*$", "answer_review: in_form", config.read_text(encoding="utf-8"))
    config.write_text(re.sub(r"(?m)^drafts_need_review:.*$", "drafts_need_review: false", text), encoding="utf-8")
    assert runner.invoke(app, ["apply", URL]).exit_code == EXIT_WAITING_FOR_CLAUDE
    (run_id,) = [p.name for p in (ready / "runs").iterdir()]
    folder = ready / "runs" / run_id
    shutil.copy(FIXTURES / "tailor_output_northwind.json", folder / "tailor_output.json")
    runner.invoke(app, ["continue", run_id])
    shutil.copy(FIXTURES / "cover_letter_northwind.json", folder / "cover_letter_output.json")
    runner.invoke(app, ["continue", run_id])
    needed = json.loads((folder / "questions_needed.json").read_text(encoding="utf-8"))
    (folder / "answers_provided.json").write_text(json.dumps(answer_everything(needed)), encoding="utf-8")
    assert runner.invoke(app, ["continue", run_id]).exit_code == 0
    plan = json.loads((folder / "answers_plan.json").read_text(encoding="utf-8"))
    drafted = [a for a in plan["answers"] if a["source"].startswith("Claude")]
    assert drafted and all(a["status"] == "approved" for a in drafted)
