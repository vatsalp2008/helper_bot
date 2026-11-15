"""`helper list`, `show`, `stats`, `export`, `mark`, `sync-notion` (spec 11)."""

from __future__ import annotations

import csv
from pathlib import Path

from typer.testing import CliRunner

from helper.cli import app
from helper.config import Paths, load_config
from helper.errors import EXIT_ERROR
from helper.tracker import repo
from helper.tracker.commands import open_db

runner = CliRunner()


def seed(root: Path) -> None:
    paths = Paths(root)
    conn = open_db(paths, load_config(paths))
    try:
        repo.insert(conn, company="Acme", role_title="SWE Intern", status="applied", date_applied="2026-11-10",
                    job_url="https://boards.greenhouse.io/acme/jobs/1")
        repo.insert(conn, company="Beta", role_title="Data Intern", status="drafted", run_id="20261114-run")
        conn.commit()
    finally:
        conn.close()


def test_list_show_stats_and_export(repo: Path) -> None:  # noqa: F811 (the `repo` fixture is the temp project)
    assert "No applications yet." in runner.invoke(app, ["list"]).output
    seed(repo)
    listed = runner.invoke(app, ["list"])
    assert listed.exit_code == 0 and "Acme" in listed.output and "greenhouse" in listed.output
    assert "Beta" not in runner.invoke(app, ["list", "--status", "applied"]).output
    shown = runner.invoke(app, ["show", "2"])
    assert "#2 Beta: Data Intern" in shown.output and "run folder: runs/20261114-run" in shown.output
    assert runner.invoke(app, ["show", "9"]).exit_code == EXIT_ERROR
    assert "2 tracked, 1 sent" in runner.invoke(app, ["stats"]).output
    out = repo / "exports" / "apps.csv"
    exported = runner.invoke(app, ["export", "--csv", "--out", str(out)])
    assert "Wrote 2 application(s)" in exported.output
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["company"] for r in rows] == ["Acme", "Beta"]


def test_mark_records_the_status_and_says_what_happened_in_notion(repo: Path) -> None:  # noqa: F811
    seed(repo)
    result = runner.invoke(app, ["mark", "2", "applied", "--date", "2026-11-14", "--note", "applied on the site"])
    assert result.exit_code == 0, result.output
    assert "#2 Beta, Data Intern: drafted -> applied (applied 2026-11-14)" in result.output
    assert "Notion: not set up, so this row stays notion_pending." in result.output
    skipped = runner.invoke(app, ["mark", "1", "skipped"])
    assert "Notion: not sent" not in skipped.output or skipped.exit_code == 0
    assert runner.invoke(app, ["mark", "1", "hired"]).exit_code == EXIT_ERROR
    assert "--date must look like" in runner.invoke(app, ["mark", "1", "applied", "--date", "11/14"]).output


def test_sync_notion_needs_the_env(repo: Path) -> None:  # noqa: F811
    result = runner.invoke(app, ["sync-notion"])
    assert result.exit_code == EXIT_ERROR and "Notion isn't set up" in result.output
