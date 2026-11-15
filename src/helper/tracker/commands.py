"""What `helper list`, `show`, `stats`, `export`, `mark` and `sync-notion` print (spec 11)."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Optional

from ..config import Paths, env_value
from ..errors import HelperError
from ..models import Config
from . import repo
from .db import APPLICATION_COLUMNS, LOCAL_ONLY_STATUSES, connect, init_db


def open_db(paths: Paths, cfg: Config) -> sqlite3.Connection:
    db = paths.db(cfg)
    init_db(db)  # creates or migrates
    return connect(db)


def notion_state(row: sqlite3.Row) -> str:
    if row["notion_page_id"] and row["notion_synced"]:
        return "synced"
    if row["status"] in LOCAL_ONLY_STATUSES and not row["notion_page_id"]:
        return "-"
    return "pending"


def _cut(text: Optional[str], width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 3] + "..."


def format_list(rows: list[sqlite3.Row], total: int) -> str:
    if not rows:
        return "No applications yet."
    lines = [f"{'id':>5}  {'applied':<10}  {'status':<20}  {'company':<22}  {'role':<44}  {'ats':<15}  notion"]
    for r in rows:
        lines.append(f"{r['id']:>5}  {(r['date_applied'] or '')[:10]:<10}  {r['status']:<20}  {_cut(r['company'], 22):<22}  "
                     f"{_cut(r['role_title'], 44):<44}  {(r['ats'] or ''):<15}  {notion_state(r)}")
    if total > len(rows):
        lines.append(f"... {len(rows)} of {total}. Use --limit or --all, --status or --company.")
    return "\n".join(lines)


def format_show(row: sqlite3.Row, history: list[sqlite3.Row], paths: Paths) -> str:
    lines = [f"#{row['id']} {row['company']}: {row['role_title']}"]
    for key in APPLICATION_COLUMNS[1:]:
        value = row[key]
        if value in (None, "") or key in ("company", "role_title"):
            continue
        if key in ("projects_used", "flags"):
            try:
                value = ", ".join(json.loads(value)) if key == "projects_used" else "\n      ".join(json.loads(value))
            except (ValueError, TypeError):
                pass
        if key == "notion_page_id":
            value = f"{value}  (https://www.notion.so/{str(value).replace('-', '')})"
        lines.append(f"  {key}: {value}")
    if row["run_id"]:
        lines.append(f"  run folder: {paths.runs.relative_to(paths.root) / row['run_id']}")
    lines.append(f"  notion: {notion_state(row)}")
    lines.append("  history:")
    lines += [f"    {h['changed_at']}  {h['status']}{'  (' + h['note'] + ')' if h['note'] else ''}" for h in history]
    return "\n".join(lines)


def format_stats(stats: dict[str, Any]) -> str:
    lines = [f"Applications: {stats['total']} tracked, {stats['sent']} sent"]
    lines.append("Sent per week (week starting):")
    peak = max(stats["per_week"].values() or [0]) or 1
    for week, n in stats["per_week"].items():
        lines.append(f"  {week}  {n:>4}  {'#' * round(30 * n / peak)}")
    lines.append("By ATS (sent): " + (", ".join(f"{k} {v}" for k, v in stats["by_ats"].items()) or "none"))
    lines.append("By status: " + (", ".join(f"{k} {v}" for k, v in stats["by_status"].items()) or "none"))
    return "\n".join(lines)


def export_csv(conn: sqlite3.Connection, path: Path) -> tuple[Path, int]:
    rows = conn.execute("SELECT * FROM applications ORDER BY id").fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(APPLICATION_COLUMNS)
        for r in rows:
            writer.writerow([r[c] for c in APPLICATION_COLUMNS])
    return path, len(rows)


def default_export_path(paths: Paths) -> Path:
    return paths.exports / f"applications_{date.today().isoformat()}.csv"


def notion_configured(paths: Paths) -> bool:
    return bool(env_value(paths, "NOTION_TOKEN") and env_value(paths, "NOTION_DATABASE_ID"))


def push_one(paths: Paths, conn: sqlite3.Connection, app_id: int) -> str:
    """Send one row to Notion right away (after `helper mark`). A failure leaves it pending."""
    from .notion import client_from_env, load_mapping, sync

    row = repo.get(conn, app_id)
    if row["status"] in LOCAL_ONLY_STATUSES and not row["notion_page_id"]:
        return "Notion: not sent (helper writes to Notion only after you apply)."
    if not notion_configured(paths):
        return "Notion: not set up, so this row stays notion_pending."
    mapping = load_mapping(paths)
    try:
        report = sync(conn, client_from_env(paths, mapping), mapping, paths.root, pull=False, only_ids=[app_id])
    except HelperError as exc:
        return f"Notion: not synced ({exc}); `helper sync-notion` retries it."
    if report.created:
        return "Notion: added to your tracker."
    if report.updated:
        return "Notion: updated in your tracker."
    if report.failed:
        return f"Notion: failed ({report.failed[0][1]}); `helper sync-notion` retries it."
    return "Notion: nothing to send."
