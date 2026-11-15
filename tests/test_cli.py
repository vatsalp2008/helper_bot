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
        (["apply", "https://careers.example.com/jobs/123"], 10),
    ],
)
def test_unbuilt_commands_exit_5_and_name_their_milestone(repo: Path, args: list[str], milestone: int) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == EXIT_NOT_BUILT
    assert f"milestone {milestone}" in result.output


def test_resume_needs_a_readable_link_or_a_jd_file(repo: Path, tmp_path: Path) -> None:
    other = runner.invoke(app, ["resume", "https://careers.example.com/jobs/123"])
    assert other.exit_code == EXIT_NOT_BUILT and "--jd-file" in other.output
    assert runner.invoke(app, ["resume"]).exit_code == EXIT_ERROR
    jd = tmp_path / "jd.txt"
    jd.write_text("We are hiring.", encoding="utf-8")
    missing_role = runner.invoke(app, ["resume", "--jd-file", str(jd), "--company", "Acme"])
    assert missing_role.exit_code == EXIT_ERROR
    assert "--company and --role" in missing_role.output


def test_continue_needs_an_existing_run(repo: Path) -> None:
    result = runner.invoke(app, ["continue", "20260101-000000-nothing"])
    assert result.exit_code == EXIT_ERROR
    assert "no run" in result.output


def test_apply_rejects_an_unknown_mode(repo: Path) -> None:
    result = runner.invoke(app, ["apply", "https://example.com/job", "--mode", "yolo"])
    assert result.exit_code == 2


def test_convert_writes_master_yaml_and_prints_a_summary(repo: Path) -> None:
    result = runner.invoke(app, ["convert"])
    assert result.exit_code == 0, result.output
    assert (repo / "data" / "master.yaml").is_file()
    assert "Projects: 26 parsed, the file says 26" in result.output
    assert "Experience: 6 parsed, the file says 6" in result.output
    assert "Could not parse: nothing" in result.output


def test_convert_show_prints_one_item(repo: Path) -> None:
    result = runner.invoke(app, ["convert", "--show", "chef"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("id: chef\n")
    missing = runner.invoke(app, ["convert", "--show", "nope"])
    assert missing.exit_code == EXIT_ERROR
    assert "No item 'nope'" in missing.output


def test_setup_command_fails_until_the_profile_header_is_filled(repo: Path) -> None:
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == EXIT_ERROR
    assert "profile.yaml" in result.output


def test_setup_command_passes_with_a_complete_profile(repo: Path, sample_profile_text: str) -> None:
    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0, result.output
    assert "0 problem(s)" in result.output
