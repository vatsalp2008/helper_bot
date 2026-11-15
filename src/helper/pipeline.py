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

from .ats import detect_ats, job_id_from_url
from .config import Paths, load_yaml
from .eligibility import Eligibility, Flag, check_eligibility, format_eligibility, load_applications
from .errors import HelperError, NeedsUser, WaitingForClaude
from .keywords import Keywords, extract_keywords
from .latex import compile_tex
from .models import CapsFile, Config, Job, Profile, RunState, TailorOutput
from .render import build_content, render_tex, resume_basename, write_tex
from .runs import load_state, make_run_id, read_json, rel, run_dir, save_state, set_step, write_json
from .tailor import build_tailor_input
from .tracker.db import now_iso
from .validate import (
    KALINI,
    Context,
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
    eligibility: Eligibility

    @property
    def blocked(self) -> bool:
        return self.state.steps["eligibility"].status == "waiting_for_user"


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
    application_id: int


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
    if grad_date and grad_date not in (cfg.default_grad_date, cfg.alt_grad_date):
        raise HelperError(f"--grad-date must be '{cfg.default_grad_date}' or '{cfg.alt_grad_date}' (spec 8 item 4)")
    placeholder = RunOptions(projects_per_resume=projects or cfg.projects_per_resume,
                             bullets_per_item=cfg.bullets_per_item, grad_date=grad_date or cfg.default_grad_date)
    ctx = load_context(paths, cfg, placeholder)
    extra = list(dict.fromkeys(include_experience or []))
    for eid in extra:
        if eid not in {e.id for e in ctx.master.experience} or eid == "ta" or eid in KALINI:
            optional = sorted(e.id for e in ctx.master.experience if e.id != "ta" and e.id not in KALINI)
            raise HelperError(f"--include-experience takes one of {', '.join(optional)}")
        meta = ctx.meta.experience.get(eid)
        if not ((meta and meta.location) or ctx.master.item(eid).location):
            raise HelperError(f"{eid} has no location yet: set it in data/resume_meta.yaml first")

    base_id = make_run_id(company, role)
    run_id, n = base_id, 2
    while run_dir(paths, run_id).exists():  # two runs in the same second
        run_id, n = f"{base_id}-{n}", n + 1
    folder = run_dir(paths, run_id)
    folder.mkdir(parents=True)
    job = Job(url=url, ats=detect_ats(url) if url else "jd_file", company=company.strip(), role_title=role.strip(),
              location=location, description_text=jd_text, source="jd_file", fetched_at=now_iso())
    write_json(folder / "job.json", job.model_dump(mode="json"))

    keywords = extract_keywords(jd_text, ctx.master, ctx.vocab, ctx.project_sets, ctx.families)
    write_json(folder / "keywords.json", keywords.model_dump(mode="json"))

    eligibility = check_eligibility(
        job, cfg, caps=CapsFile.model_validate(load_yaml(paths.caps)), applications=load_applications(paths.db(cfg)),
        profile=_profile_or_none(paths), keywords=keywords,
    )
    grad = eligibility.grad_date
    if grad_date and grad_date != eligibility.grad_date:
        grad = grad_date
        eligibility.flags.append(Flag(id="grad_date_override", level="warning", message=(
            f"Graduation date set to {grad_date} by --grad-date; the check chose {eligibility.grad_date} "
            f"({eligibility.grad_date_reason}).")))
    write_json(folder / "eligibility.json", eligibility.model_dump(mode="json"))

    ctx.run = placeholder.model_copy(update={
        "grad_date": grad,
        "include_experience": extra,
        "require_open_source": bool(keywords.open_source_signals),
        "open_source_signals": keywords.open_source_signals,
    })
    state = RunState(run_id=run_id, created_at=now_iso(), command="resume", url=url, options={
        "run": ctx.run.model_dump(),
        "eligibility": {"recommendation": eligibility.recommendation, "grad_date": grad,
                        "flags": [f.model_dump() for f in eligibility.flags]},
    })
    set_step(state, "intake", "done", "job description from --jd-file")
    set_step(state, "keywords", "done")
    if eligibility.blocked:
        set_step(state, "eligibility", "waiting_for_user", "recommend skipping; ask Vatsal")
        save_state(paths, state)
        return Started(state, keywords, ctx.run, eligibility)
    set_step(state, "eligibility", "done", f"{len(eligibility.flags)} flag(s)")
    _write_tailor_input(paths, state, job, keywords, ctx)
    save_state(paths, state)
    return Started(state, keywords, ctx.run, eligibility)


def _profile_or_none(paths: Paths) -> Optional[Profile]:
    try:
        return Profile.model_validate(load_yaml(paths.profile))
    except (HelperError, ValidationError):
        return None


def _write_tailor_input(paths: Paths, state: RunState, job: Job, keywords: Keywords, ctx: Context) -> None:
    folder = run_dir(paths, state.run_id)
    write_json(folder / TAILOR_INPUT,
               build_tailor_input(state.run_id, job, keywords, ctx, rel(paths, folder / TAILOR_OUTPUT)))
    set_step(state, "tailor", "waiting_for_claude", f"write {TAILOR_OUTPUT}")


def blocked_message(state: RunState) -> NeedsUser:
    flags = state.options.get("eligibility", {}).get("flags", [])
    lines = ["Paused for Vatsal (exit 4): the eligibility check recommends skipping this posting."]
    lines += [f"  [blocker] {f['message']}" for f in flags if f["level"] == "blocker"]
    lines.append(f"To build anyway, run `helper continue {state.run_id} --build-anyway`. Otherwise leave it.")
    return NeedsUser("\n".join(lines))


def waiting(paths: Paths, run_id: str, note: str = "") -> WaitingForClaude:
    folder = run_dir(paths, run_id)
    return WaitingForClaude(run_id, rel(paths, folder / TAILOR_INPUT), rel(paths, folder / TAILOR_OUTPUT), note)


def continue_run(paths: Paths, cfg: Config, run_id: str, build_anyway: bool = False) -> Finished:
    state = load_state(paths, run_id)
    folder = run_dir(paths, run_id)
    if "run" not in state.options:
        raise HelperError(f"run {run_id} has no resume options; start it again with `helper resume`")
    if state.steps.get("eligibility") and state.steps["eligibility"].status == "waiting_for_user":
        if not build_anyway:
            raise blocked_message(state)
        set_step(state, "eligibility", "done", "built anyway at Vatsal's request")
        ctx = load_context(paths, cfg, RunOptions.model_validate(state.options["run"]))
        job = Job.model_validate(read_json(folder / "job.json"))
        keywords = Keywords.model_validate(read_json(folder / "keywords.json"))
        _write_tailor_input(paths, state, job, keywords, ctx)
        save_state(paths, state)
        raise waiting(paths, run_id, "Eligibility overridden; the resume will be built.")
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
    application_id = _record_application(paths, cfg, state, job, out, report, final_pdf, digest)
    state.application_id = application_id
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
                    role.label if role else out.role_type, dict(ctx.meta.project_display_names), application_id)


def _record_application(paths: Paths, cfg: Config, state: RunState, job: Job, out: TailorOutput, report: Report,
                        pdf: Path, digest: str) -> int:
    """The built resume becomes a drafted application in SQLite (local only until it is sent)."""
    from .tracker import repo
    from .tracker.commands import open_db

    flags = [f"{f['level']}: {f['message']}" for f in state.options.get("eligibility", {}).get("flags", [])
             if f["level"] != "note"]
    fields = {
        "company": job.company, "role_title": job.role_title, "location": job.location, "job_url": job.url,
        "ats": detect_ats(job.url) if job.url else None, "job_id": job_id_from_url(job.url),
        "resume_path": rel(paths, pdf), "resume_hash": digest, "projects_used": [p.project_id for p in out.projects],
        "keyword_match_pct": report.keyword_match_pct, "flags": flags, "run_id": state.run_id,
    }
    conn = open_db(paths, cfg)
    try:
        existing = repo.find_by_run(conn, state.run_id)
        target = existing["id"] if existing else _unsent_duplicate(paths, conn, state.run_id)
        if target is not None:
            repo.update(conn, target, **{k: v for k, v in fields.items() if v is not None})
        else:
            target = repo.insert(conn, status="drafted", source="helper", **fields)
        conn.commit()
        return int(target)
    finally:
        conn.close()


def _unsent_duplicate(paths: Paths, conn: object, run_id: str) -> Optional[int]:
    """An unsent row for the same posting (say a "To apply" row from Notion) is reused, not duplicated."""
    path = run_dir(paths, run_id) / "eligibility.json"
    if not path.is_file():
        return None
    for d in read_json(path).get("duplicates", []):
        a = d["application"]
        if d["kind"] == "exact" and a["status"] in ("drafted", "filled") and not a.get("run_id"):
            row = conn.execute("SELECT id FROM applications WHERE id = ? AND run_id IS NULL", (a["id"],)).fetchone()  # type: ignore[attr-defined]
            if row:
                return int(row["id"])
    return None


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
    out.append("")
    out.append(format_eligibility(started.eligibility))
    return "\n".join(out)


def format_finished(paths: Paths, done: Finished) -> str:
    """The pre-build message (spec 9 step 7): eligibility flags, fit, keyword audit, track and projects."""
    out, report = done.out, done.report
    lines = [f"Resume: {rel(paths, done.pdf)} ({done.pages} page, sha256 {done.sha256[:12]})",
             f"Tracked as application #{done.application_id} (drafted). After you apply, run "
             f"`helper mark {done.application_id} applied` to add it to Notion.", ""]
    elig = done.state.options.get("eligibility", {})
    shown = [f for f in elig.get("flags", []) if f["level"] != "note" or f["id"] == "grad_date"]
    lines.append("Eligibility flags:" if shown else "Eligibility flags: none.")
    order = {"blocker": 0, "warning": 1, "note": 2}
    lines += [f"  [{f['level']}] {f['message']}" for f in sorted(shown, key=lambda f: order[f["level"]])]
    if done.state.steps.get("eligibility") and "built anyway" in (done.state.steps["eligibility"].message or ""):
        lines.append("  Built anyway at your request despite the blocker above.")
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

