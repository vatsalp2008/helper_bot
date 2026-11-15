from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from helper.cli import app
from helper.config import Paths, load_config
from helper.convert import run_convert
from helper.latex import find_binary
from helper.master import load_master
from helper.models import Header, MasterData, Profile, ResumeMetaFile, TailorOutput
from helper.pdfimage import Gray, diff, read_pgm, side_by_side
from helper.render import (
    RenderError,
    ResumeContent,
    build_content,
    bullet,
    display_phone,
    drop_leading_comment_lines,
    load_content,
    render_tex,
    resume_basename,
    tex,
    tex_differences,
    url,
)
from helper.templatecheck import PIXEL_THRESHOLD, run_template_check

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEMPLATE = REPO / "templates" / "resume.tex.j2"
BACKEND_SAMPLE = FIXTURES / "tailor_output_backend_sample.json"

needs_tex = pytest.mark.skipif(
    find_binary("pdflatex") is None or find_binary("pdftoppm") is None, reason="needs pdflatex and pdftoppm"
)


# --------------------------------------------------------------------------- filters


@pytest.mark.parametrize(
    "text,expected",
    [
        ("R&D & 50% of $45K", r"R\&D \& 50\% of \$45K"),
        ("C# and F_1 {x}", r"C\# and F\_1 \{x\}"),
        ("~800ms to ~40ms", r"$\sim$800ms to $\sim$40ms"),
        ("p<0.01, n>5, a|b", r"p\textless{}0.01, n\textgreater{}5, a\textbar{}b"),
        ("2^10 and back\\slash", r"2\textasciicircum{}10 and back\textbackslash{}slash"),
        ("R squared of 0.82", r"R\textsuperscript{2} of 0.82"),
        ("R² = 0.82, R-squared, R^2", r"R\textsuperscript{2} = 0.82, R\textsuperscript{2}, R\textsuperscript{2}"),
        ("Error Rate", "Error Rate"),
        ("45s→0.8s", "45s to 0.8s"),
        ("sequential scan → index scan", "sequential scan to index scan"),
        ("3k★ repo", "3k stars repo"),
        ("k-anonymity ≥5", "k-anonymity at least 5"),
        ("WeMakeDevs × Cognee, −0.4", "WeMakeDevs x Cognee, -0.4"),
        ("✅ Complete", "Complete"),
        ('the "merged" label', "the ``merged'' label"),
        ("Vatsal's 'draft' PRs", "Vatsal's `draft' PRs"),
        ("Résumé", "Résumé"),
        ("Jan. 2024 -- Apr. 2024", "Jan. 2024 -- Apr. 2024"),
        ("2024–2025", "2024--2025"),
    ],
)
def test_tex_escaping(text: str, expected: str) -> None:
    assert tex(text) == expected


@pytest.mark.parametrize("text", ["fast — reliable", "fast --- reliable", "a ― b"])
def test_em_dashes_are_rejected_everywhere(text: str) -> None:
    with pytest.raises(RenderError, match="em dash"):
        tex(text)
    with pytest.raises(RenderError, match="em dash"):
        bullet(text)


@pytest.mark.parametrize("text", ["Built it -- fast", "Built it – fast"])
def test_bullets_reject_en_dashes(text: str) -> None:
    with pytest.raises(RenderError, match="dash in bullet"):
        bullet(text)


def test_unknown_symbols_and_missing_values_are_rejected() -> None:
    with pytest.raises(RenderError, match="U\\+1F680"):
        tex("Shipped 🚀")
    with pytest.raises(RenderError, match="missing"):
        tex(None)


def test_bullet_bold() -> None:
    assert bullet("Wrote **scripts** that **automated** **8** workflows") == (
        r"Wrote \textbf{scripts} that \textbf{automated} \textbf{8} workflows"
    )
    assert bullet("reaching **85%** and **R squared of 0.82**") == (
        r"reaching \textbf{85\%} and \textbf{R\textsuperscript{2} of 0.82}"
    )
    assert bullet("from **~800ms** to ~40ms") == r"from \textbf{{\boldmath$\sim$}800ms} to $\sim$40ms"
    with pytest.raises(RenderError, match="unbalanced"):
        bullet("a **bold phrase")


def test_url_escapes_only_percent_and_hash() -> None:
    assert url("https://x.io/a_b~c?q=50%#top") == r"https://x.io/a_b~c?q=50\%\#top"


def test_display_phone_and_file_names() -> None:
    assert display_phone("(206) 555-0100") == "(206) 555--0100"
    assert display_phone("+1 206-555-0100") == "+1 206--555--0100"
    assert resume_basename("Vatsal Patel", "Jane Street", "software engineer intern") == (
        "VatsalPatel_Resume_JaneStreet_SoftwareEngineerIntern"
    )
    with pytest.raises(RenderError):
        resume_basename("Vatsal Patel", "  ", "SWE")


# --------------------------------------------------------------------------- the reference resume


def test_template_comments_are_dropped_before_jinja_reads_it() -> None:
    source, dropped = drop_leading_comment_lines(TEMPLATE.read_text(encoding="utf-8"))
    assert dropped == 8
    assert source.startswith(r"\documentclass")


def test_reference_content_reproduces_reference_tex() -> None:
    rendered = render_tex(load_content(REPO / "templates" / "reference_content.yaml"), TEMPLATE)
    reference = (REPO / "templates" / "reference_resume.tex").read_text(encoding="utf-8")
    assert tex_differences(rendered, reference) == []
    assert rendered.splitlines() == reference.splitlines()


def test_tex_differences_reports_changed_lines() -> None:
    reference = (REPO / "templates" / "reference_resume.tex").read_text(encoding="utf-8")
    changed = reference.replace("Seattle, WA 98105", "Boston, MA")
    found = tex_differences(changed, reference)
    assert any(line.startswith("+") and "Boston" in line for line in found)


def test_resume_content_rejects_gpa() -> None:
    data = yaml.safe_load((REPO / "templates" / "reference_content.yaml").read_text(encoding="utf-8"))
    data["gpa"] = "4.0"
    with pytest.raises(ValidationError, match="GPA"):
        ResumeContent.model_validate(data)


# --------------------------------------------------------------------------- content from master data


@pytest.fixture
def built(repo: Path, sample_profile_text: str) -> tuple[MasterData, ResumeMetaFile, Header, TailorOutput]:
    paths = Paths(repo)
    cfg = load_config(paths)
    run_convert(paths, cfg)
    meta = ResumeMetaFile.model_validate(yaml.safe_load(paths.resume_meta.read_text(encoding="utf-8")))
    header = Profile.model_validate(yaml.safe_load(sample_profile_text)).header
    tailor = TailorOutput.model_validate_json(BACKEND_SAMPLE.read_text(encoding="utf-8"))
    return load_master(paths, cfg), meta, header, tailor


def test_build_content_takes_facts_from_master_and_meta(built) -> None:  # type: ignore[no-untyped-def]
    master, meta, header, tailor = built
    content = build_content(tailor, master, meta, header)
    ta, kalini = content.experience
    assert (ta.org, ta.location, ta.dates) == (
        "Khoury College of Computer Sciences, Northeastern University", "Seattle, WA", "Jan. 2026 -- May 2026",
    )
    assert (kalini.org, kalini.location, kalini.dates) == ("Kalini Industries", "Mumbai, India", "Jan. 2024 -- Apr. 2024")
    spice = content.projects[2]
    assert spice.title == "Spice.ai: Open Source Rust Query Engine"
    assert (spice.url, spice.dates) == ("https://github.com/spiceai/spiceai", "Jul. 2026 -- Present")
    assert content.grad_date_display == "May 2027"
    assert content.header.phone == display_phone(header.phone)
    december = build_content(tailor.model_copy(update={"grad_date": "December 2027"}), master, meta, header)
    assert december.grad_date_display == "Dec. 2027"


def test_build_content_errors(built) -> None:  # type: ignore[no-untyped-def]
    master, meta, header, tailor = built

    def with_project(project_id: str) -> TailorOutput:
        return tailor.model_copy(update={"projects": [tailor.projects[0].model_copy(update={"project_id": project_id})]})

    with pytest.raises(RenderError, match="no project 'nope'"):
        build_content(with_project("nope"), master, meta, header)
    with pytest.raises(RenderError, match="experience section"):
        build_content(with_project("ta"), master, meta, header)
    no_location = tailor.model_copy(
        update={"experience": [tailor.experience[0].model_copy(update={"entry_id": "event_assistant"})]}
    )
    with pytest.raises(RenderError, match="no location"):
        build_content(no_location, master, meta, header)


# --------------------------------------------------------------------------- images


def test_pgm_png_and_pixel_diff(tmp_path: Path) -> None:
    pgm = tmp_path / "a.pgm"
    pgm.write_bytes(b"P5\n# made by a test\n4 3\n255\n" + bytes(range(0, 240, 20)))
    a = read_pgm(pgm)
    assert (a.width, a.height, a.row(1)) == (4, 3, bytes([80, 100, 120, 140]))
    b = Gray(4, 3, a.pixels[:5] + b"\xff" + a.pixels[6:])
    one = diff(a, b)
    assert (one.differing, one.total) == (1, 12)
    wider = Gray(5, 3, b"".join(a.row(y) + b"\x00" for y in range(3)))
    assert diff(a, wider).differing == 3  # the extra column counts as different
    png = side_by_side([a, b], tmp_path / "x.png", diff_of=(0, 1), gap=2)
    data = png.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (3 * 4 + 2 * 2, 3)
    idat_len = struct.unpack(">I", data[33:37])[0]
    raw = zlib.decompress(data[41 : 41 + idat_len])
    assert len(raw) == height * (1 + width * 3)


# --------------------------------------------------------------------------- compiled (needs TeX and poppler)


@needs_tex
def test_template_check_matches_the_reference_and_renders_a_sample(repo: Path, sample_profile_text: str) -> None:
    (repo / "data" / "profile.yaml").write_text(sample_profile_text, encoding="utf-8")
    paths = Paths(repo)
    cfg = load_config(paths)
    run_convert(paths, cfg)
    result = run_template_check(paths, cfg, [BACKEND_SAMPLE])
    assert result.differences == []
    assert result.compile.pages == 1 and not result.compile.overfull
    assert result.vs_local is not None and result.vs_local.differing == 0
    assert result.vs_provided is not None and result.vs_provided.ratio < PIXEL_THRESHOLD
    sample = result.samples[0]
    assert sample.passed, sample.overfull_lines()
    assert result.passed
    assert result.compare_png is not None and result.compare_png.is_file()


@needs_tex
def test_template_check_command(repo: Path) -> None:
    result = CliRunner().invoke(app, ["template-check"])
    assert result.exit_code == 0, result.output
    assert "identical to reference_resume.tex line for line" in result.output
    assert result.output.rstrip().endswith("PASS")
