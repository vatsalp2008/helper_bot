from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from helper import __version__
from helper.cli import app
from helper.errors import EXIT_ERROR, EXIT_NOT_BUILT

runner = CliRunner()

SPEC_COMMANDS = ["setup", "convert", "apply", "resume", "fill", "continue",
                 "list", "show", "stats", "export", "mark", "sync-notion"]


def test_help_lists_every_spec_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in SPEC_COMMANDS:
        assert command in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"helper {__version__}"


@pytest.mark.parametrize(
    "args,milestone",
    [
        (["convert"], 2),
        (["continue", "run-1"], 4),
        (["list"], 6),
        (["show", "1"], 6),
        (["stats"], 6),
        (["export", "--csv"], 6),
        (["mark", "1", "interview"], 6),
        (["sync-notion"], 6),
        (["apply", "https://boards.greenhouse.io/acme/jobs/1"], 7),
        (["fill", "run-1"], 7),
    ],
)
def test_unbuilt_commands_exit_5_and_name_their_milestone(repo: Path, args: list[str], milestone: int) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == EXIT_NOT_BUILT
    assert f"milestone {milestone}" in result.output


def test_resume_accepts_a_jd_file(repo: Path, tmp_path: Path) -> None:
    jd = tmp_path / "jd.txt"
    jd.write_text("We are hiring.", encoding="utf-8")
    result = runner.invoke(app, ["resume", "--jd-file", str(jd)])
    assert result.exit_code == EXIT_NOT_BUILT
    assert "milestone 4" in result.output


def test_apply_rejects_an_unknown_mode(repo: Path) -> None:
    result = runner.invoke(app, ["apply", "https://example.com/job", "--mode", "yolo"])
    assert result.exit_code == 2


def test_setup_command_fails_until_the_profile_header_is_filled(repo: Path) -> None:
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == EXIT_ERROR
    assert "profile.yaml" in result.output


def test_setup_command_passes_with_a_complete_profile(repo: Path, sample_profile_text: str) -> None:
    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0, result.output
    assert "0 problem(s)" in result.output
