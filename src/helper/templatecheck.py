"""`helper template-check`: show that resume.tex.j2 reproduces the reference resume (spec 6), and render samples."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from .config import Paths, load_yaml
from .errors import HelperError, problems
from .latex import CompileResult, OverfullBox, compile_tex, find_binary
from .master import load_master
from .models import Config, Profile, ResumeMetaFile, TailorOutput
from .pdfimage import PixelDiff, diff, page_count, rasterize, side_by_side
from .render import build_content, load_content, render_tex, tex_differences, write_tex

# Share of the page's pixels allowed to differ from reference_resume.pdf. Different TeX Live versions
# anti-alias a few glyph edges differently; a layout change moves whole lines and costs far more.
PIXEL_THRESHOLD = 0.005
DPI = 100


@dataclass
class SampleResult:
    source: Path
    tex: Path
    compile: CompileResult
    tex_lines: list[str]
    png: Optional[Path] = None

    @property
    def passed(self) -> bool:
        return self.compile.ok and self.compile.pages == 1 and not self.compile.overfull

    def overfull_lines(self) -> list[tuple[OverfullBox, str]]:
        return [(box, self.tex_lines[box.start_line - 1].strip() if 0 < box.start_line <= len(self.tex_lines) else "?")
                for box in self.compile.overfull]


@dataclass
class TemplateCheckResult:
    out_dir: Path
    differences: list[str]
    identical_lines: bool  # same lines; only the file's final newline may differ
    compile: CompileResult
    vs_provided: Optional[PixelDiff] = None
    vs_local: Optional[PixelDiff] = None
    provided_pages: Optional[int] = None
    compare_png: Optional[Path] = None
    samples: list[SampleResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        ok = not self.differences and self.compile.ok and self.compile.pages == 1
        for pixels in (self.vs_provided, self.vs_local):
            if pixels is not None and pixels.ratio > PIXEL_THRESHOLD:
                ok = False
        return ok and all(s.passed for s in self.samples)


def run_template_check(paths: Paths, cfg: Config, samples: Optional[list[Path]] = None) -> TemplateCheckResult:
    out_dir = paths.runs / "template-check"
    out_dir.mkdir(parents=True, exist_ok=True)
    content = load_content(paths.templates / "reference_content.yaml")
    rendered = render_tex(content, paths.resume_template)
    reference = paths.reference_tex.read_text(encoding="utf-8")
    tex_path = write_tex(rendered, out_dir / "reference_rendered.tex")
    result = TemplateCheckResult(
        out_dir=out_dir,
        differences=tex_differences(rendered, reference),
        identical_lines=rendered.splitlines() == reference.splitlines(),
        compile=compile_tex(tex_path, engine=cfg.latex_engine),
    )
    if not result.compile.ok:
        return result
    if find_binary("pdftoppm") is None:
        result.notes.append("pdftoppm not found, so the pixel comparison was skipped (install poppler)")
    else:
        result.provided_pages = page_count(paths.reference_pdf)
        provided = rasterize(paths.reference_pdf, out_dir / "reference_provided", DPI)
        mine = rasterize(result.compile.pdf, out_dir / "reference_rendered", DPI)  # type: ignore[arg-type]
        result.vs_provided = diff(mine, provided)
        # The original .tex compiled here separates TeX-version differences from template differences.
        local_tex = out_dir / "reference_local.tex"
        shutil.copy(paths.reference_tex, local_tex)
        local = compile_tex(local_tex, engine=cfg.latex_engine)
        if local.ok and local.pdf:
            result.vs_local = diff(mine, rasterize(local.pdf, out_dir / "reference_local", DPI))
        result.compare_png = side_by_side([provided, mine], out_dir / "compare.png", diff_of=(0, 1))
    for sample in samples or []:
        result.samples.append(render_sample(paths, cfg, sample, out_dir))
    return result


def render_sample(paths: Paths, cfg: Config, tailor_path: Path, out_dir: Path) -> SampleResult:
    """Render a tailor_output.json with real master data and the header from data/profile.yaml (no validation yet)."""
    try:
        tailor = TailorOutput.model_validate_json(tailor_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise HelperError(f"{tailor_path}: not a valid tailor_output.json:\n"
                          f"{problems(exc) if isinstance(exc, ValidationError) else exc}") from None
    master = load_master(paths, cfg)
    meta = ResumeMetaFile.model_validate(load_yaml(paths.resume_meta))
    try:
        profile = Profile.model_validate(load_yaml(paths.profile))
    except ValidationError as exc:
        raise HelperError(f"data/profile.yaml needs these filled in before helper can render a resume (see data/profile.example.yaml):\n{problems(exc)}") from None
    tex_source = render_tex(build_content(tailor, master, meta, profile.header), paths.resume_template)
    tex_path = write_tex(tex_source, out_dir / f"{tailor_path.stem}.tex")
    compiled = compile_tex(tex_path, engine=cfg.latex_engine)
    sample = SampleResult(tailor_path, tex_path, compiled, tex_source.splitlines())
    if compiled.ok and compiled.pdf and find_binary("pdftoppm"):
        image = rasterize(compiled.pdf, out_dir / f"{tailor_path.stem}_page", DPI)
        sample.png = side_by_side([image], out_dir / f"{tailor_path.stem}.png")
    return sample


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _pixels(d: PixelDiff) -> str:
    return f"{d.differing:,} of {d.total:,} pixels differ ({d.ratio:.2%})"


def format_report(result: TemplateCheckResult, paths: Paths) -> str:
    out = ["Template check (spec 6): reference_content.yaml rendered through resume.tex.j2", ""]
    if result.differences:
        out.append(f".tex: {len(result.differences)} lines differ from reference_resume.tex (whitespace ignored):")
        out.extend(f"    {line}" for line in result.differences[:40])
    else:
        same = "line for line" if result.identical_lines else "apart from whitespace"
        out.append(f".tex: identical to reference_resume.tex {same} (the template's leading comments are not output)")
    c = result.compile
    if not c.ok:
        out.append(f"PDF: did not compile: {'; '.join(c.errors[:3]) or 'see the .log'}")
    else:
        boxes = "no overfull boxes" if not c.overfull else f"{len(c.overfull)} overfull box(es)"
        out.append(f"PDF: {c.pages} page(s), {boxes}")
    if result.vs_provided is not None:
        out.append(f"Pixels vs your reference_resume.pdf: {_pixels(result.vs_provided)}")
    if result.vs_local is not None:
        out.append(f"Pixels vs reference_resume.tex compiled on this machine: {_pixels(result.vs_local)}")
        if result.vs_provided is not None and result.vs_provided.ratio > 0 and result.vs_local.differing == 0:
            out.append("  So the small difference comes from the TeX installation (glyph anti-aliasing), not the template.")
    if result.compare_png:
        out.append(f"Side by side: {_rel(result.compare_png, paths.root)}  (yours | rendered | differences in red)")
    out.extend(f"Note: {n}" for n in result.notes)
    for s in result.samples:
        out.append("")
        out.append(f"Sample: {_rel(s.source, paths.root)}")
        sc = s.compile
        if not sc.ok:
            out.append(f"  did not compile: {'; '.join(sc.errors[:3]) or 'see ' + _rel(s.tex.with_suffix('.log'), paths.root)}")
            continue
        out.append(f"  PDF: {_rel(sc.pdf, paths.root) if sc.pdf else '?'}: {sc.pages} page(s), "
                   + ("no overfull boxes" if not sc.overfull else f"{len(sc.overfull)} overfull box(es):"))
        for box, line in s.overfull_lines():
            out.append(f"    {box.too_wide_pt:.1f}pt too wide at .tex line {box.start_line}: {line[:110]}")
        if s.png:
            out.append(f"  Image: {_rel(s.png, paths.root)}")
    out.append("")
    out.append("PASS" if result.passed else "FAIL")
    return "\n".join(out)
