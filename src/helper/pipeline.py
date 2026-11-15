"""`helper resume` and `helper continue`: job description in, validated one-page PDF out (spec 9, steps 4 to 7).

Step 5 is a handoff: helper writes tailor_input.json and exits with code 3; Claude writes tailor_output.json; then
`helper continue <run_id>` validates it, renders it, and checks the PDF. A rejected output goes back to Claude
with the reasons, as many times as it takes (spec 7.7: do not continue until it passes).
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from .ats import detect_ats
from .config import Paths, load_yaml
from .errors import HelperError, WaitingForClaude
from .keywords import Keywords, extract_keywords
from .latex import compile_tex
from .models import Config, Job, Profile, RunState, TailorOutput
from .render import build_content, render_tex, resume_basename, write_tex
from .runs import load_state, make_run_id, read_json, rel, run_dir, save_state, set_step, write_json
from .tailor import build_tailor_input
from .tracker.db import now_iso
from .validate import (
    KALINI,
    Report,
    RunOptions,
    check_compiled,
    keyword_coverage_warnings,
    load_context,
    validate_tailor,
)

TAILOR_INPUT = "tailor_input.json"
TAILOR_OUTPUT = "tailor_output.json"
VALIDATION = "validation.json"


@dataclass
class Started:
    state: RunState
    keywords: Keywords
    run: RunOptions


@dataclass
class Finished:
    state: RunState
    job: Job
    out: TailorOutput
    report: Report
    pdf: Path
    tex: Path
    sha256: str
    pages: int
    role_label: str
    display_names: dict[str, str]


def start_resume(
    paths: Paths,
    cfg: Config,
    *,
    jd_text: str,
    company: str,
    role: str,
    url: Optional[str] = None,
    location: Optional[str] = None,
    projects: Optional[int] = None,
    include_experience: Optional[list[str]] = None,
    grad_date: Optional[str] = None,
) -> Started:
    if not jd_text.strip():
        raise HelperError("the job description is empty")
    if not company.strip() or not role.strip():
        raise HelperError("--company and --role are needed to name the run and the resume file")
    grad = grad_date or cfg.default_grad_date
    if grad not in (cfg.default_grad_date, cfg.alt_grad_date):
        raise HelperError(f"--grad-date must be '{cfg.default_grad_date}' or '{cfg.alt_grad_date}' (spec 8 item 4)")
    placeholder = RunOptions(projects_per_resume=projects or cfg.projects_per_resume,
                             bullets_per_item=cfg.bullets_per_item, grad_date=grad)
    ctx = load_context(paths, cfg, placeholder)
    extra = list(dict.fromkeys(include_experience or []))
    for eid in extra:
        if eid not in {e.id for e in ctx.master.experience} or eid == "ta" or eid in KALINI:
            optional = sorted(e.id for e in ctx.master.experience if e.id != "ta" and e.id not in KALINI)
            raise HelperError(f"--include-experience takes one of {', '.join(optional)}")
        meta = ctx.meta.experience.get(eid)
        if not ((meta and meta.location) or ctx.master.item(eid).location):
            raise HelperError(f"{eid} has no location yet: set it in data/resume_meta.yaml first")

    run_id = make_run_id(company, role)
    folder = run_dir(paths, run_id)
    folder.mkdir(parents=True, exist_ok=False)
    job = Job(url=url, ats=detect_ats(url) if url else "jd_file", company=company.strip(), role_title=role.strip(),
              location=location, description_text=jd_text, source="jd_file", fetched_at=now_iso())
    write_json(folder / "job.json", job.model_dump(mode="json"))

    keywords = extract_keywords(jd_text, ctx.master, ctx.vocab, ctx.project_sets, ctx.families)
    write_json(folder / "keywords.json", keywords.model_dump(mode="json"))
    ctx.run = placeholder.model_copy(update={
        "include_experience": extra,
        "require_open_source": bool(keywords.open_source_signals),
        "open_source_signals": keywords.open_source_signals,
    })
    write_json(folder / TAILOR_INPUT,
               build_tailor_input(run_id, job, keywords, ctx, rel(paths, folder / TAILOR_OUTPUT)))

    state = RunState(run_id=run_id, created_at=now_iso(), command="resume", url=url,
                     options={"run": ctx.run.model_dump()})
    set_step(state, "intake", "done", "job description from --jd-file")
    set_step(state, "eligibility", "skipped", "eligibility checks arrive in milestone 5")
    set_step(state, "keywords", "done")
    set_step(state, "tailor", "waiting_for_claude", f"write {TAILOR_OUTPUT}")
    save_state(paths, state)
    return Started(state, keywords, ctx.run)


def waiting(paths: Paths, run_id: str, note: str = "") -> WaitingForClaude:
    folder = run_dir(paths, run_id)
    return WaitingForClaude(run_id, rel(paths, folder / TAILOR_INPUT), rel(paths, folder / TAILOR_OUTPUT), note)


def continue_run(paths: Paths, cfg: Config, run_id: str) -> Finished:
    state = load_state(paths, run_id)
    folder = run_dir(paths, run_id)
    if "run" not in state.options:
        raise HelperError(f"run {run_id} has no resume options; start it again with `helper resume`")
    out_path = folder / TAILOR_OUTPUT
    if not out_path.is_file():
        raise waiting(paths, run_id, f"{TAILOR_OUTPUT} isn't there yet.")
    run = RunOptions.model_validate(state.options["run"])
    ctx = load_context(paths, cfg, run)
    job = Job.model_validate(read_json(folder / "job.json"))

    report = Report()
    try:
        out = TailorOutput.model_validate_json(out_path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        for err in exc.errors()[:10]:
            report.error("schema", ".".join(str(p) for p in err["loc"]) or TAILOR_OUTPUT, err["msg"])
        _reject(paths, state, report, "tailor_output.json doesn't match the schema in tailor_input.json")
        raise AssertionError("unreachable")  # pragma: no cover

    report = validate_tailor(out, ctx)
    keyword_coverage_warnings(out, read_json(folder / "keywords.json").get("tech", []), report)
    if not report.ok:
        _reject(paths, state, report, "tailor_output.json failed validation")

    try:
        profile = Profile.model_validate(load_yaml(paths.profile))
    except ValidationError as exc:
        raise HelperError(f"data/profile.yaml: the header must be filled in to render a resume: {exc}") from None
    content = build_content(out, ctx.master, ctx.meta, profile.header)
    tex_source = render_tex(content, paths.resume_template)
    base = resume_basename(profile.header.name, job.company, job.role_title)
    tex_path = write_tex(tex_source, folder / f"{base}.tex")
    compiled = compile_tex(tex_path, engine=cfg.latex_engine)
    check_compiled(compiled, tex_source, content, report)
    if not compiled.ok:
        write_json(folder / VALIDATION, report.to_json())
        raise HelperError(f"LaTeX failed on {rel(paths, tex_path)}: {'; '.join(compiled.errors[:3])}. "
                          f"See {rel(paths, tex_path.with_suffix('.log'))}.")
    if not report.ok:
        _reject(paths, state, report, "the rendered resume failed its checks")

    paths.resumes.mkdir(parents=True, exist_ok=True)
    final_pdf = paths.resumes / f"{base}.pdf"
    final_tex = paths.resumes / f"{base}.tex"
    shutil.copyfile(compiled.pdf, final_pdf)  # type: ignore[arg-type]
    shutil.copyfile(tex_path, final_tex)
    digest = hashlib.sha256(final_pdf.read_bytes()).hexdigest()
    write_json(folder / VALIDATION, report.to_json())
    state.options["result"] = {
        "pdf": rel(paths, final_pdf),
        "tex": rel(paths, final_tex),
        "sha256": digest,
        "pages": compiled.pages,
        "keyword_match_pct": report.keyword_match_pct,
        "projects_used": [p.project_id for p in out.projects],
        "experience_used": [e.entry_id for e in out.experience],
    }
    set_step(state, "tailor", "done")
    set_step(state, "validate", "done", f"{len(report.warnings)} warning(s)")
    set_step(state, "render", "done", rel(paths, final_pdf))
    save_state(paths, state)
    role = ctx.project_sets.role_types.get(out.role_type)
    return Finished(state, job, out, report, final_pdf, final_tex, digest, compiled.pages or 0,
                    role.label if role else out.role_type, dict(ctx.meta.project_display_names))


def _reject(paths: Paths, state: RunState, report: Report, headline: str) -> None:
    folder = run_dir(paths, state.run_id)
    write_json(folder / VALIDATION, report.to_json())
    set_step(state, "tailor", "waiting_for_claude", f"{len(report.errors)} problem(s) to fix")
    save_state(paths, state)
    lines = [f"{headline}: {len(report.errors)} problem(s). Details: {rel(paths, folder / VALIDATION)}"]
    lines += [f"  {issue}" for issue in report.errors]
    if report.warnings:
        lines.append(f"Warnings ({len(report.warnings)}):")
        lines += [f"  {issue}" for issue in report.warnings]
    lines.append(f"Fix {TAILOR_OUTPUT} and run `helper continue {state.run_id}` again.")
    raise waiting(paths, state.run_id, "\n".join(lines))


# --------------------------------------------------------------------------- messages


def format_started(paths: Paths, started: Started) -> str:
    state, kw, run = started.state, started.keywords, started.run
    folder = run_dir(paths, state.run_id)
    by_status: dict[str, list[str]] = {}
    for term in kw.tech:
        by_status.setdefault(term.status, []).append(term.canonical)
    out = [f"Run {state.run_id}", f"  Folder: {rel(paths, folder)}"]
    labels = {"in_items": "In your projects or experience", "skills_only": "Only in your Skills section",
              "not_in_master": "Not in the master list (gaps)"}
    out.append(f"  Technologies the posting names: {len(kw.tech)}")
    for status, label in labels.items():
        if by_status.get(status):
            out.append(f"    {label}: {', '.join(by_status[status])}")
    swaps = [f"{t.canonical} via {', '.join(t.swaps)}" for t in kw.tech if t.swaps]
    if swaps:
        out.append(f"    Also possible by an approved swap: {'; '.join(swaps)}")
    if run.require_open_source:
        out.append(f"  Open source signal ({', '.join(run.open_source_signals)}): chef or spiceai must be on the resume")
    top = ", ".join(f"{r.label} ({r.score})" for r in kw.role_type_scores[:3])
    out.append(f"  Closest role types by keyword overlap: {top}")
    out.append(f"  Resume: {run.projects_per_resume} projects, {run.bullets_per_item} bullets each, graduation "
               f"{run.grad_date}")
    out.append("  Eligibility checks arrive in milestone 5; this run has none.")
    return "\n".join(out)


def format_finished(paths: Paths, done: Finished) -> str:
    """The pre-build message (spec 9 step 7): eligibility flags, fit, keyword audit, track and projects."""
    out, report = done.out, done.report
    lines = [f"Resume: {rel(paths, done.pdf)} ({done.pages} page, sha256 {done.sha256[:12]})", ""]
    lines.append("Eligibility flags: none checked yet (milestone 5).")
    if out.fit_paragraph:
        lines += ["", f"Fit: {out.fit_paragraph}"]
    audit = out.keyword_audit
    lines.append("")
    lines.append(f"Matched verbatim: {', '.join(audit.matched) if audit.matched else 'none'}")
    if audit.skills_only:
        lines.append(f"In Technical Skills only: {', '.join(audit.skills_only)}")
    lines.append(f"Genuine gaps not forced in: {', '.join(audit.gaps)}" if audit.gaps else "No meaningful gaps.")
    if report.keyword_match_pct is not None:
        lines.append(f"Keyword match: {report.keyword_match_pct:g}%")
    track = done.role_label
    kalini = next((e.title for e in out.experience if e.entry_id in KALINI), "?")
    lines += ["", f"Track: {track}; Kalini entry: {kalini}"]
    names = done.display_names
    for n, p in enumerate(out.projects, 1):
        title = f"{names.get(p.project_id, p.project_id)}: {p.subtitle}"
        lines.append(f"  {n}. {title}" + (f". {p.reason}" if p.reason else ""))
    other = [f for f in out.flags if f.strip()]
    if other:
        lines += ["", "Flags:"] + [f"  - {f}" for f in other]
    if report.warnings:
        lines += ["", f"Warnings ({len(report.warnings)}):"] + [f"  {w}" for w in report.warnings]
    return "\n".join(lines)

