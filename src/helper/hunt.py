"""The unattended job hunt's list, kept in step with every run (Vatsal, 2026-11-16).

`helper find` adds what it found (ineligible postings as skipped, with the reason); a run started for a listed
posting is "applying"; a fill that stops for Vatsal is "needs_you" (its window stays open); a resume-only run is
"ready_for_you" with its `helper assist` command; a confirmed submit, or `helper mark <id> applied`, takes the posting
off the list. Every change rewrites exports/postings_queue.md. None of this ever stops a run: a list problem is
ignored and the run carries on.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import Paths
from .tracker import queue

QUEUE_MD = "postings_queue.md"
_STUCK_WORDS = ("yourself", "didn't accept", "CAPTCHA", "Paused", "verification", "ran out", "Only you can answer",
                "Fix it in the window", "can't find", "keeps returning", "helper stopped before")


def _open(paths: Paths, cfg: Any):  # type: ignore[no-untyped-def]
    from .tracker.commands import open_db

    return open_db(paths, cfg)


def refresh(paths: Paths, conn: Any) -> Path:
    return queue.write_markdown(conn, paths.exports / QUEUE_MD)


def _quiet(func):  # type: ignore[no-untyped-def]
    """List bookkeeping never breaks a run."""
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except Exception:
            return None
    return wrapper


# --------------------------------------------------------------------------- finding


PLACEHOLDER_COMPANIES = ("", "?", "unknown")


def named_job(paths: Paths, cfg: Any, job: Any, url: str, company: Optional[str] = None) -> Any:
    """The job with its employer named: `--company` when given, else the list's company when the posting page named
    none (UKG boards without a footer link came out "Unknown" and would have put that in the letter; Werner, AFL,
    Encore, 2026-11-20)."""
    if company:
        return job.model_copy(update={"company": company})
    if (job.company or "").strip().lower() not in PLACEHOLDER_COMPANIES:
        return job
    listed = _listed_company(paths, cfg, url)
    return job.model_copy(update={"company": listed}) if listed else job


@_quiet
def _listed_company(paths: Paths, cfg: Any, url: str) -> Optional[str]:
    conn = _open(paths, cfg)
    try:
        row = queue.find(conn, url)
        name = (row["company"] or "").strip() if row is not None else ""
        return name if name.lower() not in PLACEHOLDER_COMPANIES else None
    finally:
        conn.close()


def latest_find(paths: Paths) -> Optional[Path]:
    found = sorted(paths.runs.glob("find-*/candidates.json"))
    return found[-1] if found else None


def find_command(paths: Paths, cfg: Any, hours: float, limit: int, check: bool = True) -> str:
    from . import find

    conn = _open(paths, cfg)
    try:
        urls, ids = queue.known(conn)
        tracker = [r[0] for r in conn.execute("SELECT job_url FROM applications WHERE job_url IS NOT NULL")]
    finally:
        conn.close()
    found, sources = find.search(paths, hours, urls, ids, tracker)
    found.sort(key=lambda f: str(f.posted_at or ""), reverse=True)  # newest first...
    found.sort(key=lambda f: f.how != "helper fills")  # ...with the postings helper can fill ahead (a stable sort)
    if check:
        listed = {id(f): f.title for f in found}
        find.check_all(paths, cfg, found, limit=limit)
        for f in found:  # the posting's own title can show what the feed's didn't ("(Bachelors Summer 2027)")
            if f.eligibility and not f.eligibility.get("blockers") and not find.wanted(f.title, f.location, f.url):
                # ...but a page title that only leaves out the field the list named keeps the list's reading: Aon's
                # iCIMS page says "Early Careers: Health Specialist" for its Health Analytics Specialist (2026-11-23)
                if not find.title_blocked(f.title) and find.wanted(listed[id(f)], f.location, f.url):
                    continue
                f.eligibility["blockers"] = [f"not a fit for the search: {f.title}"]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = find.save(paths.runs / f"find-{stamp}", found)
    conn = _open(paths, cfg)
    try:
        for f in found:
            blockers = f.eligibility.get("blockers") or []
            if blockers:  # Vatsal, 2026-11-16: ineligible postings are skipped, with the reason on the list
                queue.add(conn, url=f.url, company=f.company, title=f.title, location=f.location, ats=f.ats,
                          source=f.source, posted_at=f.posted_at, job_id=f.job_id, status="skipped",
                          reason=f"ineligible: {blockers[0][:200]}")
        conn.commit()
        refresh(paths, conn)
    finally:
        conn.close()
    from .runs import rel

    return find.summary(found, sources, hours, rel(paths, path))


def add(paths: Paths, cfg: Any, urls: list[str], *, status: str = "queued", reason: Optional[str] = None) -> list[str]:
    """Put postings on the list, with their details from the latest `helper find` when it has them."""
    from .ats import clean_url

    meta: dict[str, dict[str, Any]] = {}
    if (latest := latest_find(paths)) is not None:
        meta = {clean_url(c["url"]): c for c in json.loads(latest.read_text(encoding="utf-8"))}
    out = []
    conn = _open(paths, cfg)
    try:
        for raw in urls:
            url = clean_url(raw)
            c = meta.get(url, {})
            new = queue.add(conn, url=url, company=c.get("company") or "?", title=c.get("title") or "?",
                            location=c.get("location"), ats=c.get("ats"), source=c.get("source"),
                            posted_at=c.get("posted_at"), job_id=c.get("job_id"), status=status, reason=reason)
            if new is None and status != "queued":
                queue.set_status(conn, url, status, reason)
            out.append(f"{'added' if new else 'already listed'}: {c.get('company') or url} | {c.get('title') or ''}")
        conn.commit()
        refresh(paths, conn)
    finally:
        conn.close()
    return out


# --------------------------------------------------------------------------- runs


@_quiet
def on_run_started(paths: Paths, cfg: Any, url: str, state: Any) -> None:
    """`helper apply` / `helper resume` on a listed posting: applying, or skipped when eligibility says skip."""
    conn = _open(paths, cfg)
    try:
        flags = (state.options.get("eligibility") or {}).get("flags") or []
        blockers = [f["message"] for f in flags if f.get("level") == "blocker"]
        if not queue.link_run(conn, url, state.run_id, state.application_id):
            return
        job_file = paths.runs / state.run_id / "job.json"
        if job_file.exists():  # a link Vatsal sent was listed as "?" (Akuna, Disney, 2026-11-18)
            job = json.loads(job_file.read_text(encoding="utf-8"))
            queue.fill_details(conn, url, company=job.get("company"), title=job.get("role_title"),
                               location=job.get("location"), ats=job.get("ats"), job_id=job.get("job_id"))
        if blockers:
            queue.set_status(conn, state.run_id, "skipped", f"ineligible: {blockers[0][:200]}")
        conn.commit()
        refresh(paths, conn)
    finally:
        conn.close()


@_quiet
def on_resume_ready(paths: Paths, cfg: Any, run_id: str) -> None:
    """A resume-only run (SmartRecruiters, Taleo...) is built: it's Vatsal's to submit."""
    conn = _open(paths, cfg)
    try:
        row = queue.find(conn, run_id)
        if row is not None and (row["ats"] or "") == "lever":  # Vatsal, 2026-11-17: Lever is applied to by hand
            queue.set_status(conn, run_id, "apply_manually", "Lever's bot check rejects submits from helper's window, "
                             f"so apply in your normal browser: run `helper assist {run_id}`")
            conn.commit()
            refresh(paths, conn)
        elif row is not None:
            queue.set_status(conn, run_id, "ready_for_you", f"helper can't submit here: run `helper assist {run_id}`")
            conn.commit()
            refresh(paths, conn)
    finally:
        conn.close()


@_quiet
def on_fill_started(paths: Paths, cfg: Any, run_id: str) -> None:
    """A fill (again) for a listed posting: it's being applied to."""
    conn = _open(paths, cfg)
    try:
        row = queue.find(conn, run_id)
        if row is not None and row["status"] != "applying":
            queue.set_status(conn, run_id, "applying")
            conn.commit()
            refresh(paths, conn)
    finally:
        conn.close()


@_quiet
def on_fill_message(paths: Paths, cfg: Any, run_id: str, text: str) -> None:
    """A fill stopped for Vatsal (CAPTCHA, a verification email, a field only he can answer): the window stays open."""
    if not any(w in text for w in _STUCK_WORDS):
        return
    conn = _open(paths, cfg)
    try:
        if queue.find(conn, run_id) is not None:
            lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
            # a heading with its list under it names what's waiting: "Only you can answer these; fill them in the
            # window:" alone left out the question (Wurl, 2026-11-20)
            reason = f"{lines[0]} {'; '.join(lines[1:])}" if lines[0].endswith(":") and len(lines) > 1 else lines[0]
            queue.set_status(conn, run_id, "needs_you", reason[:220])
            conn.commit()
            refresh(paths, conn)
    finally:
        conn.close()


@_quiet
def on_fill_outcome(paths: Paths, cfg: Any, run_id: str, application_id: Optional[int], outcome: str,
                    detail: str = "") -> None:
    conn = _open(paths, cfg)
    try:
        if outcome == "confirmed":
            queue.remove_sent(conn, run_id=run_id, application_id=application_id)
        elif outcome == "posting_closed" and queue.find(conn, run_id) is not None:  # HPE, 2026-11-19
            queue.set_status(conn, run_id, "skipped", detail or "posting closed; nothing was sent")
        elif outcome == "rejected" and queue.find(conn, run_id) is not None:  # Vatsal, 2026-11-17: apply manually
            queue.set_status(conn, run_id, "apply_manually", "not sent: the site rejected the submit from helper's "
                             f"window. Apply in your normal browser (`helper assist {run_id}`), then "
                             f"`helper mark {application_id} applied`")
        elif queue.find(conn, run_id) is not None:
            reason = {"unverified": "Submit was clicked but no confirmation showed: check it was sent, then "
                                    f"`helper mark {application_id} applied`",
                      "closed": f"not submitted: the window was closed. `helper fill {run_id} --keep-open` opens it "
                                "again",
                      "timeout": "not submitted: the wait ran out"}.get(outcome, f"not submitted ({outcome})")
            queue.set_status(conn, run_id, "needs_you", reason)
        conn.commit()
        refresh(paths, conn)
    finally:
        conn.close()


@_quiet
def on_marked(paths: Paths, conn: Any, application_id: int, status: str) -> None:
    """`helper mark <id> applied` (or any sent status): the posting leaves the list."""
    from .tracker.db import SUBMITTED_STATUSES

    if status not in SUBMITTED_STATUSES:
        return
    # a posting listed before its application existed carries only the run id (Airbnb, apexanalytix, 2026-11-18)
    row = conn.execute("SELECT run_id FROM applications WHERE id = ?", (application_id,)).fetchone()
    if queue.remove_sent(conn, run_id=row[0] if row else None, application_id=application_id):
        conn.commit()
        refresh(paths, conn)
