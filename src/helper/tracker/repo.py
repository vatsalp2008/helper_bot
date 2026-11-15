"""Applications in SQLite (spec 11): rows, status changes with history, listings, and stats."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date, timedelta
from typing import Any, Optional

from ..ats import detect_ats, job_id_from_url
from ..errors import HelperError
from .db import ALL_STATUSES, APPLICATION_COLUMNS, LOCAL_ONLY_STATUSES, SUBMITTED_STATUSES, now_iso

_WRITABLE = set(APPLICATION_COLUMNS) - {"id"}


def _check_fields(fields: dict[str, Any]) -> None:
    unknown = set(fields) - _WRITABLE
    if unknown:
        raise HelperError(f"unknown application fields: {', '.join(sorted(unknown))}")
    if "status" in fields and fields["status"] not in ALL_STATUSES:
        raise HelperError(f"'{fields['status']}' is not a status. Use one of: {', '.join(ALL_STATUSES)}")
    for key in ("projects_used", "flags"):
        if key in fields and isinstance(fields[key], (list, tuple)):
            fields[key] = json.dumps(list(fields[key]))


def insert(conn: sqlite3.Connection, *, history_note: str = "created", **fields: Any) -> int:
    fields = dict(fields)
    fields.setdefault("status", "drafted")
    fields.setdefault("created_at", now_iso())
    fields.setdefault("updated_at", fields["created_at"])
    if fields.get("job_url"):
        fields.setdefault("ats", detect_ats(fields["job_url"]))
        fields.setdefault("job_id", job_id_from_url(fields["job_url"]))
    _check_fields(fields)
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    cur = conn.execute(f"INSERT INTO applications ({cols}) VALUES ({marks})", list(fields.values()))
    app_id = int(cur.lastrowid or 0)
    conn.execute("INSERT INTO status_history (application_id, status, changed_at, note) VALUES (?, ?, ?, ?)",
                 (app_id, fields["status"], fields["updated_at"], history_note))
    return app_id


def update(conn: sqlite3.Connection, app_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields.setdefault("updated_at", now_iso())
    _check_fields(fields)
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE applications SET {sets} WHERE id = ?", [*fields.values(), app_id])


def get(conn: sqlite3.Connection, app_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    if row is None:
        raise HelperError(f"no application #{app_id} (see `helper list`)")
    return row  # type: ignore[no-any-return]


def find_by_run(conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM applications WHERE run_id = ?", (run_id,)).fetchone()  # type: ignore[no-any-return]


def history(conn: sqlite3.Connection, app_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM status_history WHERE application_id = ? ORDER BY id", (app_id,)).fetchall()


def set_status(conn: sqlite3.Connection, app_id: int, status: str, *, note: Optional[str] = None,
               applied_on: Optional[str] = None, from_notion: bool = False) -> sqlite3.Row:
    """Change a status and record it. A status that has to reach Notion leaves the row notion_pending."""
    if status not in ALL_STATUSES:
        raise HelperError(f"'{status}' is not a status. Use one of: {', '.join(ALL_STATUSES)}")
    row = get(conn, app_id)
    changes: dict[str, Any] = {"status": status}
    if applied_on:
        date.fromisoformat(applied_on)  # raises ValueError on a bad date
        changes["date_applied"] = applied_on
    elif status in SUBMITTED_STATUSES and not row["date_applied"]:
        changes["date_applied"] = date.today().isoformat()
    if not from_notion and (status not in LOCAL_ONLY_STATUSES or row["notion_page_id"]):
        changes["notion_synced"] = 0
    update(conn, app_id, **changes)
    conn.execute("INSERT INTO status_history (application_id, status, changed_at, note) VALUES (?, ?, ?, ?)",
                 (app_id, status, now_iso(), note or ("from Notion" if from_notion else None)))
    return get(conn, app_id)


def list_applications(conn: sqlite3.Connection, *, status: Optional[str] = None, company: Optional[str] = None,
                      limit: Optional[int] = None) -> list[sqlite3.Row]:
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if company:
        where.append("company LIKE ?")
        args.append(f"%{company}%")
    sql = "SELECT * FROM applications" + (f" WHERE {' AND '.join(where)}" if where else "")
    sql += " ORDER BY COALESCE(date_applied, substr(created_at, 1, 10)) DESC, id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def stats(conn: sqlite3.Connection, weeks: int = 8, today: Optional[date] = None) -> dict[str, Any]:
    """Spec 11: per week, by ATS, by status. Weeks start on Monday; only sent applications count per week."""
    today = today or date.today()
    rows = conn.execute("SELECT status, ats, date_applied FROM applications").fetchall()
    monday = today - timedelta(days=today.weekday())
    per_week: dict[str, int] = {(monday - timedelta(weeks=i)).isoformat(): 0 for i in range(weeks - 1, -1, -1)}
    for r in rows:
        if r["status"] in SUBMITTED_STATUSES and r["date_applied"]:
            try:
                d = date.fromisoformat(r["date_applied"][:10])
            except ValueError:
                continue
            week = (d - timedelta(days=d.weekday())).isoformat()
            if week in per_week:
                per_week[week] += 1
    sent = [r for r in rows if r["status"] in SUBMITTED_STATUSES]
    return {
        "total": len(rows),
        "sent": len(sent),
        "per_week": per_week,
        "by_ats": dict(Counter((r["ats"] or "unknown") for r in sent).most_common()),
        "by_status": dict(Counter(r["status"] for r in rows).most_common()),
    }


def get_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO sync_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
