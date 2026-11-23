"""Cover letters: one with every resume, checked like the resume, rendered with Vatsal's template."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from helper.cli import app
from helper.config import Paths, load_config
from helper.convert import run_convert
from helper.cover_letter import (cover_letter_step, is_northeastern_campus, plain_letter, prose_numbers, salutation,
                                 validate_letter, word_count)
from helper.errors import EXIT_WAITING_FOR_CLAUDE, WaitingForClaude
from helper.latex import find_binary
from helper.models import CoverLetterOutput, Job, Profile, TailorOutput
from helper.validate import RunOptions, load_context

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LETTER = json.loads((FIXTURES / "cover_letter_northwind.json").read_text(encoding="utf-8"))
TAILOR = TailorOutput.model_validate_json((FIXTURES / "tailor_output_northwind.json").read_text(encoding="utf-8"))
JD = FIXTURES / "jd_backend_intern.txt"
runner = CliRunner()
needs_tex = pytest.mark.skipif(find_binary("pdflatex") is None, reason="needs pdflatex")


@pytest.fixture
def ready(repo: Path, sample_profile_text: str) -> Path:
    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    run_convert(Paths(repo), load_config(Paths(repo)))
    return repo


def job(company: str = "Northwind Labs") -> Job:
    return Job(company=company, role_title="Backend SWE Intern", description_text=JD.read_text(encoding="utf-8"),
               source="jd_file", fetched_at="now")


def check(ready: Path, paragraphs: list[str], **changes: Any) -> Any:
    paths = Paths(ready)
    ctx = load_context(paths, load_config(paths), RunOptions(projects_per_resume=5, bullets_per_item=2,
                                                             grad_date="May 2027"))
    items = [ctx.master.item(i) for i in [e.entry_id for e in TAILOR.experience] + [p.project_id for p in TAILOR.projects]]
    profile = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    out = CoverLetterOutput.model_validate({**LETTER, "paragraphs": paragraphs, **changes})
    return validate_letter(out, ctx, items, job(), TAILOR, profile)


def test_the_sample_letter_passes(ready: Path) -> None:
    report = check(ready, LETTER["paragraphs"])
    assert report.ok, [str(e) for e in report.errors]
    assert 400 <= sum(word_count(p) for p in LETTER["paragraphs"]) <= 450


P = LETTER["paragraphs"]


@pytest.mark.parametrize("paragraphs,changes,expected", [
    ([P[0].replace("habit:", "habit —"), *P[1:]], {}, "no em dashes"),
    ([P[0], P[1].replace("10,000 trade events", "12,000 trade events"), *P[2:]], {}, "'12,000' isn't a number"),
    ([P[0], P[1].replace("Terraform modules", "Terraform and Snowflake modules"), *P[2:]], {},
     "'Snowflake' (Snowflake) isn't a technology ecommerce_platform used"),
    ([P[0], P[1].replace("of **9 microservices**", "of **8 microservices**"), *P[2:]], {},
     "'8' isn't a number in ecommerce_platform"),
    ([P[0], P[1] + " I also built EchoCoach, a voice coach.", *P[2:]], {}, "isn't on this resume"),
    ([P[0], P[1], P[2] + " I also created supplementary materials for the course.", P[3]], {}, "never mentions creating"),
    ([P[0], P[1].replace("through **Kafka**", "through **Kafka** and Cassandra"), *P[2:]], {}, "is a gap"),
    ([P[0], P[1], P[2].replace("I merged a fix into chef/chef, PR #16260,", "I opened PR #16260 against chef/chef"),
      P[3]], {}, "always described as merged"),
    (P[:3], {}, "exactly 4"),
    ([*P[:3], "Thank you for your time."], {}, "close with"),
    (P, {"hook": "an embedded builder mindset"}, "isn't in the posting"),
    (P, {"stories": ["echocoach", "chef"]}, "isn't on this resume"),
    ([P[0][:200], P[1], P[2][:300], P[3]], {}, "words; write about 400 to 450"),
])
def test_the_letter_checks(ready: Path, paragraphs: list[str], changes: dict[str, Any], expected: str) -> None:
    report = check(ready, paragraphs, **changes)
    assert not report.ok and any(expected in str(e) for e in report.errors), [str(e) for e in report.errors]


def test_numbers_read_the_way_people_write_them() -> None:
    assert prose_numbers("a bug that sat for three and a half years") == "a bug that sat for 3.5 years"
    assert prose_numbers("p99 latency under 50ms") == "p99 latency <50ms"
    assert prose_numbers("more than 100 students and about 200 issues") == "100+ students and ~200 issues"
    assert prose_numbers("a pipeline over 5 sources") == "a pipeline over 5 sources"  # "over" here means "across"


def test_northeastern_campus_letters() -> None:
    campus = job("Northeastern University")
    assert is_northeastern_campus(campus) and not is_northeastern_campus(job())
    assert salutation(campus) == "Dear Career Team," and salutation(job()) == "Dear Hiring Team,"
    text = plain_letter("Dear Career Team,", ["One **bold** thing.", "Two."], "Alex Example", "000000000")
    assert text.endswith("Sincerely,\nAlex Example\nNUID: 000000000\n") and "**" not in text
    assert "NUID" not in plain_letter("Dear Hiring Team,", ["One."], "Alex Example", None)


@needs_tex
def test_every_resume_gets_a_cover_letter(ready: Path) -> None:
    started = runner.invoke(app, ["resume", "--jd-file", str(JD), "--company", "Northwind Labs",
                                  "--role", "Backend SWE Intern"])
    assert started.exit_code == EXIT_WAITING_FOR_CLAUDE, started.output
    (run_id,) = [p.name for p in (ready / "runs").iterdir()]
    folder = ready / "runs" / run_id
    data = json.loads((FIXTURES / "tailor_output_northwind.json").read_text(encoding="utf-8"))
    data["grad_date"] = json.loads((folder / "tailor_input.json").read_text(encoding="utf-8"))["run"]["grad_date"]
    (folder / "tailor_output.json").write_text(json.dumps(data), encoding="utf-8")
    built = runner.invoke(app, ["continue", run_id])
    assert built.exit_code == EXIT_WAITING_FOR_CLAUDE, built.output
    assert f"Resume: resumes/{run_id}/AlexExample_Resume.pdf" in built.output
    assert f"read runs/{run_id}/cover_letter_input.json and write runs/{run_id}/cover_letter_output.json" in built.output
    given = json.loads((folder / "cover_letter_input.json").read_text(encoding="utf-8"))
    assert [i["id"] for i in given["items"]] == ["ta", "kalini_sde", "pulsetrader", "ecommerce_platform", "spiceai",
                                                 "experimentiq", "chef"]
    assert given["salutation"] == "Dear Hiring Team," and given["resume"]["keyword_audit"]["gaps"] == ["Scala", "Cassandra"]
    assert any("100+ students" in rule for rule in given["rules"]) and len(given["structure"]) == 4

    bad = {**LETTER, "paragraphs": [P[0].replace("habit:", "habit —"), *P[1:]]}
    (folder / "cover_letter_output.json").write_text(json.dumps(bad), encoding="utf-8")
    rejected = runner.invoke(app, ["continue", run_id])
    assert rejected.exit_code == EXIT_WAITING_FOR_CLAUDE and "no em dashes" in rejected.output

    shutil.copy(FIXTURES / "cover_letter_northwind.json", folder / "cover_letter_output.json")
    done = runner.invoke(app, ["continue", run_id])
    assert done.exit_code == 0, done.output
    assert f"Cover letter: resumes/{run_id}/AlexExample_CoverLetter.pdf (418 words, 1 page)" in done.output
    pdf = ready / "resumes" / run_id / "AlexExample_CoverLetter.pdf"
    assert pdf.is_file() and (ready / "resumes" / run_id / "AlexExample_Resume.pdf").is_file()
    assert (folder / "cover_letter.txt").read_text(encoding="utf-8").startswith("Dear Hiring Team,\n\nI'm writing")
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    assert state["steps"]["cover_letter"]["status"] == "done" and state["options"]["cover_letter"]["words"] == 418

    assert "resume and cover letter are built" in runner.invoke(app, ["continue", run_id]).output
    assert "already built" in runner.invoke(app, ["cover-letter", run_id]).output
    redo = runner.invoke(app, ["cover-letter", run_id, "--redo"])
    assert redo.exit_code == EXIT_WAITING_FOR_CLAUDE and not (folder / "cover_letter_output.json").exists()


def test_no_letter_before_the_resume(ready: Path) -> None:
    started = runner.invoke(app, ["resume", "--jd-file", str(JD), "--company", "Northwind Labs",
                                  "--role", "Backend SWE Intern"])
    (run_id,) = [p.name for p in (ready / "runs").iterdir()]
    assert started.exit_code == EXIT_WAITING_FOR_CLAUDE
    assert cover_letter_step(Paths(ready), load_config(Paths(ready)), run_id) is None
    assert "has no resume yet" in runner.invoke(app, ["cover-letter", run_id]).output


def test_northeastern_is_never_the_nonprofit(ready: Path) -> None:
    report = check(ready, [P[0], P[1], P[2] + " At Northeastern, a nonprofit university, I learned a lot.", P[3]])
    assert any("never described as a nonprofit" in str(e) for e in report.errors)


def test_the_company_name_is_never_a_gap(ready: Path) -> None:  # ServiceNow, 2026-11-16
    paths = Paths(ready)
    ctx = load_context(paths, load_config(paths), RunOptions(projects_per_resume=5, bullets_per_item=2,
                                                             grad_date="May 2027"))
    items = [ctx.master.item(i) for i in [e.entry_id for e in TAILOR.experience] + [p.project_id for p in TAILOR.projects]]
    profile = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    tailor = TAILOR.model_copy(deep=True)
    tailor.keyword_audit.gaps.append("Cassandra Labs")  # the company's name also shows up as a technology gap
    letter = CoverLetterOutput.model_validate({**LETTER, "paragraphs": [P[0].replace("Northwind Labs", "Cassandra Labs"),
                                                                         *P[1:]]})
    report = validate_letter(letter, ctx, items, job("Cassandra Labs"), tailor, profile)
    assert not any("is a gap" in str(e) and "Cassandra Labs" in str(e) for e in report.errors), report.errors


def test_a_tool_the_resume_swapped_in_can_be_named_for_its_project(ready: Path) -> None:
    # The tailoring agents (2026-11-20): a project the resume framed with a same-family tool (data/tech_families.yaml,
    # all approved) was refused in the letter, "isn't a technology <project> used", though the resume shows it there
    paths = Paths(ready)
    ctx = load_context(paths, load_config(paths), RunOptions(projects_per_resume=5, bullets_per_item=2,
                                                             grad_date="May 2027"))
    items = [ctx.master.item(i) for i in [e.entry_id for e in TAILOR.experience] + [p.project_id for p in TAILOR.projects]]
    profile = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    tailor = TAILOR.model_copy(deep=True)
    project = next(p for p in tailor.projects if p.project_id == "experimentiq")
    project.tech_tags = ["Python", "Flask", "PostgreSQL"]
    project.bullets = [b.replace("**FastAPI**", "**Flask**") for b in project.bullets]

    def letter(tool: str) -> Any:
        return CoverLetterOutput.model_validate({**LETTER, "paragraphs": [
            P[0], P[1] + f" In ExperimentIQ, the assignment service ran on {tool}.", *P[2:]]})

    report = validate_letter(letter("Flask"), ctx, items, job(), tailor, profile)
    assert not any("'Flask'" in str(e) for e in report.errors), [str(e) for e in report.errors]
    # a tool that project's resume entry doesn't name is still refused, family or not
    report = validate_letter(letter("Django"), ctx, items, job(), tailor, profile)
    assert any("'Django' (Django) isn't a technology experimentiq used" in str(e) for e in report.errors), \
        [str(e) for e in report.errors]


def test_naming_the_role_is_not_claiming_its_words(ready: Path) -> None:  # Cymer, 2026-11-20
    paths = Paths(ready)
    ctx = load_context(paths, load_config(paths), RunOptions(projects_per_resume=5, bullets_per_item=2,
                                                             grad_date="May 2027"))
    items = [ctx.master.item(i) for i in [e.entry_id for e in TAILOR.experience] + [p.project_id for p in TAILOR.projects]]
    profile = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    tailor = TAILOR.model_copy(deep=True)
    tailor.keyword_audit.gaps.append("Contract Management")
    posting = job().model_copy(update={"role_title": "Internship - Contract Management and AI"})
    named = CoverLetterOutput.model_validate({**LETTER, "paragraphs": [
        "I'm writing to apply for the Internship, Contract Management and AI role. " + P[0], *P[1:]]})
    report = validate_letter(named, ctx, items, posting, tailor, profile)
    assert not any("is a gap" in str(e) for e in report.errors), report.errors
    assert not any("name the role" in str(w) for w in report.warnings), report.warnings
    # the same words used as a claim are still a gap
    claimed = CoverLetterOutput.model_validate({**LETTER, "paragraphs": [
        P[0], P[1] + " That taught me contract management too.", *P[2:]]})
    report = validate_letter(claimed, ctx, items, posting, tailor, profile)
    assert any("'Contract Management' is a gap" in str(e) for e in report.errors), report.errors


def test_a_title_joined_by_a_slash_can_be_named(ready: Path) -> None:  # Neros, 2026-11-21
    # "Firmware/Embedded Software Engineer Intern": the slash kept 'firmware' (a gap) in the text, so the letter had to
    # call it the "Embedded Software Engineer Intern" role and helper warned that it didn't name the role
    paths = Paths(ready)
    ctx = load_context(paths, load_config(paths), RunOptions(projects_per_resume=5, bullets_per_item=2,
                                                             grad_date="December 2027"))
    items = [ctx.master.item(i) for i in [e.entry_id for e in TAILOR.experience] + [p.project_id for p in TAILOR.projects]]
    profile = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    tailor = TAILOR.model_copy(deep=True)
    tailor.keyword_audit.gaps.append("Firmware")
    posting = job().model_copy(update={"role_title": "Firmware/Embedded Software Engineer Intern - Summer 2027"})
    named = CoverLetterOutput.model_validate({**LETTER, "paragraphs": [
        "I'm writing to apply for the Firmware/Embedded Software Engineer Intern role. " + P[0], *P[1:]]})
    report = validate_letter(named, ctx, items, posting, tailor, profile)
    assert not any("is a gap" in str(e) for e in report.errors), report.errors
    assert not any("name the role" in str(w) for w in report.warnings), report.warnings
    # the word used as a claim is still a gap
    claimed = CoverLetterOutput.model_validate({**LETTER, "paragraphs": [
        named.paragraphs[0], P[1] + " I have written firmware too.", *P[2:]]})
    report = validate_letter(claimed, ctx, items, posting, tailor, profile)
    assert any("'Firmware' is a gap" in str(e) for e in report.errors), report.errors


def test_the_run_s_graduation_date_can_be_named(ready: Path) -> None:  # Rockwell, 2026-11-23
    # "graduating in December 2027" had 'December' refused as a name not in the resume's items
    paths = Paths(ready)
    ctx = load_context(paths, load_config(paths), RunOptions(projects_per_resume=5, bullets_per_item=2,
                                                             grad_date="December 2027"))
    items = [ctx.master.item(i) for i in [e.entry_id for e in TAILOR.experience] + [p.project_id for p in TAILOR.projects]]
    profile = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    tailor = TAILOR.model_copy(deep=True)
    tailor.grad_date = "December 2027"
    letter = CoverLetterOutput.model_validate({**LETTER, "paragraphs": [
        *P[:3], P[3] + " I graduate in December 2027."]})
    report = validate_letter(letter, ctx, items, job(), tailor, profile)
    assert not any("'December'" in str(e) for e in report.errors), report.errors


def test_a_title_s_trailing_place_and_year_can_be_left_out(ready: Path) -> None:  # Citi, 2026-11-20
    paths = Paths(ready)
    ctx = load_context(paths, load_config(paths), RunOptions(projects_per_resume=5, bullets_per_item=2,
                                                             grad_date="May 2027"))
    items = [ctx.master.item(i) for i in [e.entry_id for e in TAILOR.experience] + [p.project_id for p in TAILOR.projects]]
    profile = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    posting = job().model_copy(update={
        "role_title": "Markets - Quantitative Analysis, Summer Analyst - New York City - US, 2027",
        "location": "New York New York United States"})

    def warned(first: str) -> bool:
        letter = CoverLetterOutput.model_validate({**LETTER, "paragraphs": [first, *P[1:]]})
        return any("name the role" in str(w) for w in validate_letter(letter, ctx, items, posting, TAILOR,
                                                                      profile).warnings)

    assert not warned("I'm applying for the Markets Quantitative Analysis Summer Analyst role in New York City. " + P[0])
    assert warned("I'm applying for the Markets Quantitative Analysis role. " + P[0])  # the role itself is cut short
    assert warned(P[0])


def test_the_posting_s_own_location_can_be_named(ready: Path) -> None:  # Sterling Jewelers, 2026-11-19
    paths = Paths(ready)
    ctx = load_context(paths, load_config(paths), RunOptions(projects_per_resume=5, bullets_per_item=2,
                                                             grad_date="May 2027"))
    items = [ctx.master.item(i) for i in [e.entry_id for e in TAILOR.experience] + [p.project_id for p in TAILOR.projects]]
    profile = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    letter = CoverLetterOutput.model_validate({**LETTER, "paragraphs": [P[0] + " I would gladly move to Akron for it.",
                                                                         *P[1:]]})
    located = job().model_copy(update={"location": "Support Center - Akron, OH"})
    assert not any("'Akron'" in str(e) for e in validate_letter(letter, ctx, items, located, TAILOR, profile).errors)
    # a place the posting never names is still an unknown name
    assert any("'Akron'" in str(e) for e in validate_letter(letter, ctx, items, job(), TAILOR, profile).errors)


def test_a_company_whose_name_is_also_a_tool_can_be_named(ready: Path) -> None:  # ServiceNow, 2026-11-17
    paths = Paths(ready)
    ctx = load_context(paths, load_config(paths), RunOptions(projects_per_resume=5, bullets_per_item=2,
                                                             grad_date="May 2027"))
    tool = next(n for n in ("Databricks", "Snowflake", "Kafka") if ctx.vocab.find(f"I like {n}"))
    items = [ctx.master.item(i) for i in [e.entry_id for e in TAILOR.experience] + [p.project_id for p in TAILOR.projects]]
    profile = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    letter = CoverLetterOutput.model_validate({**LETTER, "paragraphs": [P[0].replace("Northwind Labs", tool), *P[1:]]})
    report = validate_letter(letter, ctx, items, job(tool), TAILOR, profile)
    assert not any("isn't a technology" in str(e) and tool in str(e) for e in report.errors), report.errors


def test_quotes_that_end_a_sentence_and_postings_own_hyphens(ready: Path) -> None:  # CAS and Ralliant, 2026-11-20
    # A quote ending in "products." was blanked with its period, so the next sentence's "Keeping" read as a name; and
    # Ralliant's Oracle text writes "AI‑assisted" with U+2011, so the letter's "AI-assisted" read as an unknown name
    paths = Paths(ready)
    ctx = load_context(paths, load_config(paths), RunOptions(projects_per_resume=5, bullets_per_item=2,
                                                             grad_date="May 2027"))
    items = [ctx.master.item(i) for i in [e.entry_id for e in TAILOR.experience] + [p.project_id for p in TAILOR.projects]]
    profile = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
    base = job()
    posting = base.model_copy(update={"description_text": base.description_text + "\nInterns help enable content "
                                      "currency across our products and practice AI‑assisted development.\n"})
    first = (P[0] + ' Your team wants people who "enable content currency across our products." Keeping things '
             "correct is what I enjoy, and I already practice AI-assisted development.")
    letter = CoverLetterOutput.model_validate({**LETTER, "paragraphs": [first, *P[1:]],
                                               "hook": "AI‑assisted development"})
    errors = [str(e) for e in validate_letter(letter, ctx, items, posting, TAILOR, profile).errors]
    assert not any("'Keeping'" in e or "'AI-assisted'" in e or "hook" in e.lower() for e in errors), errors
    # a word the posting never uses is still an unknown name
    errors = [str(e) for e in validate_letter(letter.model_copy(update={"hook": None}), ctx, items, base, TAILOR,
                                              profile).errors]
    assert any("'AI-assisted'" in e for e in errors), errors
