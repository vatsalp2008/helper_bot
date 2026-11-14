from __future__ import annotations

from pathlib import Path

import pytest

from helper.config import Paths, load_config
from helper.convert import run_convert
from helper.latex import find_binary
from helper.setup_env import Check, format_report, run_setup, unset_paths


def _setup(repo: Path, **kwargs: object) -> list[Check]:
    return run_setup(Paths(repo), check_latex=False, check_browser=False, **kwargs)  # type: ignore[arg-type]


def _last(checks: list[Check], name: str) -> Check:
    return [c for c in checks if c.name == name][-1]


def test_setup_creates_folders_database_and_profile_from_example(repo: Path) -> None:
    checks = _setup(repo)
    for folder in ("runs", "resumes", "exports"):
        assert (repo / folder).is_dir()
    assert (repo.parent / "browser-profile").is_dir()
    assert (repo / "helper.db").is_file()
    # profile.yaml was missing, so setup copied the example; its empty header is an error until filled
    assert (repo / "data" / "profile.yaml").is_file()
    assert _last(checks, "profile.yaml").status == "error"
    for name in ("config.yaml", "SQLite", "master list", "courses.yaml", "answers.yaml",
                 "project_sets.yaml", "caps.yaml", "resume_meta.yaml"):
        assert _last(checks, name).status == "ok", name
    assert _last(checks, "master.yaml").status == "warn"  # built in milestone 2
    assert _last(checks, "Notion").status == "warn"  # connected in milestone 6


def test_setup_is_idempotent(repo: Path, sample_profile_text: str) -> None:
    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    _setup(repo)
    checks = _setup(repo)
    assert _last(checks, "folders").detail == "all present"
    assert _last(checks, "SQLite").detail.startswith("ready")
    assert not [c for c in checks if c.status == "error"]


def test_setup_lists_unset_profile_values(repo: Path, sample_profile_text: str) -> None:
    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    check = _last(_setup(repo), "profile.yaml")
    assert check.status == "warn"
    assert "tier1.preferred_name" in check.detail
    assert "fall_internship" not in check.detail


def test_interactive_setup_fills_values_and_keeps_comments(repo: Path, sample_profile_text: str) -> None:
    path = repo / "data" / "profile.yaml"
    path.write_text(sample_profile_text, encoding="utf-8")
    asked: list[str] = []

    def fake_prompt(question: str) -> str:
        asked.append(question)
        return "Lex" if question.startswith("tier1.preferred_name") else ""

    _setup(repo, interactive=True, prompt=fake_prompt)
    text = path.read_text(encoding="utf-8")
    assert 'preferred_name: "Lex"' in text
    assert text.startswith("# Fictional profile")
    assert any(q.startswith("tier1.how_did_you_hear") for q in asked)
    assert not any(q.startswith("tier2.security_clearance") for q in asked)  # disabled item


def test_unset_paths_skips_disabled_items_notes_and_unit() -> None:
    raw = {
        "tier1": {"a": None, "address": {"unit": None, "street": None}},
        "tier2": {
            "x": {"auto_answer": False, "value": None},
            "y": {"auto_answer": True, "value": None, "note": None},
        },
        "education": [{"school": None}],
    }
    assert unset_paths(raw) == ["tier1.a", "tier1.address.street", "tier2.y.value"]


def test_setup_rejects_gpa_keys_in_data_files(repo: Path) -> None:
    (repo / "data" / "courses.yaml").write_text("courses: [A, B, C, D, E]\ngpa: 4.0\n", encoding="utf-8")
    check = _last(_setup(repo), "courses.yaml")
    assert check.status == "error"
    assert "GPA" in check.detail


def test_setup_flags_unknown_project_ids(repo: Path) -> None:
    path = repo / "data" / "project_sets.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("[pulsetrader, ecommerce_platform,", "[pulsetrader, made_up,", 1),
                    encoding="utf-8")
    check = _last(_setup(repo), "project ids")
    assert check.status == "error"
    assert "made_up" in check.detail


def test_setup_reports_a_stale_master_yaml(repo: Path) -> None:
    paths = Paths(repo)
    run_convert(paths, load_config(paths))
    assert _last(_setup(repo), "master.yaml").status == "ok"
    master_md = repo / "data" / "master_list_v23.md"
    master_md.write_text(master_md.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    check = _last(_setup(repo), "master.yaml")
    assert check.status == "warn"
    assert "changed since the last `helper convert`" in check.detail


@pytest.mark.skipif(find_binary("pdflatex") is None, reason="pdflatex not installed")
def test_latex_check_compiles_fontawesome(repo: Path) -> None:
    check = _last(run_setup(Paths(repo), check_browser=False), "LaTeX")
    assert check.status == "ok", check.detail


def test_report_counts_problems_and_warnings() -> None:
    report = format_report([Check("a", "ok", "fine"), Check("bb", "warn", "hmm"), Check("c", "error", "bad")])
    assert "[FAIL] c" in report
    assert report.endswith("1 problem(s), 1 warning(s).")
