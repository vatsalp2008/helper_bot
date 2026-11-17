"""`helper assist`: Vatsal applies, helper prepares (2026-11-16). Nothing opens or gets typed in these tests."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from helper.assist import assist, paste_rows
from helper.config import Paths
from helper.models import Profile


def test_paste_rows_hold_his_details_the_run_date_and_the_runs_answers(sample_profile_text: str) -> None:
    profile = Profile.model_validate(yaml.safe_load(sample_profile_text))
    answers = [{"label": "Are you a current Acme employee?", "value": "No", "type": "radio"},
               {"label": "Resume", "value": None, "type": "file"},
               {"label": "Expected graduation", "value": "Dec 2027", "type": "select"}]
    rows = dict(paste_rows(profile, "December 2027", answers))
    assert rows["Expected graduation"] == "December 2027"  # the run's own date, not repeated from the answers
    assert rows["Are you a current Acme employee?"] == "No" and "Resume" not in rows
    assert "CPT/OPT" in rows.get("Need sponsorship now or later?", "CPT/OPT")
    assert all(v.strip() for v in rows.values())


def test_assist_writes_the_sheet_and_opens_nothing_with_no_open(repo: Path, sample_profile_text: str,
                                                                monkeypatch) -> None:  # type: ignore[no-untyped-def]
    paths = Paths(repo)
    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    run = "20261116-000000-acme-intern"
    folder = paths.runs / run
    folder.mkdir(parents=True)
    pdf = repo / "resumes" / run / "VatsalPatel_Resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")
    (folder / "state.json").write_text(json.dumps({
        "run_id": run, "created_at": "now", "command": "resume", "application_id": 7,
        "options": {"run": {"grad_date": "December 2027"}, "result": {"pdf": f"resumes/{run}/VatsalPatel_Resume.pdf"}}}))
    (folder / "job.json").write_text(json.dumps({
        "url": "https://jobs.smartrecruiters.com/Acme/744000000000001", "company": "Acme", "role_title": "Intern",
        "description_text": "x", "source": "test", "fetched_at": "now"}))
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    out = assist(paths, run, open_browser=False)
    assert not opened and "helper mark 7 applied" in out and "December 2027" in out
    assert (folder / "paste_sheet.txt").is_file()
