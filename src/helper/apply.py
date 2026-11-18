"""`helper apply`: from a posting link to a filled form (spec 9 steps 2 and 8 to 11).

After the resume is built, form answers are planned (answers.py). Free-text and uncertain answers go to Claude through
questions_needed.json -> answers_provided.json, and Vatsal approves them (spec 10.4). `helper fill` then fills the form
in a headed browser, never clicks Submit, waits for Vatsal to submit, and logs the result.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from .adapters.common import asks_for_emailed_code, visible_errors
from .answers import NOTE_STYLE, AnswerContext, AnswerPlan, PlannedAnswer, format_plan, match_option, plan_answers
from .autosubmit import REVIEW_ONLY, Gate, blockers, clean_review_runs, record_fill_run, run_problems, site_kind
from .browser import browser_session, captcha_visible, make_pause
from .config import Paths, env_value, load_yaml
from .errors import HelperError, NeedsUser, WaitingForClaude, problems
from .models import AnswersFile, Config, Job, Profile
from .runs import load_state, read_json, rel, run_dir, save_state, set_step, write_json
from .validate import RunOptions, load_context

PLAN = "answers_plan.json"
NEEDED = "questions_needed.json"
PROVIDED = "answers_provided.json"
ANSWER_RULES = [
    "Use only facts from data/master.yaml (the master list) and the job description. No new claims.",
    "Short to medium unless the question says otherwise, specific to this role, natural and conversational, honest.",
    "No em dashes or en dashes.",
    "An 'about yourself' answer includes who Vatsal is personally, not only his work.",
    "When describing a project, give a brief background before the story.",
    "Respect every character or word limit (max_length) and report the count in a note.",
    "For a dropdown, the answer must be exactly one of its options.",
    "Leave out anything only Vatsal can answer; he will be asked.",
]


MULTI_PAGE = {"workday": "Workday", "icims": "iCIMS"}  # sites whose questions appear page by page


def fetch_job(url: str, jd_text: Optional[str] = None, purpose: str = "apply") -> Job:
    from .adapters import adapter_for
    from .ats import clean_url

    url = clean_url(url)
    job: Job = adapter_for(url, purpose).fetch_job(url)
    if job.apply_url and job.ats != "icims":  # a company page whose Apply button leads to iCIMS, Taleo...
        from .adapters import MANUAL, icims, manual_stop
        from .ats import detect_ats

        site = MANUAL.get(detect_ats(job.apply_url))
        if site and purpose == "apply":
            raise manual_stop(site, url)
        if icims.parse_ref(job.apply_url):  # Jibe pages apply on iCIMS: that's the form helper fills
            job = job.model_copy(update={"ats": "icims"})
    if jd_text:  # rule 12: a job description Vatsal gave is used as is
        job = job.model_copy(update={"description_text": jd_text, "description_html": None})
    return job


# --------------------------------------------------------------------------- answers (spec 10.3, 10.4)


def _context(paths: Paths, cfg: Config, run_id: str, job: Job) -> AnswerContext:
    state = load_state(paths, run_id)
    folder = run_dir(paths, run_id)
    eligibility = read_json(folder / "eligibility.json")
    run = RunOptions.model_validate(state.options["run"])
    try:
        profile = Profile.model_validate(load_yaml(paths.profile))
    except ValidationError as exc:
        raise HelperError(f"data/profile.yaml needs these filled in before helper can plan form answers (see data/profile.example.yaml):\n{problems(exc)}") from None
    master = load_context(paths, cfg, run).master
    return AnswerContext(
        profile=profile, cfg=cfg, job=job, grad_date=run.grad_date,
        employment=eligibility.get("employment", "unknown"), season=eligibility.get("season"),
        season_year=eligibility.get("season_year"), approved=AnswersFile.model_validate((load_yaml(paths.answers) if paths.answers.is_file() else None) or {}),
        past_employers=[e.organization for e in master.experience if e.organization],
        internships_done=sum(1 for e in master.experience if re.search(r"(?i)\bintern(ship)?\b", e.name or "")),
        projects_done=len(master.projects),
        documents={**documents(paths, profile), **_letter_documents(paths, state.options)},
        grades=grades(paths),
    )


def grades(paths: Paths) -> dict[str, str]:
    """GPA by school, for forms that ask (data/grades.yaml, git-ignored). Never on a resume (rule 3)."""
    path = paths.data / "grades.yaml"
    data = load_yaml(path) if path.is_file() else None
    return {str(k): str(v) for k, v in ((data or {}).get("gpa") or {}).items()}


def _letter_documents(paths: Paths, options: dict[str, Any]) -> dict[str, str]:
    """The run's cover letter, when one is built: the PDF for uploads and the plain text for text boxes."""
    letter = options.get("cover_letter") or {}
    found = {}
    if letter.get("pdf") and (paths.root / letter["pdf"]).is_file():
        found["cover_letter"] = str(paths.root / letter["pdf"])
    if letter.get("text") and (paths.root / letter["text"]).is_file():
        found["cover_letter_text"] = (paths.root / letter["text"]).read_text(encoding="utf-8").strip()
    return found


def documents(paths: Paths, profile: Profile) -> dict[str, str]:
    """The profile's documents that exist on disk, as absolute paths."""
    found = {}
    for name in ("transcript", "undergrad_transcript"):
        value = getattr(profile.documents, name)
        if value:
            path = Path(value).expanduser()
            path = path if path.is_absolute() else paths.root / path
            if path.is_file():
                found[name] = str(path)
    return found


def _needed(plan: AnswerPlan, job: Job, state_options: dict[str, Any], paths: Paths, run_id: str) -> dict[str, Any]:
    folder = run_dir(paths, run_id)
    return {
        "run_id": run_id,
        "write_to": rel(paths, folder / PROVIDED),
        "then_run": f"helper continue {run_id}",
        "format": {"answers": [{"field": "<field>", "answer": "<text, or one of the options>"}]},
        "rules": ANSWER_RULES,
        "job": {"company": job.company, "role_title": job.role_title, "description_text": job.description_text},
        "resume": state_options.get("result", {}),
        "questions": [
            {"field": a.field, "label": a.label, "type": a.type, "required": a.required, "options": a.options,
             "max_length": a.max_length, "description": a.description, "kind": a.kind, "suggested": a.value,
             "note": a.note, **({"style": NOTE_STYLE} if a.key == "hiring_manager_note" else {})}
            for a in plan.open_items()
        ],
    }


def _merge(plan: AnswerPlan, provided: dict[str, Any]) -> list[str]:
    """Apply Claude's answers. Every one of them still needs Vatsal's review."""
    problems = []
    open_fields = {a.field for a in plan.open_items()}
    for item in provided.get("answers", []):
        name, text = item.get("field"), (item.get("answer") or "").strip()
        a = plan.get(name or "")
        if a is None or name not in open_fields:
            problems.append(f"{name}: not a question that needed an answer")
            continue
        if not text:
            continue
        if "—" in text or "–" in text or "--" in text:
            problems.append(f"{name}: no em dashes or en dashes (rule 4)")
            continue
        if a.max_length and len(text) > a.max_length:
            problems.append(f"{name}: {len(text)} characters, the limit is {a.max_length}")
            continue
        if a.key == "hiring_manager_note":
            words = len(text.split())
            if not 40 <= words <= 110 or not text.startswith("Hi") or not text.rstrip().endswith("Vatsal Patel"):
                problems.append(f"{name}: a hiring-manager note opens with 'Hi,', runs about 60 to 90 words "
                                f"(this one has {words}), and ends 'Thank you,' then 'Vatsal Patel'")
                continue
        if a.options and a.type == "multi_select":  # "C#; Python; SQL" (Quest's tech stack, 2026-11-17)
            picks, bad = [], []
            for part in (p.strip() for p in text.split(";")):
                if not part:
                    continue
                best, score = match_option(part, a.options)
                if best is None or score < 0.95:
                    bad.append(part)
                elif best not in picks:
                    picks.append(best)
            if bad or not picks:
                problems.append(f"{name}: {', '.join(repr(b) for b in bad) or repr(text)} not among the options "
                                f"({', '.join(a.options)}); separate several choices with ';'")
                continue
            text = "; ".join(picks)
        elif a.options:
            best, score = match_option(text, a.options)
            if best is None or score < 0.95:
                problems.append(f"{name}: '{text}' is not one of the options ({', '.join(a.options)})")
                continue
            text = best
        a.value, a.kind, a.status, a.confidence = text, "auto", "needs_review", 1.0
        a.source = "Claude draft" if a.type in ("textarea", "text") and not a.options else "Claude (confirm)"
    return problems


def answers_step(paths: Paths, cfg: Config, run_id: str, approve: bool = False) -> str:
    state = load_state(paths, run_id)
    folder = run_dir(paths, run_id)
    job = Job.model_validate(read_json(folder / "job.json"))
    step = state.steps.get("answers")
    if step and step.status == "done":
        return f"Answers are approved. Fill the form with `helper fill {run_id}`."
    if job.ats in MULTI_PAGE and not job.questions:
        site = MULTI_PAGE[job.ats]
        write_json(folder / PLAN, AnswerPlan(answers=[]).model_dump(mode="json"))
        set_step(state, "answers", "done", f"{site}: questions are answered page by page during the fill")
        save_state(paths, state)
        stop = "on Workday's Review page" if job.ats == "workday" else "at the Submit button"
        return (f"{site} shows its questions page by page, so `helper fill {run_id}` answers them as it goes. Claude "
                f"drafts any written answers while it runs, and you review everything {stop} before you submit.")
    if not (folder / PLAN).is_file():
        plan = plan_answers(job.questions, _context(paths, cfg, run_id, job))
        write_json(folder / PLAN, plan.model_dump(mode="json"))
        if plan.open_items():
            write_json(folder / NEEDED, _needed(plan, job, state.options, paths, run_id))
            set_step(state, "answers", "waiting_for_claude", f"write {PROVIDED}")
            save_state(paths, state)
            raise WaitingForClaude(run_id, rel(paths, folder / NEEDED), rel(paths, folder / PROVIDED),
                                   "Form answers planned:\n" + format_plan(plan))
    plan = AnswerPlan.model_validate(read_json(folder / PLAN))
    step = state.steps.get("answers")
    if step and step.status == "waiting_for_claude":
        if not (folder / PROVIDED).is_file():
            raise WaitingForClaude(run_id, rel(paths, folder / NEEDED), rel(paths, folder / PROVIDED),
                                   f"{PROVIDED} isn't there yet.")
        problems = _merge(plan, read_json(folder / PROVIDED))
        if problems:
            raise WaitingForClaude(run_id, rel(paths, folder / NEEDED), rel(paths, folder / PROVIDED),
                                   "Fix these answers:\n" + "\n".join(f"  {p}" for p in problems))
        write_json(folder / PLAN, plan.model_dump(mode="json"))
        set_step(state, "answers", "waiting_for_user", "Vatsal reviews the drafted answers")
        save_state(paths, state)
    elif step and step.status == "waiting_for_user" and (folder / PROVIDED).is_file():
        # answers only Vatsal could give, added to answers_provided.json after the review pause
        still_open = {a.field for a in plan.answers if a.kind == "unknown"}
        late = [x for x in read_json(folder / PROVIDED).get("answers", []) if x.get("field") in still_open]
        if late:
            problems = _merge(plan, {"answers": late})
            if problems:
                raise WaitingForClaude(run_id, rel(paths, folder / NEEDED), rel(paths, folder / PROVIDED),
                                       "Fix these answers:\n" + "\n".join(f"  {p}" for p in problems))
            write_json(folder / PLAN, plan.model_dump(mode="json"))
    missing = [a for a in plan.answers if a.kind == "unknown" and a.required]
    review = plan.needs_review()
    if cfg.answer_review == "in_form" and not approve:  # drafts go into the form; Vatsal reviews them there
        saved = remember(paths, review)  # choice answers are reused on later postings
        if not cfg.drafts_need_review:  # Claude's drafts count as approved (Vatsal, 2026-11-15); helper's own unsure
            for a in review:            # matches still wait for him
                if a.source.startswith("Claude"):
                    a.status = "approved"
        write_json(folder / PLAN, plan.model_dump(mode="json"))
        set_step(state, "answers", "done", f"{len(review)} drafted answer(s) go into the form for review")
        save_state(paths, state)
        lines = [f"Answers ready. Fill the form with `helper fill {run_id}`."]
        if review:
            lines.append("Drafted answers that go into the form for you to check before Submit:")
            lines += [f"  {a.label}\n    -> {a.value}" for a in review]
        if missing:
            lines.append("Only you can answer these; they're left for you in the window:")
            lines += [f"  {a.label}{' (options: ' + ', '.join(a.options) + ')' if a.options else ''}" for a in missing]
        if saved:
            lines.append(f"Remembered {saved} choice answer(s) for later postings.")
        return "\n".join(lines)
    if (missing or review) and not approve:
        lines = ["Paused for Vatsal (exit 4): review the form answers below (spec 10.4)."]
        if review:
            lines.append("Drafted or suggested, needs your OK:")
            lines += [f"  {a.label}\n    -> {a.value}" for a in review]
        if missing:
            lines.append("Required, and only you can answer (tell Claude, who adds it to answers_provided.json):")
            lines += [f"  {a.label}{' (options: ' + ', '.join(a.options) + ')' if a.options else ''}"
                      f"{'  [' + a.note + ']' if a.note else ''}" for a in missing]
        lines.append(f"When they look right: `helper continue {run_id} --approve-answers`.")
        raise NeedsUser("\n".join(lines))
    if missing:
        raise NeedsUser("These required questions still have no answer:\n" + "\n".join(f"  {a.label}" for a in missing))
    for a in review:
        a.status = "approved"
    remember(paths, review)  # choice answers Vatsal approved are reused on later postings
    write_json(folder / PLAN, plan.model_dump(mode="json"))
    set_step(state, "answers", "done", f"{len(review)} answer(s) approved")
    save_state(paths, state)
    drafted = [a for a in review if a.source == "Claude draft"]
    save_hint = (f"\nTo reuse {'this answer' if len(drafted) == 1 else 'these answers'} later, ask Claude to save "
                 f"{'it' if len(drafted) == 1 else 'them'} to data/answers.yaml.") if drafted else ""
    return f"Answers approved. Fill the form with `helper fill {run_id}`.{save_hint}"


# --------------------------------------------------------------------------- filling (spec 10.1, 10.2, 9 steps 8-11)


def replan(paths: Paths, cfg: Config, run_id: str) -> list[str]:
    """Plan a single-page form's answers again with today's rules, for a refill after a rule was fixed (Scout,
    2026-11-18: the saved plan still said Master's for "highest level completed" and took a Yes/No confirmation for
    the resume upload). Claude's earlier answers stay wherever the new plan has nothing better than a question for
    Claude. Returns the changes, one line each."""
    folder = run_dir(paths, run_id)
    job = Job.model_validate(read_json(folder / "job.json"))
    if not job.questions or not (folder / PLAN).is_file():
        return []
    old = {a.field: a for a in AnswerPlan.model_validate(read_json(folder / PLAN)).answers}
    new = plan_answers(job.questions, _context(paths, cfg, run_id, job))
    changes: list[str] = []
    for i, a in enumerate(new.answers):
        before = old.get(a.field)
        if before is None:
            continue
        if before.source.startswith("Claude") and (a.kind in ("free_text", "unknown") or a.source.startswith("Claude")):
            new.answers[i] = before  # Claude's answer to a question the rules still can't answer
            continue
        if (a.value, a.kind) != (before.value, before.kind):
            changes.append(f"{a.label[:70]}: {before.value!r} -> {a.value!r}")
    if not cfg.drafts_need_review:  # Vatsal, 2026-11-15: Claude's drafts count as approved
        for a in new.answers:
            if a.status == "needs_review" and a.source.startswith("Claude"):
                a.status = "approved"
    write_json(folder / PLAN, new.model_dump(mode="json"))
    return changes


def rescan(paths: Paths, cfg: Config, run_id: str, scan: Optional[Any] = None) -> str:
    """Read a company-site form's questions again and plan its answers afresh, for a refill after helper learned to
    read that kind of form (Rippling, 2026-11-18: the first read missed its dropdowns and rating questions, so most of
    the form stayed empty). The old plan and answer files stay beside the new ones as *.before-rescan.json. Questions
    the rules can't answer go to Claude as in `helper continue` (WaitingForClaude)."""
    from .adapters import adapter_for, generic

    folder = run_dir(paths, run_id)
    job = Job.model_validate(read_json(folder / "job.json"))
    link = job.apply_url or job.url or ""
    if adapter_for(link) is not generic:
        raise HelperError(f"--rescan is for company-site forms; {job.ats} forms are read from the site's own data.")
    result = (scan or generic.scan_posting)(link)
    if not result.questions:
        raise HelperError("helper couldn't read the form's questions again (the posting may have closed, or the page "
                          "turned helper's hidden browser away). The old plan stays as it was.")
    job.questions = result.questions
    write_json(folder / "job.json", job.model_dump(mode="json"))
    for name in (PLAN, NEEDED, PROVIDED):
        if (folder / name).is_file():
            (folder / name).replace(folder / name.replace(".json", ".before-rescan.json"))
    state = load_state(paths, run_id)
    set_step(state, "answers", "pending", f"the form was read again: {len(result.questions)} question(s)")
    save_state(paths, state)
    return answers_step(paths, cfg, run_id)


@dataclass
class FillOutcome:
    status: str  # dry_run | filled | applied | submitted_unverified
    message: str
    results: list[Any] = field(default_factory=list)


_HUNT: dict[str, Any] = {}  # the fill under way, so a stop for Vatsal shows on the job hunt's list


def _say(text: str) -> None:
    print(text, flush=True)
    sys.stdout.flush()
    if _HUNT:
        from .hunt import on_fill_message

        on_fill_message(_HUNT["paths"], _HUNT["cfg"], _HUNT["run_id"], text)


def fill_run(paths: Paths, cfg: Config, run_id: str, *, dry_run: bool = False, wait_minutes: Optional[float] = None,
             headless: Optional[bool] = None, poll_seconds: float = 2.0) -> FillOutcome:
    from .adapters import adapter_for

    state = load_state(paths, run_id)
    folder = run_dir(paths, run_id)
    if state.command != "apply":
        raise HelperError(f"run {run_id} was a `helper resume` run; `helper apply <url>` starts one that fills a form")
    if not (state.steps.get("answers") and state.steps["answers"].status == "done"):
        raise HelperError(f"the form answers aren't approved yet: run `helper continue {run_id}`")
    job = Job.model_validate(read_json(folder / "job.json"))
    plan = AnswerPlan.model_validate(read_json(folder / PLAN))
    pdf = paths.root / state.options["result"]["pdf"]
    if dry_run:
        if job.ats == "workday":
            return FillOutcome("dry_run", f"Would fill {job.apply_url or job.url} page by page: the account step, "
                                          "My Information, My Experience (from the resume and your profile), the "
                                          "company's questions and the disclosures, stopping on the Review page.")
        if job.ats == "icims":
            return FillOutcome("dry_run", f"Would fill {job.apply_url or job.url} page by page: sign in or create the "
                                          "iCIMS account, then every application page, stopping at the Submit button.")
        return FillOutcome("dry_run", f"Would fill {job.apply_url or job.url} with:\n{format_plan(plan)}")
    if not pdf.is_file():
        raise HelperError(f"the resume PDF is missing: {rel(paths, pdf)}")
    adapter = adapter_for(job.apply_url if job.ats == "icims" and job.apply_url else job.url or job.apply_url or "")
    profile = Profile.model_validate(load_yaml(paths.profile))
    wait = cfg.fill_wait_timeout_minutes if wait_minutes is None else wait_minutes
    mode = state.options.get("submit_mode") or cfg.submit_mode
    _HUNT.update(paths=paths, cfg=cfg, run_id=run_id)
    from .hunt import on_fill_started

    on_fill_started(paths, cfg, run_id)
    if hasattr(adapter, "run_flow"):
        return _fill_flow(paths, cfg, state, job, pdf, adapter, profile, wait, headless, poll_seconds, mode)
    submit_seen = {"value": False}
    seen = {"captcha": False}
    with browser_session(paths, cfg, headless=headless) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.on("request", lambda req: submit_seen.update(value=True) if adapter.is_submit_request(req.url, req.method)
                else None)
        form = _open_form(page, adapter, job.apply_url or job.url, wait, poll_seconds, seen)
        if form is None:
            reason = getattr(adapter, "no_form_reason", None)
            _say(reason(page) if reason else "helper couldn't find the application form on this page, so it filled "
                 "nothing. Fill it in the browser window yourself; helper still watches for the confirmation and logs it.")
            results = []
        else:
            results = adapter.fill_form(form, plan.answers, pdf, country=profile.tier1.address.country,
                                        city=profile.tier1.address.city, pause=make_pause(cfg))
            if hasattr(adapter, "fill_employment"):  # the jobs on the resume, for forms with an Employment section
                results += adapter.fill_employment(form, lambda: _history(paths, cfg, state, folder, profile)[0],
                                                   make_pause(cfg))
        _snapshot(page, form, folder / "filled.png")
        write_json(folder / "fill_log.json", [vars(r) for r in results])
        failed_required = [r for r in results if not r.ok and _required(plan, r.field)]
        _record_status(paths, cfg, state, "filled")
        set_step(state, "fill", "done", f"{sum(r.ok for r in results)} field(s) filled")
        save_state(paths, state)
        submitted_by_helper = False
        errors_before: Optional[list[str]] = None
        if mode == "auto":
            reasons = _auto_blockers(paths, cfg, folder, job, plan, results, page, form, adapter, seen)
            if reasons:
                _say("Auto-submit is off for this run, so it's in review mode. Why:\n"
                     + "\n".join(f"  - {r}" for r in reasons))
            else:
                _say("Auto-submit: every condition holds (spec 10.5). Clicking Submit once; helper never retries.")
                errors_before = visible_errors(form) if form is not None else []
                submitted_by_helper = adapter.submit(form)
                if not submitted_by_helper:
                    _say("helper couldn't click Submit after all, so it's in review mode: submit it yourself.")
        if submitted_by_helper:
            _say(f"Filled {sum(r.ok for r in results)} of {len(results)} field(s); helper clicked Submit once.")
        else:
            _say(_fill_summary(results, failed_required, wait))
            drafted = [a for a in plan.needs_review() if a.value]
            yours = [a for a in plan.answers if a.kind == "unknown" and a.required]
            if drafted:
                _say("Claude drafted these; check them in the window:\n"
                     + "\n".join(f"  {a.label[:90]} -> {a.value[:120]}" for a in drafted))
            if yours:
                _say("Only you can answer these; fill them in the window:\n"
                     + "\n".join(f"  {a.label[:120]}" for a in yours))
        outcome = _wait_for_submit(page, adapter, submit_seen, wait, poll_seconds, form, seen, errors_before)
        shot = None
        if outcome in ("confirmed", "unverified"):
            shot = folder / "confirmation.png"
            if not _snapshot(page, form, shot):
                shot = None
    return _log_outcome(paths, cfg, state, outcome, shot, results, mode="auto" if submitted_by_helper else "review",
                        problems=run_problems(results, seen["captcha"]), ats=job.ats)


def _auto_blockers(paths: Paths, cfg: Config, folder: Path, job: Job, plan: AnswerPlan, results: list[Any], page: Any,
                   form: Optional[Any], adapter: Any, seen: dict[str, bool]) -> list[str]:
    """Spec 10.5: the reasons helper must not submit this run by itself (none means it may)."""
    from .tracker.commands import open_db

    if site_kind(job.ats) == "generic":  # Vatsal, 2026-11-16: company sites submit once everything is filled
        reasons = []
        empty = [a.label[:60] for a in plan.answers if a.required and a.kind in ("unknown", "skip")]
        empty += [r.label[:60] for r in results if not r.ok and _required(plan, r.field)]
        if empty:
            reasons.append("required fields still empty: " + "; ".join(dict.fromkeys(empty)))
        errors = visible_errors(form) if form is not None else ["helper couldn't find the form"]
        if errors:
            reasons.append("the form shows errors: " + "; ".join(e[:80] for e in errors[:3]))
        if form is None or len(adapter.submit_buttons(form)) != 1:
            turned_off = getattr(adapter, "disabled_submit_buttons", None)
            if form is not None and turned_off and not adapter.submit_buttons(form) and turned_off(form):
                reasons.append("the form's Submit button is still turned off, so the site wants something more (a "
                               "field it marks, or an upload still processing)")
            else:
                reasons.append("helper couldn't find exactly one Submit button")
        return reasons

    conn = open_db(paths, cfg)
    try:
        clean = clean_review_runs(conn, job.ats)
    finally:
        conn.close()
    eligibility = read_json(folder / "eligibility.json") if (folder / "eligibility.json").is_file() else {}
    gate = Gate(ats=job.ats, clean_runs=clean, min_clean_runs=cfg.auto_submit_min_clean_runs, answers=plan.answers,
                results=results, eligibility=eligibility, captcha_seen=seen["captcha"] or captcha_visible(page),
                page_errors=visible_errors(form) if form is not None else ["helper couldn't find the form"],
                has_submit_button=hasattr(adapter, "submit") and form is not None)
    return blockers(gate)


# --------------------------------------------------------------------------- multi-page flows (Workday)


def _fill_flow(paths: Paths, cfg: Config, state: Any, job: Job, pdf: Path, adapter: Any, profile: Profile, wait: float,
               headless: Optional[bool], poll_seconds: float, mode: str = "review") -> FillOutcome:
    """Fill a form that spans several pages. Questions are planned page by page; written answers are drafted by Claude
    while the window stays open; the flow ends on the site's Review page, and Vatsal submits."""
    folder = run_dir(paths, state.run_id)
    actx = _context(paths, cfg, state.run_id, job)
    work, schools = _history(paths, cfg, state, folder, profile)
    planned: list[PlannedAnswer] = []

    def plan_page(questions: list[Any]) -> list[PlannedAnswer]:
        answers = plan_answers(questions, actx).answers
        planned.extend(answers)
        write_json(folder / PLAN, AnswerPlan(answers=planned).model_dump(mode="json"))
        return answers

    def ask_claude(answers: list[PlannedAnswer], moved_on: Any) -> None:
        _ask_claude_midfill(paths, state, job, answers, moved_on, wait * 60, poll_seconds)
        if not cfg.drafts_need_review:  # Vatsal, 2026-11-15: Claude's drafts need no review
            for a in answers:
                if a.status == "needs_review" and a.source.startswith("Claude"):
                    a.status = "approved"
        write_json(folder / PLAN, AnswerPlan(answers=planned).model_dump(mode="json"))

    go_flag = folder / "go_on"

    def go_on() -> bool:
        if go_flag.exists():
            go_flag.unlink()
            return True
        return False

    submit_seen = {"value": False}
    pauses: list[str] = []

    def say(text: str) -> None:  # pauses for Vatsal keep the run from counting as clean (spec 10.5)
        if any(w in text for w in ("yourself", "didn't accept", "CAPTCHA", "Paused", "verification", "ran out")):
            pauses.append(text.splitlines()[0][:120])
        _say(text)

    if mode == "auto" and job.ats in REVIEW_ONLY:
        _say(f"Auto-submit is off for this run: {REVIEW_ONLY[job.ats]}.")
    with browser_session(paths, cfg, headless=headless) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.on("request", lambda req: submit_seen.update(value=True) if adapter.is_submit_request(req.url, req.method)
                else None)
        page.goto(job.apply_url or job.url, wait_until="domcontentloaded")
        if job.ats == "workday":
            from .adapters import workday_page

            workday_page.DEBUG_DIR = folder
        flow = adapter.FlowContext(
            company=job.company, plan=plan_page, ask_claude=ask_claude, resume_pdf=pdf, work=work, schools=schools,
            skills=_resume_skills(folder), certifications=_certifications(paths) if cfg.fill_certifications else [],
            websites=[profile.tier1.linkedin_url, profile.tier1.github_url, profile.tier1.portfolio_url],
            say=say, pause=make_pause(cfg), credentials=_credentials(paths, job.ats, job),
            has_account=job.ats == "workday" and _has_workday_account(paths, cfg, state, job), go_on=go_on,
            on_account=lambda kind: _note_account(paths, job),
            wait_seconds=wait * 60,
            poll_seconds=poll_seconds,
        )
        result = adapter.run_flow(page, flow)
        _snapshot(page, None, folder / "filled.png")
        write_json(folder / "fill_log.json", [vars(r) for r in result.results])
        _record_status(paths, cfg, state, "filled")
        set_step(state, "fill", "done", f"{sum(r.ok for r in result.results)} field(s) filled"
                                        + ("; stopped on the Review page" if result.reached_review else ""))
        save_state(paths, state)
        _say(_flow_summary(result, planned, wait))
        submitted_by_helper = False
        if mode == "auto" and job.ats not in REVIEW_ONLY:
            reasons = _flow_blockers(paths, cfg, folder, job, planned, result, page, adapter, pauses)
            if reasons:
                _say("Auto-submit is off for this run, so it waits on the Review page for you. Why:\n"
                     + "\n".join(f"  - {r}" for r in reasons))
            else:
                _say("Auto-submit: every condition holds. Clicking Submit once; helper never retries.")
                submitted_by_helper = bool(adapter.submit_review(page))
                if submitted_by_helper:
                    submit_seen["value"] = True
                else:
                    _say("helper couldn't click Submit after all: submit it yourself on the Review page.")
        outcome = _wait_for_submit(page, adapter, submit_seen, wait, poll_seconds, None)
        shot = None
        if outcome in ("confirmed", "unverified"):
            shot = folder / "confirmation.png"
            if not _snapshot(page, None, shot):
                shot = None
    return _log_outcome(paths, cfg, state, outcome, shot, result.results, ats=job.ats,
                        mode="auto" if submitted_by_helper else "review",
                        problems=run_problems(result.results, False, pauses + ([] if result.reached_review else
                                                                                ["stopped before the Review page"])))


def _flow_blockers(paths: Paths, cfg: Config, folder: Path, job: Job, planned: list[PlannedAnswer], result: Any,
                   page: Any, adapter: Any, pauses: list[str]) -> list[str]:
    """Why a multi-page run must not submit by itself on the Review page (Workday, Vatsal 2026-11-16): every field
    filled, nothing left for Vatsal, no errors, no eligibility warnings or duplicates. Best guesses don't stop it."""
    from .tracker.commands import open_db

    if not result.reached_review:
        return ["it didn't reach the Review page"]
    conn = open_db(paths, cfg)
    try:
        clean = clean_review_runs(conn, job.ats)
    finally:
        conn.close()
    eligibility = read_json(folder / "eligibility.json") if (folder / "eligibility.json").is_file() else {}
    errors = adapter.page_errors(page) if hasattr(adapter, "page_errors") else []
    if job.ats == "workday":
        # Vatsal, 2026-11-16: on Workday's Review page, submit whether or not he stepped in along the way. Workday only
        # shows Review once every required field is filled; an error on the page or an unclear button still stops it.
        reasons = ["the Review page shows errors: " + "; ".join(e[:80] for e in errors[:3])] if errors else []
        if not hasattr(adapter, "submit_review"):
            reasons.append("helper can't click this site's Submit button")
        return reasons
    gate = Gate(ats=job.ats, clean_runs=clean,
                min_clean_runs=cfg.auto_submit_min_clean_runs_by_site.get(job.ats, cfg.auto_submit_min_clean_runs),
                answers=planned, results=result.results, eligibility=eligibility, captcha_seen=False,
                page_errors=errors, has_submit_button=hasattr(adapter, "submit_review"), best_guesses_ok=True)
    reasons = blockers(gate)
    reasons += [f"not filled: {r.label[:70]}" for r in result.results if not r.ok]
    reasons += [f"you had to step in: {p[:90]}" for p in pauses if re.search(r"(?i)answer these|fix it in the window", p)]
    return list(dict.fromkeys(reasons))


EARLIER_ANSWERS = "claude_answers.json"  # field -> Claude's answer, for this run only


def _copy_merged(plan: AnswerPlan, answers: list[PlannedAnswer]) -> None:
    by_field = {a.field: a for a in plan.answers}
    for a in answers:
        merged = by_field.get(a.field)
        if merged is not None and merged.value is not None and a.value != merged.value:
            for name in ("value", "kind", "status", "confidence", "source"):
                setattr(a, name, getattr(merged, name))


def _ask_claude_midfill(paths: Paths, state: Any, job: Job, answers: list[PlannedAnswer], moved_on: Any,
                        wait_seconds: float, poll: float) -> None:
    """questions_needed.json -> answers_provided.json while the form stays open. No approval step: Vatsal reviews
    the answers on the site's Review page (Vatsal, 2026-11-14)."""
    folder = run_dir(paths, state.run_id)
    plan = AnswerPlan(answers=[a.model_copy() for a in answers])
    if not plan.open_items():
        return
    # Claude's answers from earlier in this fill: a page Workday made helper fill again asks the same questions
    # (Analog Devices, Kensho, 2026-11-16), so reuse them instead of asking twice
    earlier_file = folder / EARLIER_ANSWERS
    earlier: dict[str, str] = read_json(earlier_file) if earlier_file.is_file() else {}
    reuse = [{"field": a.field, "answer": earlier[a.field]} for a in plan.open_items() if a.field in earlier]
    if reuse and not _merge(plan, {"answers": reuse}):
        _copy_merged(plan, answers)
        plan = AnswerPlan(answers=[a.model_copy() for a in answers])
        if not plan.open_items():
            _say(f"Reusing Claude's earlier answers for {len(reuse)} question(s) on this page.")
            return
    needed = _needed(plan, job, state.options, paths, state.run_id)
    needed["then_run"] = "nothing: `helper fill` is waiting and continues by itself"
    needed["note"] = ("Questions on the open application page. Answer what the master list and the job description "
                      "support; leave out what only Vatsal can answer (he fills those in the window).")
    provided = folder / PROVIDED
    started = time.time()
    write_json(folder / NEEDED, needed)
    _say(f"waiting for Claude: read {rel(paths, folder / NEEDED)} and write {rel(paths, provided)} "
         "(helper fill continues by itself once it's there)")
    seen: Optional[float] = None
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if moved_on():
            _say("You moved on in the window, so helper continues from there.")
            return
        mtime = provided.stat().st_mtime if provided.is_file() else None
        if mtime is not None and mtime >= started - 1 and mtime != seen:
            seen = mtime
            try:
                data = read_json(provided)
            except Exception:
                data = None
            if data is not None:
                problems = _merge(plan, data)
                if not problems:
                    _copy_merged(plan, answers)
                    earlier.update({i["field"]: i["answer"] for i in data.get("answers", [])
                                    if i.get("field") and (i.get("answer") or "").strip()})
                    write_json(earlier_file, earlier)
                    saved = remember(paths, [a for a in answers if a.status == "needs_review"])
                    _say("Claude's answers are in; filling them." + (f" Remembered {saved} for later postings."
                                                                     if saved else ""))
                    return
                _say("waiting for Claude: fix these answers in " + rel(paths, provided) + ":\n"
                     + "\n".join(f"  {p}" for p in problems))
                plan = AnswerPlan(answers=[a.model_copy() for a in answers])
        time.sleep(poll)
    _say("No answers from Claude in time; those questions are left for you in the window.")


def remember(paths: Paths, answers: list[PlannedAnswer]) -> int:
    """Save answers to choice questions (dropdowns, radios, checkboxes) in data/answers.yaml, so the same question
    is answered the same way on later postings (Vatsal, 2026-11-15). Returns how many were added or changed."""
    import yaml

    from .answers import CHOICE_TYPES, NEVER_REMEMBERED, label_pattern
    from .models import ApprovedAnswer

    path = paths.answers
    current = AnswersFile.model_validate(load_yaml(path) or {}) if path.is_file() else AnswersFile()
    changed = 0
    for a in answers:
        if a.type not in CHOICE_TYPES or not a.value or a.key in NEVER_REMEMBERED:
            continue
        hit = next((e for e in current.answers if any(re.search(p, a.label, re.IGNORECASE)
                                                       for p in e.question_patterns)), None)
        if hit is not None:
            if hit.answer != a.value:
                hit.answer, hit.approved_on = a.value, date.today().isoformat()
                changed += 1
            continue
        slug = "_".join(re.findall(r"[a-z0-9]+", a.label.lower()))[:60]
        current.answers.append(ApprovedAnswer(id=f"remembered_{slug}", question_patterns=[label_pattern(a.label)],
                                              answer=a.value, approved_on=date.today().isoformat()))
        changed += 1
    if changed:
        header = ("# Approved answers (git-ignored): free text Vatsal approved, and dropdown answers helper remembers so\n"
                  "# the same question gets the same answer on later postings (Vatsal, 2026-11-15).\n")
        body = yaml.safe_dump({"answers": [e.model_dump(exclude_none=True) for e in current.answers]}, sort_keys=False,
                              allow_unicode=True, width=120)
        path.write_text(header + body, encoding="utf-8")
    return changed


def _plain(bullet: str) -> str:
    return " ".join(bullet.replace("**", "").split())


def _history(paths: Paths, cfg: Config, state: Any, folder: Path, profile: Profile) -> tuple[list[Any], list[Any]]:
    """The resume's experience entries and the profile's schools, for forms that ask for them one by one."""
    from .adapters.workday_page import SchoolEntry, WorkEntry
    from .dates import MonthYear, parse_month_year

    def month(iso: Optional[str]) -> Optional[MonthYear]:
        if not iso or not re.match(r"^\d{4}-\d{2}$", iso):
            return None
        return MonthYear(int(iso[:4]), int(iso[5:]))

    run = RunOptions.model_validate(state.options["run"])
    ctx = load_context(paths, cfg, run)
    tailor = read_json(folder / "tailor_output.json") if (folder / "tailor_output.json").is_file() else {}
    work = []
    for e in tailor.get("experience", []):
        item = ctx.master.item(e["entry_id"])
        meta = ctx.meta.experience.get(e["entry_id"])
        dates = item.dates
        work.append(WorkEntry(
            title=e["title"], company=item.organization or "", location=(meta.location if meta else None) or item.location,
            start=month(dates.start if dates else None), end=month(dates.end if dates else None),
            current=bool(dates and (dates.end or "").lower() == "present"),
            description="\n".join(f"- {_plain(b)}" for b in e["bullets"]),
        ))
    gpa = grades(paths)
    schools = [SchoolEntry(school=ed.school, degree=ed.degree, field_of_study=ed.major, start=parse_month_year(ed.start),
                           end=parse_month_year(run.grad_date if ed.end == "per_run" else ed.end),
                           gpa=gpa.get(ed.school))
               for ed in profile.education]
    return work, schools


def _resume_skills(folder: Path) -> list[str]:
    """The tailored resume's skills lines, in order, once each."""
    tailor = read_json(folder / "tailor_output.json") if (folder / "tailor_output.json").is_file() else {}
    return list(dict.fromkeys(i for line in tailor.get("skills_lines", []) for i in line.get("items", [])))


def _certifications(paths: Paths) -> list[dict[str, Any]]:
    """Vatsal's certificates (data/certifications.yaml, git-ignored), for forms that list certifications."""
    data = load_yaml(paths.data / "certifications.yaml") if (paths.data / "certifications.yaml").is_file() else None
    return [c for c in (data or {}).get("certifications", []) if isinstance(c, dict) and c.get("name")]


def _credentials(paths: Paths, ats: str, job: Optional[Job] = None) -> Optional[Any]:
    """The application login from .env for sites where helper signs in (rule 8's exceptions). Never printed."""
    if ats == "icims":
        from .adapters.icims import Credentials

        email, password = env_value(paths, "ICIMS_EMAIL"), env_value(paths, "ICIMS_PASSWORD")
        return Credentials(email, password, env_value(paths, "ICIMS_USERNAME")) if email and password else None
    return _workday_credentials(paths, job)


def _workday_credentials(paths: Paths, job: Optional[Job] = None) -> Optional[Any]:
    from .adapters.workday import Credentials, parse_ref

    email, password = env_value(paths, "WORKDAY_EMAIL"), env_value(paths, "WORKDAY_PASSWORD")
    if not (email and password):
        return None
    ref = (parse_ref(job.url or "") or parse_ref(job.apply_url or "")) if job else None
    key = ref.account_key if ref else None
    return Credentials(email, password, long_password=env_value(paths, "APPLICATION_PASSWORD_LONG"),
                       uses_long=bool(key and key in _long_password_accounts(paths)),
                       note_long=(lambda: _note_long_password(paths, key)) if key else None)


def _long_password_accounts(paths: Paths) -> set[str]:
    """Accounts made with the long password (data/accounts.yaml, git-ignored; it holds no secrets)."""
    return set(_accounts_file(paths).get("long_password", []))


def _note_long_password(paths: Paths, key: str) -> None:
    data = _accounts_file(paths)
    data["long_password"] = sorted(_long_password_accounts(paths) | {key})
    _write_accounts_file(paths, data)


def _accounts_file(paths: Paths) -> dict[str, Any]:
    path = paths.data / "accounts.yaml"
    return (load_yaml(path) if path.is_file() else None) or {}


def _write_accounts_file(paths: Paths, data: dict[str, Any]) -> None:
    import yaml

    (paths.data / "accounts.yaml").write_text(
        "# Application accounts helper knows about (git-ignored, no secrets). long_password: made with\n"
        "# APPLICATION_PASSWORD_LONG; workday: accounts that exist, so helper signs in first.\n"
        + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _note_account(paths: Paths, job: Job) -> None:
    from .adapters.workday import parse_ref

    ref = parse_ref(job.url or "") or parse_ref(job.apply_url or "")
    if ref is None:
        return
    data = _accounts_file(paths)
    known = set(data.get("workday", []))
    if ref.account_key not in known:
        data["workday"] = sorted(known | {ref.account_key})
        _write_accounts_file(paths, data)


def _has_workday_account(paths: Paths, cfg: Config, state: Any, job: Job) -> bool:
    """An earlier application or a known account at this company's Workday means the account exists: sign in first."""
    from .adapters.workday import parse_ref
    from .tracker.commands import open_db

    ref = parse_ref(job.url or "") or parse_ref(job.apply_url or "")
    if ref is None:
        return False
    if ref.account_key in _accounts_file(paths).get("workday", []):
        return True
    conn = open_db(paths, cfg)
    try:
        row = conn.execute("SELECT 1 FROM applications WHERE job_url LIKE ? AND id != ? LIMIT 1",
                           (f"%{ref.account_key}%", state.application_id or -1)).fetchone()
    finally:
        conn.close()
    return row is not None


def _flow_summary(result: Any, planned: list[PlannedAnswer], wait: float) -> str:
    ok = [r for r in result.results if r.ok]
    account = f" Workday account: {result.account}." if result.account else ""
    lines = [f"Filled {len(ok)} of {len(result.results)} field(s).{account}"]
    for r in result.results:
        if not r.ok:
            lines.append(f"  NOT filled: {r.label}: {r.detail}")
    drafted = [a for a in planned if a.status == "needs_review"]
    guesses = [a for a in planned if a.kind == "confirm" and a.value]
    if drafted:
        lines.append("Drafted by Claude (check them on the Review page):")
        lines += [f"  {a.label}\n    -> {a.value}" for a in drafted]
    if guesses:
        lines.append("Best guesses (check them on the Review page):")
        lines += [f"  {a.label}: {a.value}" for a in guesses]
    if result.reached_review:
        lines.append("Reached Workday's Review page. Unless auto-submit goes ahead (below), check everything there and "
                     f"click Submit yourself. helper watches for the confirmation for up to {wait:g} minutes.")
    else:
        lines.append("helper stopped before the Review page (see above). Finish the application in the window and "
                     f"submit it yourself; helper watches for the confirmation for up to {wait:g} minutes.")
    return "\n".join(lines)


def _snapshot(page: Any, form: Optional[Any], path: Path) -> bool:
    """A screenshot of the form itself: the embedded frame when the form lives in one, else the whole page."""
    try:
        if form is not None and form is not page.main_frame:
            form.frame_element().screenshot(path=str(path))
        else:
            page.screenshot(path=str(path), full_page=True)
        return True
    except Exception:
        try:
            page.screenshot(path=str(path), full_page=True)
            return True
        except Exception:
            return False


def _required(plan: AnswerPlan, field_name: str) -> bool:
    a = plan.get(field_name)
    return bool(a and a.required)


def _fill_summary(results: list[Any], failed_required: list[Any], wait: float) -> str:
    ok = [r for r in results if r.ok]
    lines = [f"Filled {len(ok)} of {len(results)} field(s)."]
    for r in results:
        if not r.ok:
            lines.append(f"  NOT filled: {r.label}: {r.detail}")
    if failed_required:
        lines.append("Some required fields need you: fill them in the browser window.")
    lines.append("Review everything in the browser window, then click Submit yourself. helper never clicks it "
                 f"(review mode). It watches for the confirmation for up to {wait:g} minutes.")
    return "\n".join(lines)


def _open_form(page: Any, adapter: Any, url: str, wait: float, poll: float, seen: dict[str, bool]) -> Optional[Any]:
    """Open the form page and find the form. A page that comes up without it gets one reload: Lever answered TRI with
    "Something went wrong on our end. Please try reloading" (2026-11-17). Nothing is typed or sent before this."""
    for attempt in (1, 2):
        if attempt == 1:
            page.goto(url, wait_until="domcontentloaded")
        else:
            _say("The page came up without the application form; reloading it once.")
            page.reload(wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        seen["captcha"] = _wait_out_captcha(page, wait, poll) or seen.get("captcha", False)
        form = adapter.find_form(page)
        if form is not None:
            return form
    return None


def _wait_out_captcha(page: Any, wait: float, poll: float) -> bool:
    """True when a CAPTCHA showed (Vatsal solves it; helper never does, rule 6)."""
    if not captcha_visible(page):
        return False
    _say("A CAPTCHA or verification step is showing. Please solve it in the browser window (helper never does, "
         "rule 6). Waiting...")
    deadline = time.monotonic() + wait * 60
    while captcha_visible(page) and time.monotonic() < deadline:
        time.sleep(poll)
    return True


# A page that answers Submit by saying the application didn't go through: "There was an error verifying your
# application. Please try again." (Lever's check on TRI, 2026-11-17). That's a clear "not sent", not an unclear result.
_SUBMIT_REJECTED_RE = re.compile(
    r"(?i)\berror (verifying|submitting|processing) your application\b"
    r"|\byour application (could not|couldn't|was not|wasn't) (be )?(submitted|sent|processed|received)\b"
    r"|\b(unable|failed) to submit your application\b"
)


def _submit_rejected(page: Any) -> Optional[str]:
    try:
        text = page.locator("body").inner_text(timeout=2000)
    except Exception:
        return None
    m = _SUBMIT_REJECTED_RE.search(text or "")
    return m.group(0) if m else None


def _submit_refused(form: Optional[Any], adapter: Any, before: list[str]) -> list[str]:
    """New field errors on a form that is still there, Submit button and all: the form's own checks stopped the submit
    before anything was sent."""
    if form is None or not hasattr(adapter, "submit_buttons"):
        return []
    try:
        if not adapter.submit_buttons(form):
            return []
    except Exception:
        return []
    return [e for e in visible_errors(form) if e not in before and not e.startswith("helper couldn't")]


def _clear_submit_clicked(page: Any) -> None:
    """Forget the click the page recorded (SUBMIT_WATCH_JS), so the next click on Submit counts as a new one."""
    for frame in page.frames:
        try:
            frame.evaluate("() => { window.__helperSubmitClicked = false; }")
        except Exception:
            pass


def _wait_for_submit(page: Any, adapter: Any, submit_seen: dict[str, bool], wait: float, poll: float,
                     form: Optional[Any] = None, seen: Optional[dict[str, bool]] = None,
                     errors_before: Optional[list[str]] = None) -> str:
    """`errors_before`: the form's errors from before helper's own click on Submit (the click runs first)."""
    deadline = time.monotonic() + wait * 60
    since_submit: Optional[float] = None
    warned_captcha = warned_code = False
    clicked = getattr(adapter, "submit_clicked", None)
    before = errors_before if errors_before is not None else visible_errors(form) if form is not None else []
    while time.monotonic() < deadline:
        if page.is_closed():
            return "unverified" if submit_seen["value"] else "closed"
        if adapter.is_confirmation(page, form):
            return "confirmed"
        if clicked is not None and not submit_seen["value"] and clicked(page):
            submit_seen["value"] = True
        if submit_seen["value"] and since_submit is None:
            since_submit = time.monotonic()
        if since_submit is not None and time.monotonic() - since_submit > 3 and _submit_rejected(page):
            return "rejected"  # the site said outright it didn't take the application (TRI's Lever, 2026-11-17)
        refused = _submit_refused(form, adapter, before) if since_submit is not None and \
            time.monotonic() - since_submit > 4 else []
        if refused:
            # the form stopped the submit with its own field errors: nothing was sent, so it isn't a submit to log
            # as unverified (All Options' BambooHR, 2026-11-18: State had reset; logged as sent and put in Notion)
            _say("The form didn't take the submit; nothing was sent. It shows: " + "; ".join(e[:80] for e in refused[:4])
                 + ". Fix it in the window yourself and click Submit; helper keeps watching for the confirmation.")
            submit_seen["value"], since_submit = False, None
            before = visible_errors(form)
            if clicked is not None:
                _clear_submit_clicked(page)
        if since_submit is not None and time.monotonic() - since_submit > 60:
            return "unverified"  # rule 10: never retry; an unclear result is submitted_unverified
        if not warned_captcha and captcha_visible(page):
            _say("A CAPTCHA appeared: solve it in the browser window; helper keeps watching.")
            warned_captcha = True
            if seen is not None:  # at Submit: Vatsal solves it; the fill itself still counts as clean (Vatsal,
                seen["at_submit"] = True  # 2026-11-16). One that blocks the page before filling still counts.
        if not warned_code and asks_for_emailed_code(page, form):
            _say("The site emailed you a verification code: enter it in the browser window and submit. helper never "
                 "does that step, and it keeps watching for the confirmation.")
            warned_code = True
            if seen is not None:  # Greenhouse's emailed code at Submit: not a problem with the fill (Vatsal, 2026-11-16)
                seen["at_submit"] = True
        try:
            page.wait_for_timeout(poll * 1000)
        except Exception:
            return "unverified" if submit_seen["value"] else "closed"
    return "unverified" if submit_seen["value"] else "timeout"


def _record_status(paths: Paths, cfg: Config, state: Any, status: str, *, screenshot: Optional[Path] = None) -> None:
    from .tracker import repo
    from .tracker.commands import open_db

    if not state.application_id:
        return
    conn = open_db(paths, cfg)
    try:
        current = repo.get(conn, state.application_id)
        if status == "filled" and current["status"] not in ("drafted",):
            return
        repo.set_status(conn, state.application_id, status, note=f"helper fill ({state.run_id})")
        if screenshot:
            repo.update(conn, state.application_id, confirmation_screenshot=rel(paths, screenshot))
        conn.commit()
    finally:
        conn.close()


def _log_outcome(paths: Paths, cfg: Config, state: Any, outcome: str, shot: Optional[Path], results: list[Any], *,
                 mode: str = "review", problems: Optional[list[str]] = None, ats: Optional[str] = None) -> FillOutcome:
    """Spec 9 steps 10 and 11: SQLite first, then Notion, then the CSV. Every fill is also logged for the auto-submit
    count (spec 10.5)."""
    from .tracker.commands import export_csv, open_db, push_one

    conn = open_db(paths, cfg)
    try:
        status_for_log = {"confirmed": "applied", "unverified": "submitted_unverified"}.get(outcome, outcome)
        record_fill_run(conn, state.run_id, ats, mode, status_for_log, list(problems or []))
        conn.commit()
    finally:
        conn.close()
    from .hunt import on_fill_outcome

    on_fill_outcome(paths, cfg, state.run_id, state.application_id, outcome)  # sent: off the job hunt's list

    if outcome in ("closed", "timeout", "rejected"):
        set_step(state, "submit", "waiting_for_user", "not submitted yet")
        save_state(paths, state)
        if outcome == "rejected":
            return FillOutcome("filled", "Not sent: the site answered Submit with an error and didn't take the "
                                         "application from helper's window. The application stays 'filled'. Apply in "
                                         f"your normal browser (`helper assist {state.run_id}` opens it with a paste "
                                         f"sheet), then `helper mark {state.application_id} applied`.", results)
        how = "the browser was closed" if outcome == "closed" else "the wait ran out"
        return FillOutcome("filled", f"Not submitted ({how}). The application stays 'filled'. Run `helper fill "
                                     f"{state.run_id}` to fill it again, or mark it after applying by hand: "
                                     f"`helper mark {state.application_id} applied`.", results)
    status = "applied" if outcome == "confirmed" else "submitted_unverified"
    _record_status(paths, cfg, state, status, screenshot=shot)
    set_step(state, "submit", "done", status)
    conn = open_db(paths, cfg)
    try:
        notion = push_one(paths, conn, state.application_id) if state.application_id else "Notion: no application row."
        export_csv(conn, paths.exports / "applications.csv")
    finally:
        conn.close()
    set_step(state, "log", "done", notion)
    save_state(paths, state)
    lead = ("Submitted and confirmed." if status == "applied" else
            "Submit was clicked, but no confirmation showed: logged as submitted_unverified (rule 10, never retried).")
    shot_text = f" Screenshot: {rel(paths, shot)}." if shot else ""
    return FillOutcome(status, f"{lead}{shot_text}\n{notion}\nCSV: exports/applications.csv "
                               f"(applied {date.today().isoformat()}).", results)


def preview(paths: Paths, cfg: Config, job: Job) -> str:
    """`helper apply --dry-run`: what helper would check and fill for this posting. Writes nothing."""
    from .eligibility import check_eligibility, format_eligibility, load_applications
    from .keywords import extract_keywords
    from .models import CapsFile

    run = RunOptions(projects_per_resume=cfg.projects_per_resume, bullets_per_item=cfg.bullets_per_item,
                     grad_date=cfg.default_grad_date)
    ctx = load_context(paths, cfg, run)
    keywords = extract_keywords(job.description_text, ctx.master, ctx.vocab, ctx.project_sets, ctx.families)
    profile = Profile.model_validate(load_yaml(paths.profile))
    eligibility = check_eligibility(job, cfg, caps=CapsFile.model_validate(load_yaml(paths.caps)),
                                    applications=load_applications(paths.db(cfg)), profile=profile, keywords=keywords)
    answer_ctx = AnswerContext(
        profile=profile, cfg=cfg, job=job, grad_date=eligibility.grad_date, employment=eligibility.employment,
        season=eligibility.season, season_year=eligibility.season_year,
        approved=AnswersFile.model_validate((load_yaml(paths.answers) if paths.answers.is_file() else None) or {}),
        past_employers=[e.organization for e in ctx.master.experience if e.organization],
        documents=documents(paths, profile), grades=grades(paths),
    )
    gaps = [t.canonical for t in keywords.tech if t.status == "not_in_master"]
    return "\n".join([
        f"{job.company}: {job.role_title} ({job.location or 'location not given'})",
        f"Apply at: {job.apply_url or job.url}",
        f"Technologies named: {len(keywords.tech)}; not in your master list: {', '.join(gaps) or 'none'}",
        "",
        format_eligibility(eligibility),
        "",
        "Form answers (dry run, nothing filled):",
        format_plan(plan_answers(job.questions, answer_ctx)),
    ])
