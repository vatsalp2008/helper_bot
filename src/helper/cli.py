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


@app.command()
@guarded
def apply(
    url: Annotated[str, typer.Argument(help="Job posting URL.")],
    projects: Annotated[Optional[int], typer.Option("--projects", min=1, help="Projects on the resume.")] = None,
    mode: Annotated[Optional[SubmitMode], typer.Option("--mode", help="review or auto.")] = None,
    include_experience: Annotated[
        Optional[list[str]], typer.Option("--include-experience", help="Add an off-by-default experience entry.")
    ] = None,
    jd_file: Annotated[
        Optional[Path], typer.Option("--jd-file", exists=True, dir_okay=False, help="Use this job description text.")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Fill nothing; show what would be filled.")] = False,
) -> None:
    """Full run: intake, eligibility, tailored resume, form fill, submit (review mode stops first), log."""
    _not_built("apply", 7, "Greenhouse adapter")


@app.command()
@guarded
def resume(
    url: Annotated[Optional[str], typer.Argument(help="Job posting URL (optional with --jd-file).")] = None,
    jd_file: Annotated[
        Optional[Path], typer.Option("--jd-file", exists=True, dir_okay=False, help="Use this job description text.")
    ] = None,
    company: Annotated[Optional[str], typer.Option("--company", help="Company name when there is no URL.")] = None,
    role: Annotated[Optional[str], typer.Option("--role", help="Role title when there is no URL.")] = None,
    projects: Annotated[Optional[int], typer.Option("--projects", min=1, help="Projects on the resume.")] = None,
    include_experience: Annotated[
        Optional[list[str]], typer.Option("--include-experience", help="Add an off-by-default experience entry.")
    ] = None,
) -> None:
    """Intake, tailor and render only: produce the validated one-page PDF."""
    _not_built("resume", 4, "Tailor pipeline")


@app.command()
@guarded
def fill(
    run_id: Annotated[str, typer.Argument(help="Run folder name under runs/.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Fill nothing; show what would be filled.")] = False,
) -> None:
    """Fill the application form for an existing run."""
    _not_built("fill", 7, "Greenhouse adapter")


@app.command("continue")
@guarded
def continue_(run_id: Annotated[str, typer.Argument(help="Run folder name under runs/.")]) -> None:
    """Resume a run after Claude wrote the file it was waiting for."""
    _not_built("continue", 4, "Tailor pipeline")


@app.command("list")
@guarded
def list_() -> None:
    """List tracked applications."""
    _not_built("list", 6, "Tracker")


@app.command()
@guarded
def show(application_id: Annotated[int, typer.Argument(help="Application id from `helper list`.")]) -> None:
    """Show one application in full."""
    _not_built("show", 6, "Tracker")


@app.command()
@guarded
def stats() -> None:
    """Applications per week, by ATS, and by status."""
    _not_built("stats", 6, "Tracker")


@app.command()
@guarded
def export(
    csv: Annotated[bool, typer.Option("--csv", help="Write a CSV file.")] = True,
    out: Annotated[Optional[Path], typer.Option("--out", help="Output path (default exports/).")] = None,
) -> None:
    """Export applications to CSV."""
    _not_built("export", 6, "Tracker")


@app.command()
@guarded
def mark(
    application_id: Annotated[int, typer.Argument(help="Application id from `helper list`.")],
    status: Annotated[str, typer.Argument(help="interview, rejected, offer, and so on.")],
    note: Annotated[Optional[str], typer.Option("--note", help="Optional note for the status history.")] = None,
) -> None:
    """Set an application's status."""
    _not_built("mark", 6, "Tracker")


@app.command("sync-notion")
@guarded
def sync_notion() -> None:
    """Retry Notion sync for rows marked notion_pending."""
    _not_built("sync-notion", 6, "Tracker")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
