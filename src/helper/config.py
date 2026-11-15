"""Repo paths, config.yaml loading, and a tiny .env loader."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .errors import HelperError
from .models import Config

ROOT_ENV_VAR = "HELPER_ROOT"


def find_root(start: Path | None = None) -> Path:
    """The helper repo root: $HELPER_ROOT, else the nearest folder above `start` with config.yaml + data/."""
    env = os.environ.get(ROOT_ENV_VAR)
    if env:
        root = Path(env).expanduser().resolve()
        if not (root / "config.yaml").is_file():
            raise HelperError(f"{ROOT_ENV_VAR}={env} has no config.yaml.")
        return root
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config.yaml").is_file() and (candidate / "data").is_dir():
            return candidate
    checkout = Path(__file__).resolve().parents[2]  # src/helper/config.py -> repo root (editable install)
    if (checkout / "config.yaml").is_file():
        return checkout
    raise HelperError("Could not find the helper repo (config.yaml and data/). Run inside it or set HELPER_ROOT.")


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def config_file(self) -> Path:
        return self.root / "config.yaml"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def master_yaml(self) -> Path:
        return self.data / "master.yaml"

    @property
    def profile(self) -> Path:
        return self.data / "profile.yaml"

    @property
    def courses(self) -> Path:
        return self.data / "courses.yaml"

    @property
    def answers(self) -> Path:
        return self.data / "answers.yaml"

    @property
    def project_sets(self) -> Path:
        return self.data / "project_sets.yaml"

    @property
    def caps(self) -> Path:
        return self.data / "caps.yaml"

    @property
    def resume_meta(self) -> Path:
        return self.data / "resume_meta.yaml"

    @property
    def skill_synonyms(self) -> Path:
        return self.data / "skill_synonyms.yaml"

    @property
    def tech_families(self) -> Path:
        return self.data / "tech_families.yaml"

    @property
    def tech_lexicon(self) -> Path:
        return self.data / "tech_lexicon.yaml"

    @property
    def templates(self) -> Path:
        return self.root / "templates"

    @property
    def resume_template(self) -> Path:
        return self.templates / "resume.tex.j2"

    @property
    def reference_tex(self) -> Path:
        return self.templates / "reference_resume.tex"

    @property
    def reference_pdf(self) -> Path:
        return self.templates / "reference_resume.pdf"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def resumes(self) -> Path:
        return self.root / "resumes"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    def master_list(self, cfg: Config) -> Path:
        """config.master_list if set, else the newest data/master_list_v<N>.md."""
        if cfg.master_list:
            path = Path(cfg.master_list)
            return path if path.is_absolute() else self.root / path
        versions = []
        for path in self.data.glob("master_list_v*.md"):
            m = re.fullmatch(r"master_list_v(\d+)\.md", path.name)
            if m:
                versions.append((int(m.group(1)), path))
        if not versions:
            raise HelperError("No data/master_list_v<N>.md found. Put the master list in data/.")
        return max(versions)[1]

    def db(self, cfg: Config) -> Path:
        path = Path(cfg.db_path).expanduser()
        return path if path.is_absolute() else self.root / path

    def browser_profile(self, cfg: Config) -> Path:
        return Path(cfg.browser_profile_dir).expanduser()


def load_yaml(path: Path) -> Any:
    if not path.is_file():
        raise HelperError(f"Missing file: {path}")
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise HelperError(f"{path} is not valid YAML: {exc}") from exc


def load_config(paths: Paths) -> Config:
    data = load_yaml(paths.config_file) or {}
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise HelperError(f"config.yaml is invalid:\n{exc}") from exc


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Blank lines and # comments are ignored; surrounding quotes are stripped."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def env_value(paths: Paths, key: str) -> str | None:
    """A setting from the real environment first, then .env. Empty strings count as unset."""
    value = os.environ.get(key) or read_env_file(paths.env_file).get(key)
    return value or None
