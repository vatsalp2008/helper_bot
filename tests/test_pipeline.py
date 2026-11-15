"""`helper resume` then `helper continue`, end to end, with a pasted job description (milestone 4)."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helper.cli import app
from helper.config import Paths, load_config
from helper.convert import run_convert
from helper.errors import EXIT_NEEDS_USER, EXIT_WAITING_FOR_CLAUDE
from helper.latex import find_binary

FIXTURES = Path(__file__).resolve().parent / "fixtures"
runner = CliRunner()

needs_tex = pytest.mark.skipif(find_binary("pdflatex") is None, reason="needs pdflatex")


@pytest.fixture
def ready(repo: Path, sample_profile_text: str) -> Path:
    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    paths = Paths(repo)
    run_convert(paths, load_config(paths))
    return repo


def start(repo: Path) -> str:
    result = runner.invoke(app, ["resume", "--jd-file", str(FIXTURES / "jd_backend_intern.txt"),
                                 "--company", "Northwind Labs", "--role", "Backend SWE Intern"])
    assert result.exit_code == EXIT_WAITING_FOR_CLAUDE, result.output
    (run_id,) = [p.name for p in (repo / "runs").iterdir()]
    assert f"helper continue {run_id}" in result.output
    return run_id


def test_resume_writes_the_handoff_and_waits(ready: Path) -> None:
    run_id = start(ready)
    folder = ready / "runs" / run_id
    assert {p.name for p in folder.iterdir()} == {"job.json", "keywords.json", "eligibility.json", "tailor_input.json",
                                                  "state.json"}
    tailor_input = json.loads((folder / "tailor_input.json").read_text(encoding="utf-8"))
    assert tailor_input["write_to"] == f"runs/{run_id}/tailor_output.json"
    assert tailor_input["run"]["require_open_source"] is True
    assert tailor_input["job"]["company"] == "Northwind Labs"
    assert tailor_input["projects"][0]["id"] == "ecommerce_platform"  # highest keyword overlap first
    status = {t["canonical"]: t["status"] for t in tailor_input["keywords"]["tech"]}
    assert status["Scala"] == "not_in_master" and status["Kafka"] == "in_items"
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    assert state["steps"]["tailor"]["status"] == "waiting_for_claude"


def test_continue_waits_until_the_output_exists_then_reports_problems(ready: Path) -> None:
    run_id = start(ready)
    folder = ready / "runs" / run_id
    early = runner.invoke(app, ["continue", run_id])
    assert early.exit_code == EXIT_WAITING_FOR_CLAUDE
    assert "isn't there yet" in early.output

    bad = json.loads((FIXTURES / "tailor_output_northwind.json").read_text(encoding="utf-8"))
    bad["projects"][0]["bullets"][0] = bad["projects"][0]["bullets"][0].replace("10,000", "12,000")
    bad["summary"] = "Motivated engineer"
    (folder / "tailor_output.json").write_text(json.dumps(bad), encoding="utf-8")
    schema = runner.invoke(app, ["continue", run_id])
    assert schema.exit_code == EXIT_WAITING_FOR_CLAUDE
    assert "[schema] summary" in schema.output

    del bad["summary"]
    (folder / "tailor_output.json").write_text(json.dumps(bad), encoding="utf-8")
    numbers = runner.invoke(app, ["continue", run_id])
    assert numbers.exit_code == EXIT_WAITING_FOR_CLAUDE
    assert "'12,000' is not a number in pulsetrader's master text" in numbers.output
    validation = json.loads((folder / "validation.json").read_text(encoding="utf-8"))
    assert validation["ok"] is False and validation["errors"][0]["check"] == "numbers"


@needs_tex
def test_a_valid_output_becomes_a_one_page_pdf(ready: Path) -> None:
    run_id = start(ready)
    folder = ready / "runs" / run_id
    shutil.copy(FIXTURES / "tailor_output_northwind.json", folder / "tailor_output.json")
    result = runner.invoke(app, ["continue", run_id])
    assert result.exit_code == 0, result.output
    pdf = ready / "resumes" / "AlexExample_Resume_NorthwindLabs_BackendSWEIntern.pdf"
    assert pdf.is_file() and pdf.with_suffix(".tex").is_file()
    assert "Matched verbatim: Java, Go, Kafka" in result.output
    assert "Genuine gaps not forced in: Scala, Cassandra" in result.output
    assert "Track: Backend and distributed; Kalini entry: Software Development Intern" in result.output
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    assert state["steps"]["render"]["status"] == "done"
    assert state["options"]["result"]["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert state["options"]["result"]["pages"] == 1
    assert "—" not in result.output  # rule 4 covers helper's own messages too


def test_an_eligibility_blocker_pauses_for_vatsal_until_he_says_build(ready: Path, tmp_path: Path) -> None:
    jd = tmp_path / "jd.txt"
    jd.write_text("Backend Intern (Summer 2027)\nF-1 and OPT candidates are not eligible for this role.\n",
                  encoding="utf-8")
    result = runner.invoke(app, ["resume", "--jd-file", str(jd), "--company", "Acme", "--role", "Backend Intern"])
    assert result.exit_code == EXIT_NEEDS_USER, result.output
    assert "[blocker] The posting names F-1, OPT or CPT candidates as ineligible." in result.output
    assert "--build-anyway" in result.output
    (run_id,) = [p.name for p in (ready / "runs").iterdir()]
    folder = ready / "runs" / run_id
    assert (folder / "eligibility.json").is_file() and not (folder / "tailor_input.json").exists()

    again = runner.invoke(app, ["continue", run_id])
    assert again.exit_code == EXIT_NEEDS_USER
    build = runner.invoke(app, ["continue", run_id, "--build-anyway"])
    assert build.exit_code == EXIT_WAITING_FOR_CLAUDE, build.output
    assert (folder / "tailor_input.json").is_file()
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    assert state["steps"]["eligibility"] == {**state["steps"]["eligibility"], "status": "done",
                                             "message": "built anyway at Vatsal's request"}


def test_the_graduation_date_comes_from_the_eligibility_check(ready: Path, tmp_path: Path) -> None:
    jd = tmp_path / "jd.txt"
    jd.write_text("Software Engineer Intern (Summer 2027)\nYou must be returning to school after the internship.\n",
                  encoding="utf-8")
    result = runner.invoke(app, ["resume", "--jd-file", str(jd), "--company", "Acme", "--role", "SWE Intern"])
    assert result.exit_code == EXIT_WAITING_FOR_CLAUDE, result.output
    assert "Graduation date: December 2027 (the posting requires returning to school after Summer 2027)" in result.output
    (run_id,) = [p.name for p in (ready / "runs").iterdir()]
    tailor_input = json.loads((ready / "runs" / run_id / "tailor_input.json").read_text(encoding="utf-8"))
    assert tailor_input["run"]["grad_date"] == "December 2027"

    forced = runner.invoke(app, ["resume", "--jd-file", str(jd), "--company", "Acme", "--role", "SWE Intern 2",
                                 "--grad-date", "May 2027"])
    assert forced.exit_code == EXIT_WAITING_FOR_CLAUDE
    assert "set to May 2027 by --grad-date" in forced.output
