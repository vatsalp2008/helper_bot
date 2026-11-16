"""Load data/master.yaml, refusing a copy that is older than the master list markdown."""

from __future__ import annotations

import hashlib

from pydantic import ValidationError

from .config import Paths, load_yaml
from .errors import HelperError, problems
from .models import Config, MasterData


def master_list_sha256(paths: Paths, cfg: Config) -> str:
    return hashlib.sha256(paths.master_list(cfg).read_bytes()).hexdigest()


def load_master(paths: Paths, cfg: Config, check_fresh: bool = True) -> MasterData:
    if not paths.master_yaml.is_file():
        raise HelperError("data/master.yaml is missing: run `helper convert`.")
    try:
        master = MasterData.model_validate(load_yaml(paths.master_yaml))
    except ValidationError as exc:
        raise HelperError(f"data/master.yaml is out of date or damaged; run `helper convert` to rebuild it from the master list.\n{problems(exc)}") from None
    if check_fresh and master.source.sha256 != master_list_sha256(paths, cfg):
        raise HelperError(
            f"{paths.master_list(cfg).name} changed since the last `helper convert`. Run it again first."
        )
    return master
