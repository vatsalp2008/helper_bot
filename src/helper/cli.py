"""helper command line (spec section 12)."""

from __future__ import annotations

import functools
import os
from enum import Enum
from pathlib import Path
from typing import Annotated, Callable, Optional, TypeVar

import typer

from . import __version__
from .config import Paths, find_root
from .errors import EXIT_ERROR, EXIT_STOPPED, STOPPED, HelperError, report_unexpected

app = typer.Typer(
    help="helper: one job URL in; a tailored resume, a filled form, and a tracked application out.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

F = TypeVar("F", bound=Callable[..., object])


def guarded(func: F) -> F:
    """Print HelperError messages plainly and exit with the error's exit code. Anything else (a missing install, a
    busy browser, a bug) gets a plain explanation too, with the traceback saved for Claude (HELPER_DEBUG=1 shows it)."""

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return func(*args, **kwargs)
        except HelperError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(exc.exit_code) from None
        except (typer.Exit, typer.Abort, typer.TyperException):
            raise
        except KeyboardInterrupt:
            typer.echo(STOPPED, err=True)
            raise typer.Exit(EXIT_STOPPED) from None
        except Exception as exc:
            if os.environ.get("HELPER_DEBUG"):
                raise
            typer.echo(report_unexpected(exc), err=True)
            raise typer.Exit(EXIT_ERROR) from None

    return wrapper  # type: ignore[return-value]


def _paths() -> Paths:
    return Paths(find_root())



class SubmitMode(str, Enum):
    review = "review"
    auto = "auto"


def _version(value: bool) -> None:
    if value:
        typer.echo(f"helper {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        Optional[bool], typer.Option("--version", callback=_version, is_eager=True, help="Show the version.")
    ] = None,
) -> None:
    pass


# --------------------------------------------------------------------------- milestone 1


@app.command()
@guarded
def setup(
    interactive: Annotated[
        bool, typer.Option("--interactive", help="Prompt for unset profile values (run it in your own terminal).")
    ] = False,
) -> None:
    """Create folders, initialize SQLite, check LaTeX, Playwright and data files, and report what is missing."""
    from .setup_env import format_report, run_setup

    checks = run_setup(_paths(), interactive=interactive)
    typer.echo(format_report(checks))
    if any(c.status == "error" for c in checks):
        raise typer.Exit(EXIT_ERROR)


# --------------------------------------------------------------------------- later milestones


@app.command()
@guarded
def convert(
    show: Annotated[
        Optional[str], typer.Option("--show", help="After converting, print one item's parsed record by id.")
    ] = None,
) -> None:
    """Parse the master list markdown into data/master.yaml and show a parse summary."""
    from .config import load_config
    from .convert import format_summary, run_convert, show_item

    paths = _paths()
    result = run_convert(paths, load_config(paths))
    typer.echo(show_item(result.master, show) if show else format_summary(result))


# --------------------------------------------------------------------------- milestone 3


@app.command("template-check")
@guarded
def template_check(
    sample: Annotated[
        Optional[list[Path]],
        typer.Option("--sample", help="Also render this tailor_output.json with real master data (repeatable)."),
    ] = None,
) -> None:
    """Check that templates/resume.tex.j2 reproduces reference_resume.tex and its PDF (spec 6)."""
    from .config import load_config
    from .templatecheck import format_report, run_template_check

    paths = _paths()
    result = run_template_check(paths, load_config(paths), sample)
    typer.echo(format_report(result, paths))
    if not result.passed:
        raise typer.Exit(EXIT_ERROR)


@app.command()
@guarded
def apply(
    url: Annotated[str, typer.Argument(help="Job posting URL.")],
    projects: Annotated[Optional[int], typer.Option("--projects", min=1, max=8, help="Projects on the resume.")] = None,
    mode: Annotated[Optional[SubmitMode], typer.Option("--mode", help="review (default) or auto.")] = None,
    include_experience: Annotated[
        Optional[list[str]], typer.Option("--include-experience", help="Add an off-by-default experience entry.")
    ] = None,
    jd_file: Annotated[
        Optional[Path], typer.Option("--jd-file", exists=True, dir_okay=False, help="Use this job description text.")
    ] = None,
    grad_date: Annotated[
        Optional[str], typer.Option("--grad-date", help='Override the graduation date the eligibility check picks.')
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Read the posting and show checks and answers; no run.")] = False,
) -> None:
    """Full run: intake, eligibility, tailored resume, form answers, then `helper fill` (review mode stops before Submit)."""
    from .apply import fetch_job
    from .config import load_config
    from .pipeline import blocked_message, format_started, start_run, waiting

    paths = _paths()
    cfg = load_config(paths)
    job = fetch_job(url, jd_text=jd_file.read_text(encoding="utf-8") if jd_file else None)
    if dry_run:
        from .apply import preview

        typer.echo(preview(paths, cfg, job))
        return
    started = start_run(paths, cfg, job, command="apply", projects=projects, include_experience=include_experience,
                        grad_date=grad_date)
    submit_mode = mode.value if mode is not None else cfg.submit_mode
    if submit_mode != "review":  # auto: helper submits only when every spec 10.5 condition holds
        from .runs import save_state

        started.state.options["submit_mode"] = submit_mode
        save_state(paths, started.state)
    typer.echo(format_started(paths, started))
    from .hunt import on_run_started

    on_run_started(paths, cfg, url, started.state)  # a posting on the job hunt's list is being applied to now
    if submit_mode == "auto":
        typer.echo("Submit mode: auto. helper clicks Submit itself only if every auto-submit condition holds "
                   "(spec 10.5); otherwise this run falls back to review mode and says why.")
    if started.blocked:
        raise blocked_message(started.state)
    raise waiting(paths, started.state.run_id)


@app.command()
@guarded
def resume(
    url: Annotated[Optional[str], typer.Argument(help="Job posting URL (Greenhouse links are read directly).")] = None,
    jd_file: Annotated[
        Optional[Path], typer.Option("--jd-file", exists=True, dir_okay=False, help="The job description as text.")
    ] = None,
    company: Annotated[Optional[str], typer.Option("--company", help="Company name.")] = None,
    role: Annotated[Optional[str], typer.Option("--role", help="Role title, short (it names the PDF).")] = None,
    location: Annotated[Optional[str], typer.Option("--location", help="Job location, if known.")] = None,
    projects: Annotated[Optional[int], typer.Option("--projects", min=1, max=8, help="Projects on the resume.")] = None,
    include_experience: Annotated[
        Optional[list[str]], typer.Option("--include-experience", help="Add an off-by-default experience entry.")
    ] = None,
    grad_date: Annotated[
        Optional[str], typer.Option("--grad-date", help='"May 2027" (default) or "December 2027" (spec 8 item 4).')
    ] = None,
) -> None:
    """Tailor and render only: writes tailor_input.json for Claude, then `helper continue` builds the PDF."""
    from .config import load_config
    from .pipeline import blocked_message, format_started, start_resume, waiting

    paths = _paths()
    if jd_file is None:
        if not url:
            raise HelperError("pass a posting link, or --jd-file with the job description saved as text")
        from .apply import fetch_job
        from .pipeline import start_run

        started = start_run(paths, load_config(paths), fetch_job(url, purpose="resume"), command="resume", projects=projects,
                            include_experience=include_experience, grad_date=grad_date)
    else:
        if not company or not role:
            raise HelperError("--company and --role are required with --jd-file")
        started = start_resume(
            paths, load_config(paths), jd_text=jd_file.read_text(encoding="utf-8"), company=company, role=role,
            url=url, location=location, projects=projects, include_experience=include_experience, grad_date=grad_date,
        )
    typer.echo(format_started(paths, started))
    if url:
        from .hunt import on_run_started

        on_run_started(paths, load_config(paths), url, started.state)  # a listed posting helper can't fill
    if started.blocked:
        raise blocked_message(started.state)
    raise waiting(paths, started.state.run_id)


@app.command()
@guarded
def fill(
    run_id: Annotated[str, typer.Argument(help="Run folder name under runs/.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be filled; open no browser.")] = False,
    wait: Annotated[Optional[float], typer.Option("--wait", help="Minutes to wait for you to submit.")] = None,
    headless: Annotated[bool, typer.Option("--headless", help="Hidden browser (tests only).")] = False,
    keep_open: Annotated[bool, typer.Option("--keep-open", help="The job hunt: if it stops for you, keep the window "
                                            "open for stuck_window_hours (default 24) instead of the usual wait.")] = False,
    replan: Annotated[bool, typer.Option("--replan", help="Plan the answers again with today's rules first (a refill "
                                         "after an answer rule was fixed); Claude's earlier answers are kept.")] = False,
    rescan: Annotated[bool, typer.Option("--rescan", help="Read a company-site form's questions again first (a refill "
                                         "after helper learned to read that kind of form); questions the rules can't "
                                         "answer go to Claude, as in `helper continue`.")] = False,
) -> None:
    """Fill the application form in a browser window, stop before Submit, and log the result after you submit."""
    from .apply import fill_run, replan as replan_answers, rescan as rescan_form
    from .config import load_config
    from .errors import EXIT_NEEDS_USER

    paths = _paths()
    cfg = load_config(paths)
    if keep_open and wait is None:  # Vatsal, 2026-11-16: a stuck window stays open for him to review later
        wait = cfg.stuck_window_hours * 60
    if rescan:  # waiting for Claude stops here (exit 3); after `helper continue`, run `helper fill` again
        typer.echo(rescan_form(paths, cfg, run_id))
    elif replan:
        changes = replan_answers(paths, cfg, run_id)
        typer.echo("Planned again with today's rules" + (":\n" + "\n".join(f"  {c}" for c in changes) if changes
                                                         else ": no answers changed."))
    outcome = fill_run(paths, cfg, run_id, dry_run=dry_run, wait_minutes=wait, headless=True if headless else None)
    typer.echo(outcome.message)
    if outcome.status == "filled":
        raise typer.Exit(EXIT_NEEDS_USER)


@app.command()
@guarded
def assist(
    run_id: Annotated[str, typer.Argument(help="Run folder name under runs/.")],
    no_open: Annotated[bool, typer.Option("--no-open", help="Only print the paste sheet; open nothing.")] = False,
) -> None:
    """You apply, helper prepares: open the posting in your normal browser, show the files, print a paste sheet."""
    from .assist import assist as run_assist

    typer.echo(run_assist(_paths(), run_id, open_browser=not no_open))


@app.command()
@guarded
def find(
    hours: Annotated[float, typer.Option("--hours", help="How far back to look.")] = 24,
    limit: Annotated[int, typer.Option("--limit", help="Postings to check for eligibility (newest first).")] = 100,
    no_check: Annotated[bool, typer.Option("--no-check", help="Skip the eligibility checks (faster).")] = False,
) -> None:
    """The job hunt: new postings from the last day, from every public source helper can read. Read-only, except that
    ineligible postings go on the list as skipped."""
    from .config import load_config
    from .hunt import find_command

    paths = _paths()
    typer.echo(find_command(paths, load_config(paths), hours, limit, check=not no_check))


queue_app = typer.Typer(help="The job hunt's list of postings (exports/postings_queue.md).", no_args_is_help=False,
                        invoke_without_command=True)
app.add_typer(queue_app, name="queue")


@queue_app.callback()
def queue_main(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Every row as JSON.")] = False,
    page_file: Annotated[Optional[Path], typer.Option("--page-file", help="Write the list's web page record "
                                                      "(hunt/list) to this JSON file.")] = None,
) -> None:
    """Show the list."""
    if ctx.invoked_subcommand is not None:
        return
    import json as _json

    from .config import load_config
    from .hunt import refresh
    from .tracker import queue as q
    from .tracker.commands import open_db

    paths = _paths()
    conn = open_db(paths, load_config(paths))
    try:
        if as_json:
            typer.echo(_json.dumps(q.as_dicts(conn), indent=1, ensure_ascii=False, default=str))
            return
        if page_file is not None:
            page_file.parent.mkdir(parents=True, exist_ok=True)
            page_file.write_text(_json.dumps(q.page_doc(conn), ensure_ascii=False, default=str), encoding="utf-8")
            typer.echo(f"wrote {page_file} ({len(q.page_doc(conn)['rows'])} rows)")
            return
        typer.echo(q.render_markdown(conn))
        typer.echo(f"(also in {refresh(paths, conn).relative_to(paths.root)})")
    finally:
        conn.close()


@queue_app.command("add")
@guarded
def queue_add(
    urls: Annotated[list[str], typer.Argument(help="Posting links (details come from the latest `helper find`).")],
    skip: Annotated[Optional[str], typer.Option("--skip", help="List them as skipped, with this reason.")] = None,
) -> None:
    """Put postings on the list (queued), or record them as skipped."""
    from .config import load_config
    from .hunt import add

    paths = _paths()
    for line in add(paths, load_config(paths), urls, status="skipped" if skip else "queued", reason=skip):
        typer.echo(line)


@queue_app.command("set")
@guarded
def queue_set(
    key: Annotated[str, typer.Argument(help="Row id, posting link or run id.")],
    status: Annotated[str, typer.Argument(help="queued, applying, needs_you, ready_for_you or skipped.")],
    reason: Annotated[Optional[str], typer.Option("--reason", help="Why (shown on the list).")] = None,
) -> None:
    """Change a posting's status on the list."""
    from .config import load_config
    from .hunt import refresh
    from .tracker import queue as q
    from .tracker.commands import open_db

    paths = _paths()
    conn = open_db(paths, load_config(paths))
    try:
        if not q.set_status(conn, key, status, reason):
            raise HelperError(f"nothing on the list matches {key!r} (`helper queue` shows the list)")
        conn.commit()
        refresh(paths, conn)
        typer.echo(f"{key}: {status}" + (f" ({reason})" if reason else ""))
    finally:
        conn.close()


@queue_app.command("remove")
@guarded
def queue_remove(key: Annotated[str, typer.Argument(help="Row id, posting link or run id.")]) -> None:
    """Take a posting off the list."""
    from .config import load_config
    from .hunt import refresh
    from .tracker import queue as q
    from .tracker.commands import open_db

    paths = _paths()
    conn = open_db(paths, load_config(paths))
    try:
        if not q.remove(conn, key):
            raise HelperError(f"nothing on the list matches {key!r}")
        conn.commit()
        refresh(paths, conn)
        typer.echo(f"removed {key}")
    finally:
        conn.close()


@queue_app.command("note")
@guarded
def queue_note(text: Annotated[str, typer.Argument(help="What this session found (shown at the top of the list).")]) -> None:
    """Record the session's summary on the list (for example: only 7 good postings in the last 24 hours)."""
    from .config import load_config
    from .hunt import refresh
    from .tracker import queue as q
    from .tracker.commands import open_db

    paths = _paths()
    conn = open_db(paths, load_config(paths))
    try:
        q.set_note(conn, text)
        conn.commit()
        refresh(paths, conn)
    finally:
        conn.close()


@app.command("continue")
@guarded
def continue_(
    run_id: Annotated[str, typer.Argument(help="Run folder name under runs/.")],
    build_anyway: Annotated[
        bool, typer.Option("--build-anyway", help="Build even though the eligibility check recommends skipping.")
    ] = False,
    approve_answers: Annotated[
        bool, typer.Option("--approve-answers", help="Vatsal approved the drafted form answers (apply runs).")
    ] = False,
) -> None:
    """Resume a run: after an eligibility pause, or after Claude wrote its output file (validate, render, check)."""
    from .config import load_config
    from .pipeline import continue_run, format_finished

    from .cover_letter import cover_letter_step

    paths = _paths()
    cfg = load_config(paths)
    done = continue_run(paths, cfg, run_id, build_anyway=build_anyway)
    if done is not None:
        typer.echo(format_finished(paths, done))
    letter = cover_letter_step(paths, cfg, run_id)  # every resume gets a cover letter (Vatsal, 2026-11-14)
    if letter:
        typer.echo(letter)
    from .runs import load_state

    if load_state(paths, run_id).command == "apply":
        from .apply import answers_step

        typer.echo(answers_step(paths, cfg, run_id, approve=approve_answers))
    elif done is None and not letter:
        typer.echo(f"Run {run_id} is finished: its resume and cover letter are built.")
    if letter and load_state(paths, run_id).command == "resume":
        typer.echo(f"To apply yourself: `helper assist {run_id}` opens the posting in your browser with a paste sheet.")
        from .hunt import on_resume_ready

        on_resume_ready(paths, cfg, run_id)


@app.command("cover-letter")
@guarded
def cover_letter(
    run_id: Annotated[str, typer.Argument(help="Run folder name under runs/.")],
    redo: Annotated[bool, typer.Option("--redo", help="Write the letter again from scratch.")] = False,
) -> None:
    """Write (or rewrite) the cover letter for a run whose resume is built."""
    from .config import load_config
    from .cover_letter import cover_letter_step
    from .errors import HelperError
    from .runs import load_state

    paths = _paths()
    state = load_state(paths, run_id)
    if not (state.steps.get("render") and state.steps["render"].status == "done"):
        raise HelperError(f"run {run_id} has no resume yet: finish it with `helper continue {run_id}` first")
    letter = cover_letter_step(paths, load_config(paths), run_id, redo=redo)
    typer.echo(letter or f"The cover letter for {run_id} is already built: {state.options['cover_letter']['pdf']}. "
                         "Use --redo to write it again.")


# --------------------------------------------------------------------------- milestone 6


@app.command("list")
@guarded
def list_(
    status: Annotated[Optional[str], typer.Option("--status", help="Only this status.")] = None,
    company: Annotated[Optional[str], typer.Option("--company", help="Company name contains this.")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many rows.")] = 30,
    all_rows: Annotated[bool, typer.Option("--all", help="Every row.")] = False,
) -> None:
    """List tracked applications, newest first."""
    from .config import load_config
    from .tracker import repo
    from .tracker.commands import format_list, open_db

    paths = _paths()
    conn = open_db(paths, load_config(paths))
    try:
        everything = repo.list_applications(conn, status=status, company=company)
        typer.echo(format_list(everything if all_rows else everything[:limit], len(everything)))
    finally:
        conn.close()


@app.command()
@guarded
def show(application_id: Annotated[int, typer.Argument(help="Application id from `helper list`.")]) -> None:
    """Show one application in full, with its status history."""
    from .config import load_config
    from .tracker import repo
    from .tracker.commands import format_show, open_db

    paths = _paths()
    conn = open_db(paths, load_config(paths))
    try:
        typer.echo(format_show(repo.get(conn, application_id), repo.history(conn, application_id), paths))
    finally:
        conn.close()


@app.command()
@guarded
def stats(weeks: Annotated[int, typer.Option("--weeks", min=1, max=104, help="Weeks to show.")] = 8) -> None:
    """Applications per week, by ATS, and by status."""
    from .config import load_config
    from .tracker import repo
    from .tracker.commands import format_stats, open_db

    from .autosubmit import readiness

    paths = _paths()
    cfg = load_config(paths)
    conn = open_db(paths, cfg)
    try:
        typer.echo(format_stats(repo.stats(conn, weeks=weeks)))
        typer.echo(readiness(conn, cfg.auto_submit_min_clean_runs, cfg.auto_submit_min_clean_runs_by_site))
    finally:
        conn.close()


@app.command()
@guarded
def export(
    csv: Annotated[bool, typer.Option("--csv", help="Write a CSV file (the only format).")] = True,
    out: Annotated[Optional[Path], typer.Option("--out", help="Output path (default exports/).")] = None,
) -> None:
    """Export every application to CSV."""
    from .config import load_config
    from .tracker.commands import default_export_path, export_csv, open_db

    paths = _paths()
    conn = open_db(paths, load_config(paths))
    try:
        path, n = export_csv(conn, out or default_export_path(paths))
    finally:
        conn.close()
    typer.echo(f"Wrote {n} application(s) to {path}")


@app.command()
@guarded
def mark(
    application_id: Annotated[int, typer.Argument(help="Application id from `helper list`.")],
    status: Annotated[str, typer.Argument(help="applied, oa, interview, offer, rejected, and so on.")],
    note: Annotated[Optional[str], typer.Option("--note", help="Note for the status history.")] = None,
    on: Annotated[Optional[str], typer.Option("--date", help="Date applied (YYYY-MM-DD), default today.")] = None,
) -> None:
    """Set an application's status. Sent applications are added to or updated in Notion right away."""
    from .config import load_config
    from .tracker import repo
    from .tracker.commands import open_db, push_one

    paths = _paths()
    conn = open_db(paths, load_config(paths))
    try:
        before = repo.get(conn, application_id)["status"]
        try:
            row = repo.set_status(conn, application_id, status, note=note, applied_on=on)
        except ValueError:
            raise HelperError(f"--date must look like 2026-11-14, not {on!r}") from None
        conn.commit()
        typer.echo(f"#{row['id']} {row['company']}, {row['role_title']}: {before} -> {row['status']}"
                   + (f" (applied {row['date_applied']})" if row["date_applied"] else ""))
        typer.echo(push_one(paths, conn, application_id))
        from .hunt import on_marked

        on_marked(paths, conn, application_id, row["status"])
    finally:
        conn.close()


@app.command("sync-notion")
@guarded
def sync_notion(
    check: Annotated[bool, typer.Option("--check", help="Only compare data/notion_mapping.yaml with the tracker.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change; write nothing.")] = False,
) -> None:
    """Send pending applications to Notion and bring back your edits (the first run imports your tracker)."""
    from .config import load_config
    from .tracker.commands import notion_configured, open_db
    from .tracker.notion import check_mapping, client_from_env, format_report, load_mapping, sync

    paths = _paths()
    if not notion_configured(paths):
        raise HelperError("Notion isn't set up: put NOTION_TOKEN and NOTION_DATABASE_ID in .env (see .env.example)")
    mapping = load_mapping(paths)
    api = client_from_env(paths, mapping)
    if check:
        problems = check_mapping(api.data_source(mapping.data_source_id), mapping)
        if problems:
            raise HelperError("data/notion_mapping.yaml doesn't match your tracker:\n  " + "\n  ".join(problems))
        typer.echo(f"data/notion_mapping.yaml matches your tracker: {', '.join(mapping.columns.model_dump().values())}; "
                   f"untouched: {', '.join(mapping.never_touch)}.")
        return
    conn = open_db(paths, load_config(paths))
    try:
        typer.echo(format_report(sync(conn, api, mapping, paths.root, dry_run=dry_run), mapping))
    finally:
        conn.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
