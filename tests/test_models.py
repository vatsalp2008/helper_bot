from __future__ import annotations

import json
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from helper.config import Paths, load_config
from helper.models import Config, Profile, TailorOutput, find_gpa_keys

from .conftest import FIXTURES, REPO


def _key_paths(node: Any, prefix: str = "") -> set[str]:
    """Dotted mapping keys, not descending into lists, ignoring optional `note` keys."""
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "note":
                continue
            path = f"{prefix}.{key}" if prefix else key
            keys.add(path)
            keys |= _key_paths(value, path)
    return keys


# --------------------------------------------------------------------------- config


def test_config_matches_spec_values() -> None:
    cfg = load_config(Paths(REPO))
    assert cfg.projects_per_resume == 5
    assert cfg.bullets_per_item == 2
    assert cfg.submit_mode == "review"
    assert cfg.auto_submit_min_clean_runs == 5
    assert cfg.default_grad_date == "May 2027"
    assert cfg.alt_grad_date == "December 2027"
    assert cfg.browser_profile_dir == "~/.helper/browser-profile"
    assert cfg.headless is False


@pytest.mark.parametrize(
    "bad",
    [
        {"submit_mode": "yolo"},
        {"default_grad_date": "May 27"},
        {"alt_grad_date": "Dec. 2027"},
        {"match_ask_threshold": 0.9, "match_auto_threshold": 0.85},
        {"pause_ms": [600, 150]},
        {"projects_per_resume": 0},
        {"unknown_key": 1},
    ],
)
def test_config_rejects_bad_values(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Config.model_validate(bad)


# --------------------------------------------------------------------------- profile


def test_sample_profile_reports_unset_values(sample_profile_text: str) -> None:
    profile = Profile.model_validate(yaml.safe_load(sample_profile_text))
    missing = profile.missing_values()
    assert "tier1.preferred_name" in missing
    assert "tier2.eeo.gender" in missing  # EEO enabled but not set
    assert "tier2.security_clearance.value" not in missing  # disabled items never pause as "missing"
    assert "tier2.earliest_start_date.fall_internship" not in missing  # "ask" is a choice, not a gap
    assert "tier1.address.unit" not in missing  # optional


@pytest.mark.parametrize("where", ["top", "tier1", "education"])
def test_profile_rejects_gpa_anywhere(sample_profile_text: str, where: str) -> None:
    data = yaml.safe_load(sample_profile_text)
    if where == "top":
        data["gpa"] = "3.9"
    elif where == "tier1":
        data["tier1"]["GPA"] = "3.9"
    else:
        data["education"][0]["cumulative_gpa"] = "3.9"
    with pytest.raises(ValidationError, match="GPA"):
        Profile.model_validate(data)


def test_profile_rejects_unknown_keys(sample_profile_text: str) -> None:
    data = yaml.safe_load(sample_profile_text)
    data["tier1"]["favorite_color"] = "blue"
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_example_profile_has_the_same_shape_as_the_sample(sample_profile_text: str) -> None:
    example = yaml.safe_load((REPO / "data" / "profile.example.yaml").read_text(encoding="utf-8"))
    assert _key_paths(example) == _key_paths(yaml.safe_load(sample_profile_text))


def test_real_profile_is_valid_and_matches_the_example_shape() -> None:
    path = REPO / "data" / "profile.yaml"
    if not path.exists():
        pytest.skip("data/profile.yaml is personal and git-ignored")
    real = yaml.safe_load(path.read_text(encoding="utf-8"))
    Profile.model_validate(real)
    example = yaml.safe_load((REPO / "data" / "profile.example.yaml").read_text(encoding="utf-8"))
    assert _key_paths(real) == _key_paths(example)


# --------------------------------------------------------------------------- tailor output


def _sample_output() -> dict[str, Any]:
    return json.loads((FIXTURES / "tailor_output_sample.json").read_text(encoding="utf-8"))


def test_tailor_output_sample_parses() -> None:
    out = TailorOutput.model_validate(_sample_output())
    assert len(out.projects) == 5
    assert out.experience[0].title.startswith("Graduate Teaching Assistant")


def test_tailor_output_has_no_room_for_a_summary_section() -> None:
    data = _sample_output()
    data["summary"] = "Motivated engineer."
    with pytest.raises(ValidationError):
        TailorOutput.model_validate(data)


def test_tailor_output_rejects_gpa() -> None:
    data = _sample_output()
    data["gpa"] = "3.9"
    with pytest.raises(ValidationError, match="GPA"):
        TailorOutput.model_validate(data)


def test_find_gpa_keys_walks_lists_and_dicts() -> None:
    assert find_gpa_keys({"a": [{"b": {"GPA_value": 1}}]}) == ["a[0].b.GPA_value"]
    assert find_gpa_keys({"grade": 1}) == []
