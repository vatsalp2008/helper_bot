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

from .answers import NOTE_STYLE, AnswerContext, AnswerPlan, PlannedAnswer, format_plan, match_option, plan_answers
from .browser import browser_session, captcha_visible, make_pause
from .config import Paths, env_value, load_yaml
from .errors import HelperError, NeedsUser, WaitingForClaude
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


def fetch_job(url: str, jd_text: Optional[str] = None) -> Job:
    from .adapters import adapter_for
    from .ats import clean_url

    url = clean_url(url)
    job: Job = adapter_for(url).fetch_job(url)
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
        raise HelperError(f"data/profile.yaml is incomplete: {exc}") from None
    master = load_context(paths, cfg, run).master
    return AnswerContext(
        profile=profile, cfg=cfg, job=job, grad_date=run.grad_date,
        employment=eligibility.get("employment", "unknown"), season=eligibility.get("season"),
        season_year=eligibility.get("season_year"), approved=AnswersFile.model_validate(load_yaml(paths.answers) or {}),
        past_employers=[e.organization for e in master.experience if e.organization],
        documents={**documents(paths, profile), **_letter_documents(paths, state.options)},
    )


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
        if a.options:
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
    if job.ats == "workday" and not job.questions:
        write_json(folder / PLAN, AnswerPlan(answers=[]).model_dump(mode="json"))
        set_step(state, "answers", "done", "Workday: questions are answered page by page during the fill")
        save_state(paths, state)
        return (f"Workday shows its questions page by page, so `helper fill {run_id}` answers them as it goes. Claude "
                "drafts any written answers while it runs, and you review everything on Workday's Review page "
                "before you submit.")
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
    missing = [a for a in plan.answers if a.kind == "unknown" and a.required]
    review = plan.needs_review()
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
    write_json(folder / PLAN, plan.model_dump(mode="json"))
    set_step(state, "answers", "done", f"{len(review)} answer(s) approved")
    save_state(paths, state)
    drafted = [a for a in review if a.source == "Claude draft"]
    save_hint = (f"\nTo reuse {'this answer' if len(drafted) == 1 else 'these answers'} later, ask Claude to save "
                 f"{'it' if len(drafted) == 1 else 'them'} to data/answers.yaml.") if drafted else ""
    return f"Answers approved. Fill the form with `helper fill {run_id}`.{save_hint}"


# --------------------------------------------------------------------------- filling (spec 10.1, 10.2, 9 steps 8-11)


@dataclass
class FillOutcome:
    status: str  # dry_run | filled | applied | submitted_unverified
    message: str
    results: list[Any] = field(default_factory=list)


def _say(text: str) -> None:
    print(text, flush=True)
    sys.stdout.flush()


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
        return FillOutcome("dry_run", f"Would fill {job.apply_url or job.url} with:\n{format_plan(plan)}")
    if not pdf.is_file():
        raise HelperError(f"the resume PDF is missing: {rel(paths, pdf)}")
    adapter = adapter_for(job.url or job.apply_url or "")
    profile = Profile.model_validate(load_yaml(paths.profile))
    wait = cfg.fill_wait_timeout_minutes if wait_minutes is None else wait_minutes
    if hasattr(adapter, "run_flow"):
        return _fill_flow(paths, cfg, state, job, pdf, adapter, profile, wait, headless, poll_seconds)
    submit_seen = {"value": False}
    with browser_session(paths, cfg, headless=headless) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.on("request", lambda req: submit_seen.update(value=True) if adapter.is_submit_request(req.url, req.method)
                else None)
        page.goto(job.apply_url or job.url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        _wait_out_captcha(page, wait, poll_seconds)
        form = adapter.find_form(page)
        if form is None:
            _say("helper couldn't find the application form on this page, so it filled nothing. Fill it in the "
                 "browser window yourself; helper still watches for the confirmation and logs it.")
            results = []
        else:
            results = adapter.fill_form(form, plan.answers, pdf, country=profile.tier1.address.country,
                                        city=profile.tier1.address.city, pause=make_pause(cfg))
        _snapshot(page, form, folder / "filled.png")
        write_json(folder / "fill_log.json", [vars(r) for r in results])
        failed_required = [r for r in results if not r.ok and _required(plan, r.field)]
        _record_status(paths, cfg, state, "filled")
        set_step(state, "fill", "done", f"{sum(r.ok for r in results)} field(s) filled")
        save_state(paths, state)
        _say(_fill_summary(results, failed_required, wait))
        outcome = _wait_for_submit(page, adapter, submit_seen, wait, poll_seconds, form)
        shot = None
        if outcome in ("confirmed", "unverified"):
            shot = folder / "confirmation.png"
            if not _snapshot(page, form, shot):
                shot = None
    return _log_outcome(paths, cfg, state, outcome, shot, results)


# --------------------------------------------------------------------------- multi-page flows (Workday)


def _fill_flow(paths: Paths, cfg: Config, state: Any, job: Job, pdf: Path, adapter: Any, profile: Profile, wait: float,
               headless: Optional[bool], poll_seconds: float) -> FillOutcome:
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
        write_json(folder / PLAN, AnswerPlan(answers=planned).model_dump(mode="json"))

    go_flag = folder / "go_on"

    def go_on() -> bool:
        if go_flag.exists():
            go_flag.unlink()
            return True
        return False

    submit_seen = {"value": False}
    with browser_session(paths, cfg, headless=headless) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.on("request", lambda req: submit_seen.update(value=True) if adapter.is_submit_request(req.url, req.method)
                else None)
        page.goto(job.apply_url or job.url, wait_until="domcontentloaded")
        flow = adapter.FlowContext(
            company=job.company, plan=plan_page, ask_claude=ask_claude, resume_pdf=pdf, work=work, schools=schools,
            skills=_resume_skills(folder), certifications=_certifications(paths),
            say=_say, pause=make_pause(cfg), credentials=_workday_credentials(paths),
            has_account=_has_workday_account(paths, cfg, state, job), go_on=go_on, wait_seconds=wait * 60,
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
        outcome = _wait_for_submit(page, adapter, submit_seen, wait, poll_seconds, None)
        shot = None
        if outcome in ("confirmed", "unverified"):
            shot = folder / "confirmation.png"
            if not _snapshot(page, None, shot):
                shot = None
    return _log_outcome(paths, cfg, state, outcome, shot, result.results)


def _ask_claude_midfill(paths: Paths, state: Any, job: Job, answers: list[PlannedAnswer], moved_on: Any,
                        wait_seconds: float, poll: float) -> None:
    """questions_needed.json -> answers_provided.json while the form stays open. No approval step: Vatsal reviews
    the answers on the site's Review page (Vatsal, 2026-11-14)."""
    folder = run_dir(paths, state.run_id)
    plan = AnswerPlan(answers=[a.model_copy() for a in answers])
    if not plan.open_items():
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
                    by_field = {a.field: a for a in plan.answers}
                    for a in answers:
                        merged = by_field.get(a.field)
                        if merged is not None and merged.value is not None and a.value != merged.value:
                            for name in ("value", "kind", "status", "confidence", "source"):
                                setattr(a, name, getattr(merged, name))
                    _say("Claude's answers are in; filling them.")
                    return
                _say("waiting for Claude: fix these answers in " + rel(paths, provided) + ":\n"
                     + "\n".join(f"  {p}" for p in problems))
                plan = AnswerPlan(answers=[a.model_copy() for a in answers])
        time.sleep(poll)
    _say("No answers from Claude in time; those questions are left for you in the window.")


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
    schools = [SchoolEntry(school=ed.school, degree=ed.degree, field_of_study=ed.major, start=parse_month_year(ed.start),
                           end=parse_month_year(run.grad_date if ed.end == "per_run" else ed.end))
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


def _workday_credentials(paths: Paths) -> Optional[Any]:
    from .adapters.workday import Credentials

    email, password = env_value(paths, "WORKDAY_EMAIL"), env_value(paths, "WORKDAY_PASSWORD")
    return Credentials(email, password) if email and password else None


def _has_workday_account(paths: Paths, cfg: Config, state: Any, job: Job) -> bool:
    """An earlier application at this company's Workday means the account exists: sign in first."""
    from .adapters.workday import parse_ref
    from .tracker.commands import open_db

    ref = parse_ref(job.url or "") or parse_ref(job.apply_url or "")
    if ref is None:
        return False
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
        lines.append("Stopped on Workday's Review page. Check everything there, then click Submit yourself; helper never "
                     f"clicks it (review mode). It watches for the confirmation for up to {wait:g} minutes.")
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


def _wait_out_captcha(page: Any, wait: float, poll: float) -> None:
    if not captcha_visible(page):
        return
    _say("A CAPTCHA or verification step is showing. Please solve it in the browser window (helper never does, "
         "rule 6). Waiting...")
    deadline = time.monotonic() + wait * 60
    while captcha_visible(page) and time.monotonic() < deadline:
        time.sleep(poll)


def _wait_for_submit(page: Any, adapter: Any, submit_seen: dict[str, bool], wait: float, poll: float,
                     form: Optional[Any] = None) -> str:
    deadline = time.monotonic() + wait * 60
    since_submit: Optional[float] = None
    warned_captcha = False
    clicked = getattr(adapter, "submit_clicked", None)
    while time.monotonic() < deadline:
        if page.is_closed():
            return "unverified" if submit_seen["value"] else "closed"
        if adapter.is_confirmation(page, form):
            return "confirmed"
        if clicked is not None and not submit_seen["value"] and clicked(page):
            submit_seen["value"] = True
        if submit_seen["value"] and since_submit is None:
            since_submit = time.monotonic()
        if since_submit is not None and time.monotonic() - since_submit > 60:
            return "unverified"  # rule 10: never retry; an unclear result is submitted_unverified
        if not warned_captcha and captcha_visible(page):
            _say("A CAPTCHA appeared: solve it in the browser window; helper keeps watching.")
            warned_captcha = True
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


def _log_outcome(paths: Paths, cfg: Config, state: Any, outcome: str, shot: Optional[Path], results: list[Any]) -> FillOutcome:
    """Spec 9 steps 10 and 11: SQLite first, then Notion, then the CSV."""
    from .tracker.commands import export_csv, open_db, push_one

    if outcome in ("closed", "timeout"):
        set_step(state, "submit", "waiting_for_user", "not submitted yet")
        save_state(paths, state)
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
        approved=AnswersFile.model_validate(load_yaml(paths.answers) or {}),
        past_employers=[e.organization for e in ctx.master.experience if e.organization],
        documents=documents(paths, profile),
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
