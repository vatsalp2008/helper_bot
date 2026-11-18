"""The maintained list of postings for the unattended job hunt (Vatsal, 2026-11-16).

`helper find` adds what it found; each run moves its posting along; a confirmed submit removes it. What's left is what
Vatsal should look at when he's back: windows that stopped for him (needs_you), postings only he can submit
(ready_for_you, with their `helper assist` command), work in progress, and what was skipped and why.
`exports/postings_queue.md` is rewritten after every change.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..ats import clean_url
from .db import now_iso

STATUSES = ("queued", "applying", "needs_you", "apply_manually", "ready_for_you", "skipped")
_ORDER = {"needs_you": 0, "apply_manually": 1, "ready_for_you": 2, "applying": 3, "queued": 4, "skipped": 5}
_HEADINGS = {
    "needs_you": "Needs you (the window is still open)",
    # Vatsal, 2026-11-17: postings he applies to by hand on the company's site (helper's try didn't go through)
    "apply_manually": "Apply manually (on the company's own site)",
    "ready_for_you": "Ready for you (helper can't submit these; the resume and letter are built)",
    "applying": "Applying now",
    "queued": "Queued",
    "skipped": "Skipped",
}
_NOTE_KEY = "hunt_last_session"

def add(conn: sqlite3.Connection, *, url: str, company: str, title: str, location: Optional[str] = None,
        ats: Optional[str] = None, source: Optional[str] = None, posted_at: Optional[str] = None,
        job_id: Optional[str] = None, status: str = "queued", reason: Optional[str] = None) -> Optional[int]:
    """The new row's id, or None when the posting is already on the list."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    url = clean_url(url)
    if conn.execute("SELECT 1 FROM postings WHERE url = ? OR (job_id IS NOT NULL AND job_id = ? AND company = ?)",
                    (url, job_id, company)).fetchone():
        return None
    now = now_iso()
    cur = conn.execute(
        "INSERT INTO postings (url, job_id, company, title, location, ats, source, posted_at, found_at, status, reason, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (url, job_id, company, title, location, ats, source, posted_at, now, status, reason, now))
    return int(cur.lastrowid or 0)


def _where(key: str) -> tuple[str, tuple[Any, ...]]:
    """A row by id, link or run id."""
    if key.isdigit():
        return "id = ?", (int(key),)
    if key.startswith("http"):
        return "url = ?", (key,)
    return "run_id = ?", (key,)


def find(conn: sqlite3.Connection, key: str) -> Optional[sqlite3.Row]:
    clause, args = _where(key)
    return conn.execute(f"SELECT * FROM postings WHERE {clause}", args).fetchone()


def set_status(conn: sqlite3.Connection, key: str, status: str, reason: Optional[str] = None, *,
               run_id: Optional[str] = None, application_id: Optional[int] = None) -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; one of {', '.join(STATUSES)}")
    clause, args = _where(key)
    cur = conn.execute(
        f"UPDATE postings SET status = ?, reason = ?, run_id = COALESCE(?, run_id), "
        f"application_id = COALESCE(?, application_id), updated_at = ? WHERE {clause}",
        (status, reason, run_id, application_id, now_iso(), *args))
    return cur.rowcount > 0


def link_run(conn: sqlite3.Connection, url: str, run_id: str, application_id: Optional[int] = None) -> bool:
    """A run started for a listed posting: it's being applied to now."""
    cur = conn.execute("UPDATE postings SET run_id = ?, application_id = COALESCE(?, application_id), "
                       "status = 'applying', reason = NULL, updated_at = ? WHERE url = ?",
                       (run_id, application_id, now_iso(), clean_url(url)))
    return cur.rowcount > 0


def remove(conn: sqlite3.Connection, key: str) -> bool:
    clause, args = _where(key)
    return conn.execute(f"DELETE FROM postings WHERE {clause}", args).rowcount > 0


def remove_sent(conn: sqlite3.Connection, *, run_id: Optional[str] = None, application_id: Optional[int] = None) -> bool:
    """The application went out: the posting leaves the list (Vatsal: 'remove it once you're done applying')."""
    cur = conn.execute("DELETE FROM postings WHERE (run_id IS NOT NULL AND run_id = ?) OR "
                       "(application_id IS NOT NULL AND application_id = ?)", (run_id, application_id))
    return cur.rowcount > 0


SKIPPED_SHOWN_HOURS = 48  # skipped postings stay in the database (searches don't bring them back) but leave the view


def rows(conn: sqlite3.Connection, statuses: Optional[Iterable[str]] = None, *,
         skipped_hours: Optional[float] = None) -> list[sqlite3.Row]:
    wanted = tuple(statuses or STATUSES)
    found = conn.execute(f"SELECT * FROM postings WHERE status IN ({','.join('?' * len(wanted))})", wanted).fetchall()
    if skipped_hours is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=skipped_hours)).isoformat()
        found = [r for r in found if r["status"] != "skipped" or _utc(r["updated_at"]) >= cutoff]
    return sorted(found, key=lambda r: (_ORDER.get(r["status"], 9), r["updated_at"] or ""), reverse=False)


def _utc(stamp: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(stamp or "").astimezone(timezone.utc).isoformat()
    except ValueError:
        return ""


def known(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """Links and job ids already on the list or already applied to, so a search doesn't bring them back."""
    urls: set[str] = set()
    ids: set[str] = set()
    for r in conn.execute("SELECT url, job_id FROM postings"):
        urls.add(r[0])
        if r[1]:
            ids.add(str(r[1]))
    for r in conn.execute("SELECT job_url, job_id FROM applications"):
        if r[0]:
            urls.add(r[0])
        if r[1]:
            ids.add(str(r[1]))
    return urls, ids


def set_note(conn: sqlite3.Connection, text: str) -> None:
    conn.execute("INSERT INTO sync_state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (_NOTE_KEY, f"{now_iso()[:16].replace('T', ' ')}: {text}"))


def note(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (_NOTE_KEY,)).fetchone()
    return row[0] if row else ""


def as_dicts(conn: sqlite3.Connection, skipped_hours: Optional[float] = None) -> list[dict[str, Any]]:
    return [dict(r) for r in rows(conn, skipped_hours=skipped_hours)]


_PAGE_FIELDS = ("company", "title", "location", "url", "ats", "status", "reason", "run_id", "posted_at", "updated_at")


def page_doc(conn: sqlite3.Connection) -> dict[str, Any]:
    """The list's web page record (`hunt/list`): postings and statuses only, never contact details."""
    shown = [{k: r[k] for k in _PAGE_FIELDS} for r in rows(conn, skipped_hours=SKIPPED_SHOWN_HOURS)]
    return {"rows": shown, "note": note(conn), "updated_at": now_iso()}


def render_markdown(conn: sqlite3.Connection) -> str:
    listed = rows(conn, skipped_hours=SKIPPED_SHOWN_HOURS)
    lines = ["# Postings list", ""]
    if note(conn):
        lines += [f"Last session: {note(conn)}", ""]
    if not listed:
        return "\n".join(lines + ["Nothing on the list: every posting found so far was applied to."]) + "\n"
    for status in sorted(_HEADINGS, key=lambda s: _ORDER[s]):
        group = [r for r in listed if r["status"] == status]
        if not group:
            continue
        lines += [f"## {_HEADINGS[status]} ({len(group)})", ""]
        for r in group:
            where = f", {r['location']}" if r["location"] else ""
            lines.append(f"- **{r['company']}**: {r['title']}{where} ({r['ats'] or 'site'}) <{r['url']}>")
            extra = []
            if r["reason"]:
                extra.append(r["reason"])
            if r["run_id"]:
                extra.append(f"run `{r['run_id']}`")
            if r["posted_at"]:
                extra.append(f"posted {str(r['posted_at'])[:16].replace('T', ' ')}")
            if extra:
                lines.append(f"  - {'; '.join(extra)}")
        lines.append("")
    return "\n".join(lines)


def write_markdown(conn: sqlite3.Connection, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(conn), encoding="utf-8")
    return path
