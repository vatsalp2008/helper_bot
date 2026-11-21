from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from helper.convert import parse_master, vocabulary_for
from helper.keywords import extract_keywords, split_sections
from helper.models import MasterData, ProjectSetsFile, TechFamiliesFile, TechFamily

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
JD = (FIXTURES / "jd_backend_intern.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def setup() -> tuple[MasterData, object, ProjectSetsFile]:
    lexicon = yaml.safe_load((REPO / "data" / "tech_lexicon.yaml").read_text(encoding="utf-8"))
    master, _, _ = parse_master((REPO / "data" / "master_list_v23.md").read_text(encoding="utf-8"), lexicon, "x.md")
    sets = ProjectSetsFile.model_validate(yaml.safe_load((REPO / "data" / "project_sets.yaml").read_text(encoding="utf-8")))
    return master, vocabulary_for(master, lexicon), sets


def test_sections_are_found_and_classified() -> None:
    sections = split_sections(JD)
    assert [(s.kind, s.heading) for s in sections] == [
        ("other", None),
        ("responsibilities", "About the role"),
        ("responsibilities", "What you'll do"),
        ("required", "Basic qualifications"),
        ("preferred", "Preferred qualifications"),
    ]
    assert "Experience with Scala or Cassandra" in sections[-1].lines


@pytest.mark.parametrize(
    "text,kind",
    [
        ("## Minimum Qualifications", "required"),
        ("**Nice to have**", "preferred"),
        ("RESPONSIBILITIES", "responsibilities"),
        ("Benefits:", "other"),
    ],
)
def test_heading_forms(text: str, kind: str) -> None:
    sections = split_sections(f"{text}\n- something")
    assert sections[-1].kind == kind


def test_technologies_are_sorted_against_the_master_list(setup) -> None:  # type: ignore[no-untyped-def]
    master, vocab, sets = setup
    kw = extract_keywords(JD, master, vocab, sets, TechFamiliesFile())
    status = {t.canonical: t.status for t in kw.tech}
    assert status["Kafka"] == status["PostgreSQL"] == status["Apache Flink"] == "in_items"
    assert status["CI/CD"] == "skills_only"
    assert status["Scala"] == status["Cassandra"] == "not_in_master"
    kafka = kw.term("Kafka")
    assert kafka is not None and "pulsetrader" in kafka.items and kafka.sections == ["responsibilities"]
    assert kw.term("Apache Spark").spellings == ["Spark"]  # type: ignore[union-attr]
    assert kw.open_source_signals == ["code reviews", "debug", "open source", "reliability"]
    assert kw.item_scores[0].id == "ecommerce_platform"
    assert {r.id for r in kw.role_type_scores[:2]} == {"data_engineering", "backend_distributed"}


def test_approved_families_list_possible_swaps(setup) -> None:  # type: ignore[no-untyped-def]
    master, vocab, sets = setup
    families = TechFamiliesFile(families=[TechFamily(id="ci", members=["GitHub Actions", "GitLab CI"], approved=True)])
    kw = extract_keywords("Experience with GitLab CI pipelines", master, vocab, sets, families)
    gitlab = kw.term("GitLab CI")
    assert gitlab is not None and "experimentiq (GitHub Actions)" in gitlab.swaps
    unapproved = extract_keywords("Experience with GitLab CI pipelines", master, vocab, sets, TechFamiliesFile())
    assert unapproved.term("GitLab CI").swaps == []  # type: ignore[union-attr]


def test_a_spaced_spelling_of_xgboost_is_found(setup) -> None:  # type: ignore[no-untyped-def]
    # KLA's posting (2026-11-19) spells it "XG Boost"; the keyword step missed it, so projects with XGBoost scored low
    master, vocab, sets = setup
    kw = extract_keywords("Requirements\n- Hands-on experience with PyTorch, XG Boost or Random Forest", master, vocab,
                          sets, TechFamiliesFile())
    xgboost = kw.term("XGBoost")
    assert xgboost is not None and xgboost.status == "in_items" and xgboost.spellings == ["XG Boost"]


def test_everyday_rest_and_the_rocky_mountains_are_no_technologies(setup) -> None:  # type: ignore[no-untyped-def]
    # Melaleuca (2026-11-20): "the rest of Melaleuca" read as REST APIs and "the Rocky Mountains" as Rocky Linux, so
    # projects scored on words the posting never meant as technologies
    master, vocab, sets = setup
    prose = "About us\n- Join the rest of Melaleuca in Idaho Falls, at the foot of the Rocky Mountains.\n"
    kw = extract_keywords(prose, master, vocab, sets, TechFamiliesFile())
    assert kw.term("REST APIs") is None and kw.term("Rocky Linux") is None
    kw = extract_keywords(prose + "Requirements\n- Build REST APIs and RESTful services on RHEL or Rocky servers",
                          master, vocab, sets, TechFamiliesFile())
    rest, rocky = kw.term("REST APIs"), kw.term("Rocky Linux")
    assert rest is not None and "rest" not in rest.spellings and rest.count == 2
    assert rocky is not None and rocky.spellings == ["Rocky"] and rocky.count == 1


def test_t_mobile_fiber_is_no_go_framework(setup) -> None:  # type: ignore[no-untyped-def]
    # T-Mobile (2026-11-20): "The Fiber Growth team" read as Go's Fiber framework, listed as a gap no resume could fill
    master, vocab, sets = setup
    kw = extract_keywords("What It's Like\n- The Fiber Growth team acquires new fiber customers.\n", master, vocab,
                          sets, TechFamiliesFile())
    assert kw.term("Fiber") is None
    kw = extract_keywords("Requirements\n- Build Go services with Go Fiber or Gin.\n", master, vocab, sets,
                          TechFamiliesFile())
    fiber = kw.term("Fiber")
    assert fiber is not None and fiber.spellings == ["Go Fiber"]
