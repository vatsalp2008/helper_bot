"""`helper setup`: folders, SQLite, tool checks, data-file checks, and a report of what is missing.

Runs without prompts by default because Claude runs it without a terminal. `--interactive` prompts
for missing profile values when you run it yourself.
"""

from __future__ import annotations

import importlib.metadata
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ValidationError

from .config import Paths, env_value, load_config, load_yaml
from .errors import HelperError
from .latex import compile_tex, find_binary, kpsewhich
from .master import load_master
from .models import (
    AnswersFile,
    CapsFile,
    Config,
    CoursesFile,
    Profile,
    ProjectSetsFile,
    ResumeMetaFile,
    SkillSynonymsFile,
    TechFamiliesFile,
    find_gpa_keys,
)
from .tracker.db import init_db
from .yamledit import set_scalar_in_file

Status = Literal["ok", "warn", "error"]
Prompt = Callable[[str], str]


@dataclass
class Check:
    name: str
    status: Status
    detail: str


FONTAWESOME_TEST = r"""\documentclass{article}
\usepackage{fontawesome5}
\begin{document}
\faPhone*~\faEnvelope~\faLinkedin~\faGithub~\faGlobe~\faIcon{external-link-alt}
\end{document}
"""

DATA_MODELS: dict[str, type[BaseModel]] = {
    "courses.yaml": CoursesFile,
    "answers.yaml": AnswersFile,
    "project_sets.yaml": ProjectSetsFile,
    "caps.yaml": CapsFile,
    "resume_meta.yaml": ResumeMetaFile,
    "skill_synonyms.yaml": SkillSynonymsFile,
    "tech_families.yaml": TechFamiliesFile,
}


def run_setup(
    paths: Paths,
    *,
    interactive: bool = False,
    check_latex: bool = True,
    check_browser: bool = True,
    prompt: Optional[Prompt] = None,
) -> list[Check]:
    cfg = load_config(paths)
    checks = [Check("config.yaml", "ok", "valid")]
    checks.append(_folders(paths, cfg))
    db = paths.db(cfg)
    checks.append(Check("SQLite", "ok", f"{'created' if init_db(db) else 'ready'}: {db.name}"))
    if check_latex:
        checks.append(_latex_check(cfg))
    checks.append(_poppler_check())
    if check_browser:
        checks.append(_playwright_check())
    checks.extend(_data_checks(paths, cfg))
    checks.extend(_profile_checks(paths, interactive, prompt))
    checks.append(_notion_check(paths))
    return checks


def _folders(paths: Paths, cfg: Config) -> Check:
    made = []
    for folder in (paths.runs, paths.resumes, paths.exports, paths.browser_profile(cfg)):
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            made.append(str(folder.relative_to(paths.root)) if folder.is_relative_to(paths.root) else str(folder))
    return Check("folders", "ok", ("created " + ", ".join(made)) if made else "all present")


def _latex_check(cfg: Config) -> Check:
    engine = find_binary(cfg.latex_engine)
    if engine is None:
        return Check("LaTeX", "error", f"{cfg.latex_engine} not found. Install MacTeX or TeX Live.")
    if kpsewhich("fontawesome5.sty") is None:
        return Check("LaTeX", "error", "fontawesome5 is missing: run `sudo tlmgr install fontawesome5`.")
    with tempfile.TemporaryDirectory(prefix="helper-setup-") as tmp:
        tex = Path(tmp) / "fa_test.tex"
        tex.write_text(FONTAWESOME_TEST, encoding="utf-8")
        result = compile_tex(tex, engine=cfg.latex_engine)
    if not result.ok:
        first = result.errors[0] if result.errors else "no PDF produced"
        return Check("LaTeX", "error", f"fontawesome5 test compile failed: {first}")
    return Check("LaTeX", "ok", f"{engine.name} in {engine.parent}; fontawesome5 test compile passed")


def _poppler_check() -> Check:
    missing = [b for b in ("pdftotext", "pdftoppm", "pdfinfo") if find_binary(b) is None]
    if missing:
        return Check("poppler", "warn", f"missing {', '.join(missing)} (needed from milestone 3): brew install poppler")
    return Check("poppler", "ok", "pdftotext, pdftoppm, pdfinfo found")


def _playwright_check() -> Check:
    try:
        version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError:
        return Check("Playwright", "error", "Python package missing: run `uv pip install -e '.[dev]'`")
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            executable = Path(p.chromium.executable_path)
    except Exception as exc:  # the driver failing to start should not stop setup
        return Check("Playwright", "warn", f"{version} installed; could not query browsers ({type(exc).__name__})")
    if executable.exists():
        return Check("Playwright", "ok", f"{version}; Chromium ready")
    return Check(
        "Playwright", "warn",
        f"{version} installed; Chromium not downloaded yet (needed from milestone 7): "
        "`.venv/bin/playwright install chromium`",
    )


def _data_checks(paths: Paths, cfg: Config) -> list[Check]:
    checks = []
    try:
        checks.append(Check("master list", "ok", paths.master_list(cfg).name))
    except HelperError as exc:
        checks.append(Check("master list", "error", str(exc)))
    if paths.master_yaml.is_file():
        try:
            master = load_master(paths, cfg)
            checks.append(Check(
                "master.yaml", "ok",
                f"up to date: {len(master.projects)} projects, {len(master.experience)} experience entries",
            ))
        except HelperError as exc:
            checks.append(Check("master.yaml", "warn", str(exc).splitlines()[0]))
    else:
        checks.append(Check("master.yaml", "warn", "not built yet: run `helper convert`"))

    loaded: dict[str, Any] = {}
    for name, model in DATA_MODELS.items():
        try:
            raw = load_yaml(paths.data / name)
        except HelperError as exc:
            checks.append(Check(name, "error", str(exc)))
            continue
        gpa = find_gpa_keys(raw)
        if gpa:
            checks.append(Check(name, "error", f"GPA keys are not allowed (rule 3): {', '.join(gpa)}"))
            continue
        try:
            loaded[name] = model.model_validate(raw)
        except ValidationError as exc:
            checks.append(Check(name, "error", _first_errors(exc)))
            continue
        checks.append(Check(name, "ok", _summary(name, loaded[name])))

    sets, meta = loaded.get("project_sets.yaml"), loaded.get("resume_meta.yaml")
    if sets and meta:
        known = set(meta.project_display_names)
        unknown = sorted(
            {pid for rt in sets.role_types.values() for pid in rt.projects + rt.plus_one_of} - known
        )
        if unknown:
            checks.append(Check("project ids", "error", f"project_sets.yaml uses unknown ids: {', '.join(unknown)}"))
    return checks


def _summary(name: str, model: BaseModel) -> str:
    if isinstance(model, CoursesFile):
        return f"{len(model.courses)} courses"
    if isinstance(model, AnswersFile):
        return f"{len(model.answers)} approved answers"
    if isinstance(model, ProjectSetsFile):
        return f"{len(model.role_types)} role types"
    if isinstance(model, CapsFile):
        return f"{len(model.caps)} caps, {len(model.notes)} notes"
    if isinstance(model, ResumeMetaFile):
        return f"{len(model.project_display_names)} project names, {len(model.experience)} experience entries"
    if isinstance(model, SkillSynonymsFile):
        return f"{len(model.approved_terms())} of {len(model.synonyms)} synonyms approved"
    if isinstance(model, TechFamiliesFile):
        return f"{len(model.approved())} of {len(model.families)} tech families approved"
    return "valid"


def _first_errors(exc: ValidationError, limit: int = 3) -> str:
    parts = []
    for err in exc.errors()[:limit]:
        where = ".".join(str(p) for p in err["loc"]) or "(top level)"
        parts.append(f"{where}: {err['msg']}")
    more = len(exc.errors()) - limit
    return "; ".join(parts) + (f"; and {more} more" if more > 0 else "")


# --------------------------------------------------------------------------- profile


def _profile_checks(paths: Paths, interactive: bool, prompt: Optional[Prompt]) -> list[Check]:
    checks = []
    if not paths.profile.exists():
        example = paths.data / "profile.example.yaml"
        if not example.is_file():
            return [Check("profile.yaml", "error", "missing, and data/profile.example.yaml is gone too")]
        shutil.copy(example, paths.profile)
        checks.append(Check("profile.yaml", "warn", "created from profile.example.yaml; fill it in"))
    raw = load_yaml(paths.profile) or {}
    if interactive:
        _prompt_for_missing(paths.profile, raw, prompt or _terminal_prompt)
        raw = load_yaml(paths.profile) or {}
    try:
        profile = Profile.model_validate(raw)
    except ValidationError as exc:
        checks.append(Check("profile.yaml", "error", _first_errors(exc)))
        return checks
    missing = profile.missing_values()
    if missing:
        checks.append(Check(
            "profile.yaml", "warn",
            f"valid; {len(missing)} unset value(s) will pause forms: {', '.join(missing)}",
        ))
    else:
        checks.append(Check("profile.yaml", "ok", "valid; nothing unset"))
    return checks


def unset_paths(raw: Any) -> list[str]:
    """Dotted paths of null values worth asking about (skips notes, optional unit, disabled Tier 2 items)."""
    found: list[str] = []

    def walk(node: Any, path: str, enabled: bool) -> None:
        if isinstance(node, dict):
            here_enabled = enabled and node.get("auto_answer", True) is not False
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key), here_enabled)
        elif node is None and enabled and not path.endswith((".note", ".unit")):
            found.append(path)

    walk(raw, "", True)
    return found


def _prompt_for_missing(profile_path: Path, raw: Any, prompt: Prompt) -> None:
    for path in unset_paths(raw):
        answer = prompt(f"{path} (Enter to leave unset)").strip()
        if answer:
            set_scalar_in_file(profile_path, path, answer)


def _terminal_prompt(question: str) -> str:
    import typer

    return typer.prompt(question, default="", show_default=False)


def _notion_check(paths: Paths) -> Check:
    if env_value(paths, "NOTION_TOKEN") and env_value(paths, "NOTION_DATABASE_ID"):
        return Check("Notion", "ok", "token and database ID set (sync arrives in milestone 6)")
    return Check("Notion", "warn", "not configured yet; connects in milestone 6 (see .env.example)")


# --------------------------------------------------------------------------- report

_LABEL = {"ok": "OK  ", "warn": "WARN", "error": "FAIL"}


def format_report(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"[{_LABEL[c.status]}] {c.name.ljust(width)}  {c.detail}" for c in checks]
    errors = sum(c.status == "error" for c in checks)
    warnings = sum(c.status == "warn" for c in checks)
    lines.append("")
    lines.append(f"{errors} problem(s), {warnings} warning(s).")
    return "\n".join(lines)
