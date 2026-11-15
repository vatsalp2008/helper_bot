"""The converter, run against the real master list in data/."""

from __future__ import annotations

import re

import pytest
import yaml

from helper.convert import dump_master, parse_field_line, parse_item_block, parse_master
from helper.models import MasterData, ResumeMetaFile
from helper.numbers import find_numbers
from helper.wording import violations

from .conftest import REPO

MASTER_MD = REPO / "data" / "master_list_v23.md"


@pytest.fixture(scope="module")
def master() -> MasterData:
    lexicon = yaml.safe_load((REPO / "data" / "tech_lexicon.yaml").read_text(encoding="utf-8"))
    data, _, _ = parse_master(MASTER_MD.read_text(encoding="utf-8"), lexicon, "data/master_list_v23.md")
    return data


def test_counts_match_the_file_and_nothing_is_left_unparsed(master: MasterData) -> None:
    assert len(master.projects) == master.source.declared_projects == 26
    assert len(master.experience) == master.source.declared_experience == 6
    assert master.problems == []


def test_ids_match_resume_meta(master: MasterData) -> None:
    meta = ResumeMetaFile.model_validate(yaml.safe_load((REPO / "data" / "resume_meta.yaml").read_text()))
    assert {p.id for p in master.projects} == set(meta.project_display_names)
    assert {e.id for e in master.experience} == set(meta.experience)


def test_every_project_has_dates_status_and_a_link(master: MasterData) -> None:
    for project in master.projects:
        assert project.dates and project.status and project.github_url, project.id


def test_every_bullet_in_the_markdown_is_kept_verbatim(master: MasterData) -> None:
    text = MASTER_MD.read_text(encoding="utf-8")
    body = text[text.index("## PROJECT DETAILS"): text.index("## SUMMARY STATISTICS")]
    source_bullets = [line[2:].strip() for line in body.splitlines() if line.startswith("- ")]
    parsed = {b for item in master.items() for b in item.bullets()}
    assert len(source_bullets) == len(parsed) > 150
    assert all(b in parsed for b in source_bullets)


def test_metrics_are_split_verbatim(master: MasterData) -> None:
    metrics = master.item("experimentiq").metrics["Key Metrics"]
    assert "45s→0.8s metric computation" in metrics
    assert "20x speedup (800ms→40ms) on assignment log query via composite indexing" in metrics
    assert len(master.item("echocoach").metrics) == 2  # the eval metrics and the fine-tuning metrics


def test_statuses_are_kept_as_written(master: MasterData) -> None:
    assert master.item("spiceai").status == "Merged (PR #13507)"
    assert master.item("chef").status == "Merged (2026-08-31)"
    assert master.item("pnwer").status == "In Progress (Experience Expo)"
    assert master.item("pnwer").status_kind == "in_progress"
    assert master.item("streamline").status_kind == "complete"


@pytest.mark.parametrize(
    "item_id,display",
    [
        ("f1_analytics", "Jun. 2025 -- Oct. 2025"),  # these five match reference_resume.tex
        ("experimentiq", "Jul. 2026 -- Aug. 2026"),
        ("investment_prediction", "Jan. 2025 -- May 2025"),
        ("housing_price", "Sept. 2024 -- Dec. 2024"),
        ("traffic_accident", "Mar. 2026 -- Apr. 2026"),
        ("kalini_sde", "Jan. 2024 -- Apr. 2024"),
        ("ta", "Jan. 2026 -- May 2026"),
        ("spiceai", "Jul. 2026 -- Present"),
        ("baps", "Ongoing"),
    ],
)
def test_dates(master: MasterData, item_id: str, display: str) -> None:
    assert master.item(item_id).dates.display == display  # type: ignore[union-attr]


def test_wording_rules_are_attached_with_their_master_text(master: MasterData) -> None:
    spiceai = {r.id: r for r in master.item("spiceai").wording_rules}
    assert set(spiceai) == {"spiceai_pr_merged", "spiceai_12747_bug_report"}
    assert 'never "no PRs merged yet."' in spiceai["spiceai_pr_merged"].source
    assert "bug report that led to the CEO's overhaul" in spiceai["spiceai_12747_bug_report"].source
    assert "pending re-approval" in master.item("chef").wording_rules[0].source
    assert master.item("ecommerce_platform").wording_rules[0].source == "spec 5.1"
    assert master.item("ta").wording_rules[0].title_prefix == "Graduate Teaching Assistant"


@pytest.mark.parametrize(
    "item_id,text,ok",
    [
        ("chef", "Merged a fix for integer umask values into chef/chef", True),
        ("chef", "Fixed integer umask handling; PR pending re-approval", False),
        ("spiceai", "Merged PR #13507, with 2 more PRs open", True),
        ("spiceai", "PR #13507 is still pending", False),
        ("spiceai", "Filed issue #12747, a bug report that led to the CEO's 2,244-line overhaul", True),
        ("spiceai", "Implemented the Decimal128 overflow overhaul", False),
        ("spiceai", "Drove a 2,244-line correctness overhaul", False),
        ("ecommerce_platform", "Built 9 Java microservices", False),
        ("ecommerce_platform", "Built 9 microservices in Java Spring Boot plus one service in Go", True),
        ("ecommerce_platform", "Built 9 microservices in Java Spring Boot", False),
        # metric precision rules (spec 7.4)
        ("experimentiq", "Modeled 10M+ simulated events in 12 dbt models", True),
        ("experimentiq", "Processed 10M+ events in 12 dbt models", False),
        ("pulsetrader", "Processed 10,000 trade events/sec across 8 symbols", True),
        ("pulsetrader", "Processed 10,000 events/sec across 8 symbols", False),
        ("echocoach", "Traded a borderline tone drop (p = 0.06) for actionability", True),
        ("echocoach", "Kept tone steady while lifting actionability by +1.3", False),
        ("perceptnet", "Reached ~77% Car mAP@0.7 on KITTI", True),
        ("perceptnet", "Reached ~77% mAP after TensorRT INT8 quantization", False),
        ("kalini_ml", "Reached 85% accuracy across 5+ algorithms", True),
        ("kalini_ml", "Reached 85% accuracy with a 40% inference speedup", False),
    ],
)
def test_wording_rules_behave(master: MasterData, item_id: str, text: str, ok: bool) -> None:
    found = [v for rule in master.item(item_id).wording_rules for v in violations(rule, text)]
    assert (not found) is ok, found


def test_ta_title_rule(master: MasterData) -> None:
    rule = master.item("ta").wording_rules[0]
    assert violations(rule, "", title="Graduate Teaching Assistant, Data Structures and Algorithms in C") == []
    assert violations(rule, "", title="Teaching Assistant")


def test_database_framing(master: MasterData) -> None:
    assert master.database_framing is not None
    sql, nosql = master.database_framing.rules
    assert (sql.built_with, sql.also) == ("PostgreSQL", "MySQL")
    assert (nosql.built_with, nosql.also) == ("DynamoDB", "MongoDB")
    for pid in ("ecommerce_platform", "experimentiq", "seattle_transit", "housing_price"):
        assert "MySQL" in master.item(pid).allowed_tech, pid
    assert {"DynamoDB", "MongoDB"} <= set(master.item("investment_prediction").allowed_tech)
    # "Built with PostgreSQL" covers text and platforms, not only the Technologies line (Vatsal, 2026-11-14):
    # SWARM's text says Postgres, and Supabase's database is PostgreSQL.
    for pid in ("swarm", "moodmeals", "wardrobe"):
        assert "MySQL" in master.item(pid).allowed_tech, pid
    assert master.item("swarm").implied_tech == []  # its text already names Postgres
    assert master.item("moodmeals").implied_tech == ["PostgreSQL"]
    assert "MySQL" not in master.item("pulsetrader").allowed_tech  # TimescaleDB + BigQuery, not in the rule


@pytest.mark.parametrize(
    "item_id,text,ok",
    [
        ("swarm", "TypeScript, Next.js, MySQL\nLangGraph.js orchestrator over a MySQL-backed task queue", True),
        ("swarm", "TypeScript, Next.js, Supabase\nLangGraph.js orchestrator with Postgres LISTEN/NOTIFY", True),
        ("swarm", "TypeScript, MySQL\nLangGraph.js orchestrator with Postgres LISTEN/NOTIFY", False),
        ("swarm", "MySQL persistence\nOIDC SSO on Supabase Auth", False),
        ("swarm", "MySQL with listen/notify events", False),
        ("experimentiq", "MySQL, Redis\nCut the PostgreSQL-backed query from ~800ms to ~40ms", False),
        ("investment_prediction", "Flask, MySQL, DynamoDB\nDynamoDB for unstructured sentiment data", True),
        ("investment_prediction", "Flask, PostgreSQL, MongoDB\nDynamoDB for sentiment data", False),
    ],
)
def test_framed_entries_name_one_database(master: MasterData, item_id: str, text: str, ok: bool) -> None:
    rules = [r for r in master.item(item_id).wording_rules if r.scope == "entry"]
    assert rules
    found = [v for rule in rules for v in violations(rule, text)]
    assert (not found) is ok, found


def test_master_list_entries_pass_their_own_entry_rules(master: MasterData) -> None:
    for item in master.items():
        text = "\n".join([", ".join(item.technologies), *item.bullets()])
        for rule in item.wording_rules:
            if rule.scope == "entry":
                assert violations(rule, text) == [], item.id


def test_variants_are_claimable(master: MasterData) -> None:
    assert {"React", "Angular", "TypeScript", "Vue.js", "Django", "GitLab CI"} <= set(master.item("nutritrack").allowed_tech)
    assert "Kubernetes" in master.item("ecommerce_platform").variants[0]
    assert {i.id for i in master.items() if i.variants} == {"ecommerce_platform", "nutritrack"}


def test_comparisons_are_not_claims(master: MasterData) -> None:
    assert "Electron" not in master.item("themanager").allowed_tech
    assert "Stripe API" not in master.item("experimentiq").allowed_tech
    assert "Stripe API" not in master.item("pulsetrader").allowed_tech
    assert "Stripe API" in master.item("swarm").allowed_tech


def test_chef_technologies_come_from_its_text(master: MasterData) -> None:
    chef = master.item("chef")
    assert chef.technologies == []
    assert {"Ruby", "RSpec"} <= set(chef.allowed_tech)
    assert "Shell/Bash" not in chef.allowed_tech  # Chef's `bash` resource is not Bash scripting


def test_ta_numbers_follow_the_master_list(master: MasterData) -> None:
    ta = master.item("ta")
    assert {"100+", "19+"} <= set(ta.allowed_numbers)
    values = {n.value for raw in ta.allowed_numbers for n in find_numbers(raw)}
    assert 60 not in values  # the reference resume's "around 60 students" is not in the master list


def test_kalini_entries(master: MasterData) -> None:
    ml, sde = master.item("kalini_ml"), master.item("kalini_sde")
    assert {"85%", "40%", "15%", "20+", "5+"} <= set(ml.allowed_numbers)
    assert {"TensorFlow", "Scikit-learn", "Pandas", "SQL", "Flask"} <= set(ml.allowed_tech)
    assert {"Shell/Bash", "Slack API", "React", "Flask"} <= set(sde.allowed_tech)
    assert ml.organization == sde.organization == "Kalini Industries"


def test_identifier_tokens(master: MasterData) -> None:
    assert "p99" in master.item("streamline").allowed_tokens
    assert "INT8" in master.item("echocoach").allowed_tokens


def test_skills(master: MasterData) -> None:
    assert len(master.skills) == 13
    assert master.languages[:3] == ["Python", "Java", "TypeScript"]
    assert "C#" in master.languages and "Shell/Bash" in master.languages
    frontend = next(g for g in master.skills if g.name.startswith("Frontend"))
    django = next(i for i in frontend.items if i.name == "Django")
    assert django.note == "confirmed real for the PathAI application"
    assert master.database_framing is not None and master.database_framing.text.startswith("Projects built with PostgreSQL")


def test_other_sections_are_kept_verbatim(master: MasterData) -> None:
    assert {"TABLE OF CONTENTS", "CAREER POSITIONING", "PORTFOLIO HIGHLIGHTS"} <= set(master.other_sections)
    assert "Applied Intuition" in master.other_sections["CAREER POSITIONING"]


def test_dump_is_deterministic_and_round_trips(master: MasterData) -> None:
    text = dump_master(master)
    assert text.startswith("# Generated by `helper convert`")
    reloaded = MasterData.model_validate(yaml.safe_load(text))
    assert reloaded == master
    assert dump_master(reloaded) == text


# --------------------------------------------------------------------------- small synthetic cases


def test_parse_field_line() -> None:
    assert parse_field_line("**Organization:** Acme | **Location:** Seattle, WA | **Timeline:** May 2026 - Present") == [
        ("Organization", "Acme"), ("Location", "Seattle, WA"), ("Timeline", "May 2026 - Present"),
    ]
    assert parse_field_line("**Status: MERGED.** PR #1 merged.") == [("Status", "MERGED. PR #1 merged.")]
    assert parse_field_line("**Key Metrics:** a **b** | c") == [("Key Metrics", "a **b** | c")]
    assert parse_field_line("plain text") is None


def test_unparsed_lines_are_reported() -> None:
    parsed = parse_item_block([(1, "**Description:**"), (2, "- one"), (3, "a stray line")])
    assert parsed.sections == {"Description": ["one"]}
    assert parsed.unparsed == ["line 3: a stray line"]


def test_new_items_get_a_slug_and_a_problem() -> None:
    markdown = "\n".join([
        "## ALL PROJECTS OVERVIEW", "| 1 | Brand New | Tools | Jan 2027 - Feb 2027 | Complete |",
        "## PROJECT DETAILS", "### PROJECT 1: Brand New Thing - A Tool", "**Timeline:** January 2027 - February 2027",
        "**Technologies:** Python", "**Description:**", "- Built it",
        "## PROFESSIONAL EXPERIENCE", "## SKILLS & TECHNOLOGIES", "### Languages", "Python",
    ])
    data, _, _ = parse_master(markdown, {}, "test.md")
    assert data.projects[0].id == "brand_new_thing"
    assert any("is new" in p for p in data.problems)
    assert re.search(r"Database framing", " ".join(data.problems))
