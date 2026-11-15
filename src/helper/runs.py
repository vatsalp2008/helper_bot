"""runs/<run_id>/: one folder per application. state.json records where the run is, so every command can resume."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from .config import Paths
from .errors import HelperError
from .models import RunState, StepState, StepStatus
from .tracker.db import now_iso


def make_run_id(company: str, role: str, now: Optional[datetime] = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", f"{company} {role}".lower()).strip("-")[:48].rstrip("-")
    return f"{stamp}-{slug}" if slug else stamp


def run_dir(paths: Paths, run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise HelperError(f"'{run_id}' is not a run id (see the folders under runs/)")
    return paths.runs / run_id


def rel(paths: Paths, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(paths.root.resolve()))
    except ValueError:
        return str(path)


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HelperError(f"{path} is missing") from None
    except json.JSONDecodeError as exc:
        raise HelperError(f"{path} is not valid JSON: {exc}") from None


def load_state(paths: Paths, run_id: str) -> RunState:
    path = run_dir(paths, run_id) / "state.json"
    if not path.is_file():
        raise HelperError(f"no run '{run_id}': {rel(paths, path)} doesn't exist")
    try:
        return RunState.model_validate(read_json(path))
    except ValidationError as exc:
        raise HelperError(f"{rel(paths, path)} is damaged: {exc}") from None


def save_state(paths: Paths, state: RunState) -> Path:
    return write_json(run_dir(paths, state.run_id) / "state.json", state.model_dump(mode="json"))


def set_step(state: RunState, name: str, status: StepStatus, message: Optional[str] = None) -> None:
    state.steps[name] = StepState(status=status, updated_at=now_iso(), message=message)
