"""helper command line (spec section 12)."""

from __future__ import annotations

import functools
from enum import Enum
from pathlib import Path
from typing import Annotated, Callable, Optional, TypeVar

import typer

from . import __version__
from .config import Paths, find_root
from .errors import EXIT_ERROR, HelperError, NotBuiltYet

app = typer.Typer(
    help="helper: one job URL in; a tailored resume, a filled form, and a tracked application out.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)

F = TypeVar("F", bound=Callable[..., object])


def guarded(func: F) -> F:
    """Print HelperError messages plainly and exit with the error's exit code."""

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> object:
        try:
            return func(*args, **kwargs)
        except HelperError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(exc.exit_code) from None

    return wrapper  # type: ignore[return-value]


def _paths() -> Paths:
    return Paths(find_root())


def _not_built(command: str, milestone: int, name: str) -> None:
    raise NotBuiltYet(f"`helper {command}` arrives in milestone {milestone} ({name}).")


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

    if mode == SubmitMode.auto:
        raise NotBuiltYet("Auto-submit arrives in milestone 9. Review mode fills the form and stops before Submit.")
    paths = _paths()
    cfg = load_config(paths)
    job = fetch_job(url, jd_text=jd_file.read_text(encoding="utf-8") if jd_file else None)
    if dry_run:
        from .apply import preview

        typer.echo(preview(paths, cfg, job))
        return
    started = start_run(paths, cfg, job, command="apply", projects=projects, include_experience=include_experience,
                        grad_date=grad_date)
    typer.echo(format_started(paths, started))
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

        started = start_run(paths, load_config(paths), fetch_job(url), command="resume", projects=projects,
                            include_experience=include_experience, grad_date=grad_date)
    else:
        if not company or not role:
            raise HelperError("--company and --role are required with --jd-file")
        started = start_resume(
            paths, load_config(paths), jd_text=jd_file.read_text(encoding="utf-8"), company=company, role=role,
            url=url, location=location, projects=projects, include_experience=include_experience, grad_date=grad_date,
        )
    typer.echo(format_started(paths, started))
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
) -> None:
    """Fill the application form in a browser window, stop before Submit, and log the result after you submit."""
    from .apply import fill_run
    from .config import load_config
    from .errors import EXIT_NEEDS_USER

    paths = _paths()
    outcome = fill_run(paths, load_config(paths), run_id, dry_run=dry_run, wait_minutes=wait,
                       headless=True if headless else None)
    typer.echo(outcome.message)
    if outcome.status == "filled":
        raise typer.Exit(EXIT_NEEDS_USER)


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

    paths = _paths()
    cfg = load_config(paths)
    done = continue_run(paths, cfg, run_id, build_anyway=build_anyway)
    if done is not None:
        typer.echo(format_finished(paths, done))
    from .runs import load_state

    if load_state(paths, run_id).command == "apply":
        from .apply import answers_step

        typer.echo(answers_step(paths, cfg, run_id, approve=approve_answers))
    elif done is None:
        typer.echo(f"Run {run_id} is finished: its resume is built.")


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

    paths = _paths()
    conn = open_db(paths, load_config(paths))
    try:
        typer.echo(format_stats(repo.stats(conn, weeks=weeks)))
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
