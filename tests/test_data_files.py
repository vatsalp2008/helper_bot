from __future__ import annotations

from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from helper.models import (
    AnswersFile,
    Cap,
    CapsFile,
    CoursesFile,
    ProjectSetsFile,
    ResumeMetaFile,
    find_gpa_keys,
)

from .conftest import REPO


def load(name: str) -> Any:
    return yaml.safe_load((REPO / "data" / name).read_text(encoding="utf-8"))


def test_courses_are_the_confirmed_eight() -> None:
    courses = CoursesFile.model_validate(load("courses.yaml")).courses
    assert courses[:5] == ["Statistics", "Machine Learning", "Database Systems", "Algorithms", "Data Structures"]
    assert courses[5:] == ["Computer Vision", "Distributed Systems", "Artificial Intelligence"]


def test_courses_reject_duplicates_and_short_lists() -> None:
    with pytest.raises(ValidationError):
        CoursesFile.model_validate({"courses": ["A", "B", "C", "D", "A"]})
    with pytest.raises(ValidationError):
        CoursesFile.model_validate({"courses": ["A", "B"]})


def test_caps_match_the_spec_seeds() -> None:
    caps = CapsFile.model_validate(load("caps.yaml"))
    by_company = {cap.company: cap for cap in caps.caps}
    assert (by_company["Salesforce"].max_applications, by_company["Salesforce"].window_months) == (3, 12)
    assert (by_company["Samsung"].max_applications, by_company["Samsung"].window_months) == (10, 6)
    assert (by_company["Databricks"].max_applications, by_company["Databricks"].window_days) == (1, 30)
    assert {note.company for note in caps.notes} == {"Qualcomm", "Google"}


@pytest.mark.parametrize(
    "cap",
    [
        {"company": "X", "max_applications": 1},
        {"company": "X", "max_applications": 1, "window_days": 1, "window_months": 1},
        {"company": "X", "max_applications": 0, "window_days": 30},
    ],
)
def test_cap_needs_one_window_and_a_positive_limit(cap: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Cap.model_validate(cap)


def test_resume_meta_covers_every_project_and_experience_entry() -> None:
    meta = ResumeMetaFile.model_validate(load("resume_meta.yaml"))
    assert len(meta.project_display_names) == 26
    assert set(meta.experience) == {"ta", "kalini_sde", "kalini_ml", "event_assistant", "student_proctor", "baps"}
    assert meta.experience["ta"].resume_title == "Graduate Teaching Assistant, Data Structures and Algorithms in C"
    assert meta.experience["kalini_sde"].location == meta.experience["kalini_ml"].location == "Mumbai, India"
    assert meta.project_display_names["f1_analytics"] == "F1 Analytics"


def test_project_sets_follow_the_spec_table() -> None:
    sets = ProjectSetsFile.model_validate(load("project_sets.yaml")).role_types
    known = set(ResumeMetaFile.model_validate(load("resume_meta.yaml")).project_display_names)
    assert len(sets) == 17
    for key, role in sets.items():
        assert set(role.projects + role.plus_one_of) <= known, key
        assert len(role.projects) <= 5, key
        assert not set(role.projects) & set(role.plus_one_of), key
    assert sets["backend_distributed"].projects == ["pulsetrader", "ecommerce_platform", "spiceai", "experimentiq", "chef"]
    assert sets["data_science_analytics"].plus_one_of == ["housing_price", "investment_prediction", "echocoach", "moodmeals"]
    assert sets["robotics_av"].projects == ["perceptnet", "scenarioforge"]
    # spec 7.1: Software Development Intern for SWE/infra/full-stack/mobile, Machine Learning Intern for ML/data/AI
    assert {sets[k].kalini for k in ("backend_distributed", "fullstack_web", "frontend", "ios", "android")} == {"sde"}
    assert {sets[k].kalini for k in ("data_science_analytics", "data_engineering", "ml_engineering", "applied_ai_agents")} == {"ml"}


def test_answers_file_loads() -> None:
    AnswersFile.model_validate(load("answers.yaml"))


def test_no_data_file_has_a_gpa_key() -> None:
    # data/grades.yaml is the one exception: forms only, never read for a resume (rule 3; Vatsal, 2026-11-15)
    for path in sorted((REPO / "data").glob("*.yaml")):
        if path.name != "grades.yaml":
            assert not find_gpa_keys(yaml.safe_load(path.read_text(encoding="utf-8"))), path.name
