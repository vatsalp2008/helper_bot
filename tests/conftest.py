from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway copy of config, data and templates, with HELPER_ROOT pointing at it.

    The real data/profile.yaml is personal and never copied; tests use fixtures instead.
    """
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    shutil.copytree(REPO / "templates", root / "templates")
    for path in (REPO / "data").iterdir():
        if path.is_file() and path.name not in ("profile.yaml", "master.yaml"):
            shutil.copy(path, root / "data" / path.name)
    config = (REPO / "config.yaml").read_text(encoding="utf-8")
    config = config.replace("~/.helper/browser-profile", str(tmp_path / "browser-profile"))
    (root / "config.yaml").write_text(config, encoding="utf-8")
    monkeypatch.setenv("HELPER_ROOT", str(root))
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    monkeypatch.delenv("WORKDAY_EMAIL", raising=False)
    monkeypatch.delenv("WORKDAY_PASSWORD", raising=False)
    return root


@pytest.fixture
def sample_profile_text() -> str:
    return (FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")
