"""The validator (spec 7.7 plus the plan's extra checks): one failing and one passing case per rule."""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml
from pydantic import ValidationError

from helper.convert import parse_master, vocabulary_for
from helper.latex import CompileResult, OverfullBox, compile_tex, find_binary
from helper.models import (
    CoursesFile,
    Header,
    ProjectSetsFile,
    ResumeMetaFile,
    SkillSynonym,
    SkillSynonymsFile,
    TailorOutput,
    TechFamiliesFile,
    TechFamily,
)
from helper.render import build_content, render_tex, write_tex
from helper.validate import Context, Report, RunOptions, check_compiled, in_text, number_key, validate_tailor
from helper.numbers import find_numbers

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
BACKEND = json.loads((FIXTURES / "tailor_output_backend_sample.json").read_text(encoding="utf-8"))
NORTHWIND = json.loads((FIXTURES / "tailor_output_northwind.json").read_text(encoding="utf-8"))


def _yaml(name: str) -> Any:
    return yaml.safe_load((REPO / "data" / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def base() -> Context:
    lexicon = _yaml("tech_lexicon.yaml")
    master, _, _ = parse_master((REPO / "data" / "master_list_v23.md").read_text(encoding="utf-8"), lexicon, "x.md")
    return Context(
        master=master,
        vocab=vocabulary_for(master, lexicon),
        meta=ResumeMetaFile.model_validate(_yaml("resume_meta.yaml")),
        courses=CoursesFile.model_validate(_yaml("courses.yaml")).courses,
        project_sets=ProjectSetsFile.model_validate(_yaml("project_sets.yaml")),
        # Test-local proposals, so these tests don't depend on what Vatsal approved in data/.
        synonyms=SkillSynonymsFile(synonyms=[
            SkillSynonym(term="Hypothesis Testing", evidence=["experimentiq: Welch's t-test"], approved=False),
            SkillSynonym(term="Debugging", evidence=["ta: Debugged complex C implementations"], approved=True),
        ]),
        families=TechFamiliesFile(),
        run=RunOptions(projects_per_resume=5, bullets_per_item=2, grad_date="May 2027"),
    )


def ctx_with(base: Context, **changes: Any) -> Context:
    return dataclasses.replace(base, _cache={}, **changes)


def report_for(ctx: Context, data: dict[str, Any]) -> Report:
    return validate_tailor(TailorOutput.model_validate(data), ctx)


def mutated(fn: Callable[[dict[str, Any]], Any], data: dict[str, Any] = BACKEND) -> dict[str, Any]:
    d = copy.deepcopy(data)
    fn(d)
    return d


def project(d: dict[str, Any], pid: str) -> dict[str, Any]:
    return next(p for p in d["projects"] if p["project_id"] == pid)


def set_bullet(pid: str, n: int, text: str) -> Callable[[dict[str, Any]], None]:
    def fn(d: dict[str, Any]) -> None:
        entries = d["projects"] + d["experience"]
        entry = next(e for e in entries if e.get("project_id", e.get("entry_id")) == pid)
        entry["bullets"][n] = text
    return fn


def messages(report: Report) -> list[str]:
    return [str(i) for i in report.errors]


# --------------------------------------------------------------------------- the good outputs pass


@pytest.mark.parametrize("data", [BACKEND, NORTHWIND], ids=["backend", "northwind"])
def test_valid_outputs_pass(base: Context, data: dict[str, Any]) -> None:
    ctx = ctx_with(base, run=base.run.model_copy(update={"require_open_source": True, "open_source_signals": ["debug"]}))
    report = report_for(ctx, data)
    assert report.ok, messages(report)
    assert not report.warnings, [str(w) for w in report.warnings]


# --------------------------------------------------------------------------- one failing case per rule

CASES: list[tuple[str, Callable[[dict[str, Any]], Any], str, str]] = [
    # structure (spec 7.7)
    ("project count", lambda d: d["projects"].pop(), "structure", "4 projects; exactly 5"),
    ("bullets per item", lambda d: d["projects"][0]["bullets"].pop(), "structure", "1 bullets; exactly 2"),
    ("unknown project", lambda d: d["projects"][0].update(project_id="nope"), "structure", "no such project"),
    ("duplicate project", lambda d: d["projects"][1].update(project_id="pulsetrader"), "structure", "more than once"),
    ("experience as project", lambda d: d["projects"][0].update(project_id="kalini_ml"), "structure",
     "experience entry, not a project"),
    ("three tags", lambda d: d["projects"][0].update(tech_tags=["Kafka", "Apache Flink"]), "structure", "2 tech tags"),
    ("subtitle", lambda d: d["projects"][0].update(subtitle=" "), "structure", "needs a subtitle"),
    ("role type", lambda d: d.update(role_type="astronaut"), "structure", "not in project_sets.yaml"),
    ("grad date", lambda d: d.update(grad_date="December 2027"), "grad_date", "this run uses May 2027"),
    # experience (spec 7.1)
    ("TA required", lambda d: d["experience"].pop(0), "experience", "always included"),
    ("one Kalini", lambda d: d["experience"].pop(1), "experience", "exactly one Kalini"),
    ("Kalini by track", lambda d: d["experience"][1].update(entry_id="kalini_ml", title="Machine Learning Intern"),
     "experience", "track uses kalini_sde"),
    ("TA title prefix", lambda d: d["experience"][0].update(title="Teaching Assistant"), "experience",
     "must start with 'Graduate Teaching Assistant'"),
    ("TA title exact", lambda d: d["experience"][0].update(title="Graduate Teaching Assistant, C"), "experience",
     "must be exactly"),
    ("job titles are facts", lambda d: d["experience"][1].update(title="Software Engineer Intern"), "experience",
     "job titles are facts"),
    ("off by default", lambda d: d["experience"].append(
        {"entry_id": "student_proctor", "title": "Student Proctor", "bullets": ["Proctored exams.", "Checked IDs."]}),
     "experience", "off resumes by default"),
    # numbers (rule 2, spec 7.4)
    ("invented number", set_bullet("pulsetrader", 0, "Built a **Kafka** pipeline processing **12,000 trade events/sec** "
                                   "across **8** symbols."), "numbers", "'12,000' is not a number"),
    ("precision dropped", set_bullet("experimentiq", 0, "Built an **A/B testing** platform over **10M** simulated events "
                                     "with **12** dbt models."), "numbers", "write 10M+"),
    ("wrong unit", set_bullet("experimentiq", 1, "Cut an assignment log query from **~800s to ~40s** with a composite "
                              "**PostgreSQL** index."), "numbers", "the master list has"),
    ("identifier", set_bullet("pulsetrader", 1, "Ran **3** Flink jobs on trade events at **p95 <50ms**."), "numbers",
     "'p95'"),
    # technologies (rule 1)
    ("tech in bullet", set_bullet("pulsetrader", 0, "Built a **Scala** pipeline processing **10,000 trade events/sec** "
                                  "across **8** symbols."), "tech", "Scala"),
    ("tech in tag", lambda d: d["projects"][0].update(tech_tags=["Kafka", "Apache Flink", "Spark"]), "tech",
     "Apache Spark"),
    ("unknown name", set_bullet("ecommerce_platform", 0, "Architected **9** microservices, built in **Java Spring Boot** "
                                "with one in **Go**, in Agile sprints."), "names", "'Agile'"),
    # wording rules (spec 5.1 and 7.4)
    ("spiceai implementation", set_bullet("spiceai", 1, "Implemented the Decimal128 overflow overhaul across **10** "
                                          "files."), "wording", "spiceai_12747_bug_report"),
    ("chef pending", set_bullet("chef", 0, "Fixed an integer umask bug open for **3.5 years**; PR pending re-approval."),
     "wording", "chef_pr_merged"),
    ("9 Java", set_bullet("ecommerce_platform", 0, "Built **9** Java microservices with choreography-based **Sagas**."),
     "wording", "ecommerce_not_9_java"),
    ("simulated", set_bullet("experimentiq", 0, "Built an **A/B testing** platform over **10M+** events with **12** dbt "
                             "models."), "wording", "experimentiq_simulated"),
    ("trade events", set_bullet("pulsetrader", 0, "Built a **Kafka** pipeline processing **10,000** events/sec across "
                                "**8** symbols."), "wording", "pulsetrader_trade_events"),
    # style
    ("bold", set_bullet("pulsetrader", 0, "**Built** a **Kafka** and **Apache Flink** pipeline with **10,000 trade "
                        "events/sec** across **8** symbols."), "bold", "5 bold phrases"),
    ("em dash", set_bullet("pulsetrader", 0, "Built a Kafka pipeline \u2014 10,000 trade events/sec across 8 symbols."),
     "dashes", "em dash"),
    ("dash in bullet", set_bullet("pulsetrader", 0, "Built a Kafka pipeline -- 10,000 trade events/sec across 8 "
                                  "symbols."), "dashes", "can't contain --"),
    ("em dash in subtitle", lambda d: d["projects"][0].update(subtitle="Streaming \u2014 Real Time"), "dashes", "em dash"),
    ("GPA", lambda d: d["skills_lines"][0]["items"].append("GPA 3.9/4.0"), "gpa", "GPA"),
    ("qualitative flag", lambda d: d.update(flags=[]), "qualitative", "has no number"),
    ("unprintable", set_bullet("pulsetrader", 0, "Built a **Kafka** pipeline \U0001F680 with **10,000 trade events/sec** "
                               "across **8** symbols."), "render", "U+1F680"),
    # skills lines
    ("three lines", lambda d: d["skills_lines"].pop(), "skills", "2 lines; exactly 3"),
    ("skill not in master", lambda d: d["skills_lines"][0]["items"].append("Scala"), "skills",
     "'Scala' is not in the master list"),
    ("unapproved synonym", lambda d: d["skills_lines"][2]["items"].append("Hypothesis Testing"), "skills",
     "proposed in skill_synonyms.yaml but not approved"),
    # coursework
    ("five courses", lambda d: d["coursework"].pop(), "coursework", "4 courses; exactly 5"),
    ("course not taken", lambda d: d["coursework"].__setitem__(0, "Quantum Computing"), "coursework",
     "not in courses.yaml"),
    # keyword audit (spec 7.5)
    ("matched not shown", lambda d: d["keyword_audit"].update(matched=["Scala"]), "audit", "not on the resume verbatim"),
    ("gap on resume", lambda d: d["keyword_audit"].update(gaps=["Kafka"]), "audit", "listed as a gap but is on"),
    ("gap in master", lambda d: d["keyword_audit"].update(gaps=["Kubernetes"]), "audit", "is in the master list"),
    ("skills_only not in skills", lambda d: d["keyword_audit"].update(skills_only=["Scala"]), "audit",
     "not in a skills line"),
]


@pytest.mark.parametrize("name,mutate,check,needle", CASES, ids=[c[0] for c in CASES])
def test_each_rule_rejects(base: Context, name: str, mutate: Callable[[dict[str, Any]], Any], check: str,
                           needle: str) -> None:
    report = report_for(base, mutated(mutate))
    hits = [i for i in report.errors if i.check == check and needle in i.message]
    assert hits, f"{name}: expected [{check}] '{needle}' in {messages(report)}"


def test_summary_section_and_gpa_fields_are_refused_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        TailorOutput.model_validate(mutated(lambda d: d.update(summary="Motivated engineer")))
    with pytest.raises(ValidationError, match="GPA"):
        TailorOutput.model_validate(mutated(lambda d: d.update(gpa="3.9")))


def test_open_source_project_required_when_the_posting_values_it(base: Context) -> None:
    run = base.run.model_copy(update={"require_open_source": True, "open_source_signals": ["code reviews"]})

    def drop_open_source(d: dict[str, Any]) -> None:
        d["projects"] = [p for p in d["projects"] if p["project_id"] not in ("chef", "spiceai")]

    report = report_for(ctx_with(base, run=run), mutated(drop_open_source))
    assert any(i.check == "projects" and "include chef or spiceai" in i.message for i in report.errors)


def test_requested_experience_must_be_present(base: Context) -> None:
    run = base.run.model_copy(update={"include_experience": ["student_proctor"]})
    report = report_for(ctx_with(base, run=run), BACKEND)
    assert any("requested with --include-experience" in i.message for i in report.errors)


def test_approved_synonyms_are_allowed_in_skills_lines(base: Context) -> None:
    data = mutated(lambda d: d["skills_lines"][2]["items"].append("Debugging"))
    assert report_for(base, data).ok, messages(report_for(base, data))


def test_real_proposal_files_load(base: Context) -> None:
    SkillSynonymsFile.model_validate(_yaml("skill_synonyms.yaml"))
    TechFamiliesFile.model_validate(_yaml("tech_families.yaml"))


def test_swaps_apply_to_projects_only(base: Context) -> None:
    families = TechFamiliesFile(families=[TechFamily(id="web", members=["Flask", "FastAPI"], approved=True)])
    data = mutated(set_bullet("kalini_sde", 0, "Built a full-stack dashboard with **React** and a **FastAPI** service "
                              "serving **5** production lines at sub-second latency."))
    errors = report_for(ctx_with(base, families=families), data).errors
    assert any(i.check == "tech" and "FastAPI" in i.message and "kalini_sde" in i.where for i in errors)


def test_family_swaps_need_approval_and_replace_rather_than_add(base: Context) -> None:
    swapped = mutated(lambda d: project(d, "chef").update(tech_tags=["Ruby", "RSpec", "GitHub Actions"]))
    assert any(i.check == "tech" and "GitHub Actions" in i.message for i in report_for(base, swapped).errors)
    approved = TechFamiliesFile(families=[TechFamily(id="ci", members=["GitHub Actions", "Buildkite"], approved=True)])
    ctx = ctx_with(base, families=approved)
    assert report_for(ctx, swapped).ok, messages(report_for(ctx, swapped))
    both = mutated(lambda d: project(d, "chef").update(
        tech_tags=["Ruby", "GitHub Actions", "Buildkite"]))
    assert any("stands in for Buildkite" in i.message for i in report_for(ctx, both).errors)


def test_entry_rules_cover_the_whole_entry(base: Context) -> None:
    swarm = {
        "project_id": "swarm",
        "subtitle": "Multi-Agent Orchestration",
        "tech_tags": ["TypeScript", "MySQL", "LangGraph.js"],
        "bullets": [
            "Built a **7-package** monorepo with a LangGraph.js orchestrator, keeping each **12-agent** task under **<$5**.",
            "Coordinated agents over Postgres LISTEN/NOTIFY, logging **18** audit event types.",
        ],
    }
    data = mutated(lambda d: d["projects"].__setitem__(3, swarm))
    errors = report_for(base, data).errors
    assert any(i.check == "wording" and "db_framing_mysql" in i.message for i in errors), [str(e) for e in errors]


def test_numbers_compare_value_unit_marker_and_sign() -> None:
    def key(text: str) -> Any:
        return number_key(find_numbers(text)[0])

    assert key("~800ms").unit == "ms" and key("~800ms").marker == "~"
    assert key("$45K").unit == "$" and key("$45K").value == 45000
    assert key("10M+").marker == "+" and key("10M+").value == 10_000_000
    assert key("−0.4").sign == "-" and key("+1.3").sign == "+"
    assert key("20x").unit == key("20X").unit == "x"


def test_phrase_matching() -> None:
    assert in_text("Time Series Forecasting", "Time-series forecasting platform")
    assert in_text("REST APIs", "a Flask REST API")
    assert in_text("Sagas", "choreography-based Saga pattern")
    assert in_text("CEO's", "the CEO filed")
    assert not in_text("Go", "Google Cloud")
    assert not in_text("Scala", "scalable")


# --------------------------------------------------------------------------- the compiled PDF


def test_compiled_checks_pages_and_heading_overflow(tmp_path: Path) -> None:
    tex = "\\documentclass{article}\n\\resumeProjectHeading{\\href{u}{\\textbf{PulseTrader: Pipeline}}}{Jun. 2026}\n"
    result = CompileResult(ok=True, pdf=tmp_path / "missing.pdf", log="", pages=2,
                           overfull=[OverfullBox(12.5, 2, 2, "alignment"), OverfullBox(1.0, 1, 1, "paragraph")])
    content = build_content(TailorOutput.model_validate(BACKEND), _master(), _meta(), _header())
    report = Report()
    check_compiled(result, tex, content, report)
    found = messages(report)
    assert any("[pages]" in m and "2 pages" in m for m in found)
    assert any("[overfull] PulseTrader: Pipeline" in m for m in found)
    assert any(w.check == "overfull" for w in report.warnings)  # outside a heading: a warning


@pytest.mark.skipif(find_binary("pdflatex") is None or find_binary("pdftotext") is None, reason="needs TeX and poppler")
def test_bullets_longer_than_two_lines_are_rejected(tmp_path: Path) -> None:
    long = mutated(set_bullet("pulsetrader", 0, (
        "Built a **Kafka** and **Apache Flink** pipeline processing **10,000 trade events/sec** across **8** symbols, "
        "with Avro schema evolution through Confluent Schema Registry, exactly-once semantics, watermark-based "
        "out-of-order handling, dead letter queues, and a 10s anomaly suppression window feeding a React dashboard "
        "over FastAPI and WebSocket streams.")))
    content = build_content(TailorOutput.model_validate(long), _master(), _meta(), _header())
    source = render_tex(content, REPO / "templates" / "resume.tex.j2")
    result = compile_tex(write_tex(source, tmp_path / "long.tex"))
    report = Report()
    check_compiled(result, source, content, report)
    assert any(i.check == "lines" and "PulseTrader bullet 1" in i.where and "3 lines" in i.message
               for i in report.errors), messages(report)


def _master() -> Any:
    lexicon = _yaml("tech_lexicon.yaml")
    return parse_master((REPO / "data" / "master_list_v23.md").read_text(encoding="utf-8"), lexicon, "x.md")[0]


def _meta() -> ResumeMetaFile:
    return ResumeMetaFile.model_validate(_yaml("resume_meta.yaml"))


def _header() -> Header:
    profile = yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8"))
    return Header.model_validate(profile["header"])
