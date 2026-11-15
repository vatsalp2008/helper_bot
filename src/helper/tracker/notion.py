"""Notion sync (spec 11). Your tracker mirrors the applications you send; your hand edits flow back.

helper writes only the columns in data/notion_mapping.yaml, only for applications that were sent, and never creates a
column (Vatsal, 2026-11-14). SQLite stays the system of record: a failed sync keeps the row and leaves it
notion_pending, and `helper sync-notion` retries it.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol

import requests
from pydantic import Field

from ..config import Paths, env_value, load_yaml
from ..errors import HelperError
from ..models import StrictModel
from . import repo
from .db import LOCAL_ONLY_STATUSES, now_iso

API = "https://api.notion.com/v1"
ALLOWED_TYPES: dict[str, tuple[str, ...]] = {
    "role_title": ("title",),
    "company": ("rich_text", "select"),
    "job_url": ("url",),
    "location": ("rich_text", "select"),
    "resume_pdf": ("files",),
    "status": ("status", "select"),
    "date_applied": ("date",),
}


class NotionError(HelperError):
    """A Notion API call failed. The message never contains the token."""


class NotionColumns(StrictModel):
    role_title: str
    company: str
    job_url: str
    location: str
    resume_pdf: str
    status: str
    date_applied: str


class NotionMapping(StrictModel):
    """data/notion_mapping.yaml"""

    notion_version: str = "2025-09-03"
    data_source_id: str
    data_source_name: Optional[str] = None
    columns: NotionColumns
    status_to_notion: dict[str, Optional[str]]  # helper status -> Notion option; null keeps Notion's value
    status_from_notion: dict[str, str]  # Notion option -> helper status
    never_touch: list[str] = Field(default_factory=list)


def load_mapping(paths: Paths) -> NotionMapping:
    path = paths.data / "notion_mapping.yaml"
    if not path.is_file():
        raise HelperError("data/notion_mapping.yaml is missing")
    return NotionMapping.model_validate(load_yaml(path))


# --------------------------------------------------------------------------- the API


class NotionAPI(Protocol):
    def data_source(self, data_source_id: str) -> dict[str, Any]: ...
    def query(self, data_source_id: str, filter: Optional[dict[str, Any]] = None) -> Iterator[dict[str, Any]]: ...
    def create_page(self, data_source_id: str, properties: dict[str, Any]) -> dict[str, Any]: ...
    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]: ...
    def upload_pdf(self, path: Path) -> str: ...


class NotionClient:
    def __init__(self, token: str, version: str, session: Optional[Any] = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 3) -> None:
        self._token = token
        self.version = version
        self.session = session or requests.Session()
        self.sleep = sleep
        self.max_retries = max_retries

    def request(self, method: str, path: str, *, json_body: Optional[dict[str, Any]] = None,
                files: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}", "Notion-Version": self.version}
        if files is None:
            headers["Content-Type"] = "application/json"
        for attempt in range(self.max_retries + 1):
            try:
                r = self.session.request(method, f"{API}/{path}", headers=headers,
                                         json=json_body if files is None else None, files=files, timeout=30)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise NotionError(f"can't reach Notion ({type(exc).__name__})") from None
                self.sleep(2.0**attempt)
                continue
            if (r.status_code == 429 or r.status_code >= 500) and attempt < self.max_retries:
                self.sleep(float(r.headers.get("Retry-After") or 2.0**attempt))
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = {}
                raise NotionError(f"Notion said {r.status_code} {body.get('code', '')}: {body.get('message', '')}".strip())
            return r.json()  # type: ignore[no-any-return]
        raise NotionError("Notion kept failing")  # pragma: no cover

    def data_source(self, data_source_id: str) -> dict[str, Any]:
        return self.request("GET", f"data_sources/{data_source_id}")

    def query(self, data_source_id: str, filter: Optional[dict[str, Any]] = None) -> Iterator[dict[str, Any]]:
        cursor: Optional[str] = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if filter:
                body["filter"] = filter
            if cursor:
                body["start_cursor"] = cursor
            page = self.request("POST", f"data_sources/{data_source_id}/query", json_body=body)
            yield from page.get("results", [])
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")

    def create_page(self, data_source_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "pages", json_body={
            "parent": {"type": "data_source_id", "data_source_id": data_source_id}, "properties": properties})

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"pages/{page_id}", json_body={"properties": properties})

    def upload_pdf(self, path: Path) -> str:
        upload = self.request("POST", "file_uploads", json_body={"filename": path.name, "content_type": "application/pdf"})
        with path.open("rb") as fh:
            self.request("POST", f"file_uploads/{upload['id']}/send", files={"file": (path.name, fh, "application/pdf")})
        return str(upload["id"])


def client_from_env(paths: Paths, mapping: NotionMapping) -> NotionClient:
    token = env_value(paths, "NOTION_TOKEN")
    if not token:
        raise HelperError("NOTION_TOKEN isn't set in .env (see .env.example)")
    return NotionClient(token, mapping.notion_version)


# --------------------------------------------------------------------------- columns and values


def check_mapping(schema: dict[str, Any], mapping: NotionMapping) -> list[str]:
    """Everything wrong between the mapping file and the live tracker. helper never creates or renames columns."""
    props = schema.get("properties", {})
    problems = []
    for field_name, column in mapping.columns:
        prop = props.get(column)
        if prop is None:
            problems.append(f"column '{column}' (for {field_name}) isn't in your tracker. helper never creates "
                            "columns: rename it in data/notion_mapping.yaml or add the column yourself")
        elif prop["type"] not in ALLOWED_TYPES[field_name]:
            problems.append(f"column '{column}' is {prop['type']}; {field_name} needs {' or '.join(ALLOWED_TYPES[field_name])}")
        if column in mapping.never_touch:
            problems.append(f"column '{column}' is both mapped and in never_touch")
    status = props.get(mapping.columns.status)
    if status and status["type"] in ("status", "select"):
        options = {o["name"] for o in status[status["type"]].get("options", [])}
        wanted = {v for v in mapping.status_to_notion.values() if v} | set(mapping.status_from_notion)
        for name in sorted(wanted - options):
            problems.append(f"Status option '{name}' isn't in your tracker (it has: {', '.join(sorted(options))})")
    return problems


def _text(value: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": value[:2000]}}]


def _value(kind: str, value: str) -> dict[str, Any]:
    if kind == "title":
        return {"title": _text(value)}
    if kind == "rich_text":
        return {"rich_text": _text(value)}
    if kind in ("select", "status"):
        return {kind: {"name": value[:100]}}
    raise NotionError(f"can't write a {kind} property")


def plain(prop: Optional[dict[str, Any]]) -> Optional[str]:
    if not prop:
        return None
    kind = prop.get("type")
    value = prop.get(kind) if kind else None
    if kind in ("title", "rich_text"):
        text = "".join(part.get("plain_text", "") for part in value or []).strip()
        return text or None
    if kind in ("select", "status"):
        return value.get("name") if value else None
    if kind == "url":
        return value or None
    if kind == "date":
        return (value or {}).get("start") or None
    if kind == "files":
        return value[0].get("name") if value else None
    return None


def page_properties(row: sqlite3.Row, mapping: NotionMapping, types: dict[str, str], *, creating: bool,
                    upload_id: Optional[str] = None, resume_name: Optional[str] = None) -> dict[str, Any]:
    """The values helper writes. A new page gets all seven columns; an existing page gets status, date and resume."""
    c = mapping.columns
    props: dict[str, Any] = {}
    if creating:
        props[c.role_title] = {"title": _text(row["role_title"])}
        props[c.company] = _value(types[c.company], row["company"])
        if row["job_url"]:
            props[c.job_url] = {"url": row["job_url"]}
        if row["location"]:
            props[c.location] = _value(types[c.location], row["location"])
    status_name = mapping.status_to_notion.get(row["status"])
    if status_name:
        props[c.status] = _value(types[c.status], status_name)
    if row["date_applied"]:
        props[c.date_applied] = {"date": {"start": row["date_applied"][:10]}}
    if upload_id:
        props[c.resume_pdf] = {"files": [{"type": "file_upload", "file_upload": {"id": upload_id},
                                          "name": resume_name or "resume.pdf"}]}
    return props


def row_from_page(page: dict[str, Any], mapping: NotionMapping) -> dict[str, Any]:
    p, c = page.get("properties", {}), mapping.columns
    applied = plain(p.get(c.date_applied))
    notion_status = plain(p.get(c.status))
    status = mapping.status_from_notion.get(notion_status or "") or ("applied" if applied else "drafted")
    resume = plain(p.get(c.resume_pdf))
    return {
        "company": plain(p.get(c.company)) or "(no company)",
        "role_title": plain(p.get(c.role_title)) or "(untitled)",
        "job_url": plain(p.get(c.job_url)),
        "location": plain(p.get(c.location)),
        "date_applied": applied[:10] if applied else None,
        "status": status,
        "notion_page_id": page["id"],
        "notion_last_edited": page.get("last_edited_time"),
        "created_at": page.get("created_time") or now_iso(),
        "notes": f"Resume in Notion: {resume}" if resume else None,
    }


def _norm_id(page_id: Optional[str]) -> str:
    return (page_id or "").replace("-", "").lower()


# --------------------------------------------------------------------------- sync


@dataclass
class SyncReport:
    data_source: str
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    imported: int = 0
    status_changes: list[tuple[int, str, str]] = field(default_factory=list)  # (id, from, to)
    dry_run: bool = False


def sync(conn: sqlite3.Connection, api: NotionAPI, mapping: NotionMapping, root: Path, *, push: bool = True,
         pull: bool = True, dry_run: bool = False, only_ids: Optional[list[int]] = None) -> SyncReport:
    schema = api.data_source(mapping.data_source_id)
    problems = check_mapping(schema, mapping)
    if problems:
        raise HelperError("Your Notion tracker doesn't match data/notion_mapping.yaml:\n  " + "\n  ".join(problems))
    title = "".join(t.get("plain_text", "") for t in schema.get("title", [])) or mapping.data_source_name or "tracker"
    types = {name: prop["type"] for name, prop in schema["properties"].items()}
    report = SyncReport(data_source=title, dry_run=dry_run)
    if push:
        _push(conn, api, mapping, types, root, report, only_ids)
    if pull:
        _pull(conn, api, mapping, report)
    return report


def _push(conn: sqlite3.Connection, api: NotionAPI, mapping: NotionMapping, types: dict[str, str], root: Path,
          report: SyncReport, only_ids: Optional[list[int]]) -> None:
    marks = ", ".join("?" for _ in LOCAL_ONLY_STATUSES)
    rows = conn.execute(f"SELECT * FROM applications WHERE notion_synced = 0 AND status NOT IN ({marks}) ORDER BY id",
                        LOCAL_ONLY_STATUSES).fetchall()
    for row in rows:
        if only_ids is not None and row["id"] not in only_ids:
            continue
        creating = row["notion_page_id"] is None
        pdf = (root / row["resume_path"]) if row["resume_path"] else None
        needs_pdf = bool(pdf and pdf.is_file() and row["resume_hash"] and row["resume_hash"] != row["notion_resume_hash"])
        if report.dry_run:
            (report.created if creating else report.updated).append(row["id"])
            continue
        try:
            upload_id = api.upload_pdf(pdf) if needs_pdf and pdf else None
            props = page_properties(row, mapping, types, creating=creating, upload_id=upload_id,
                                    resume_name=pdf.name if pdf else None)
            page = (api.create_page(mapping.data_source_id, props) if creating
                    else api.update_page(row["notion_page_id"], props))
        except NotionError as exc:
            repo.update(conn, row["id"], notion_synced=0, notion_error=str(exc))
            conn.commit()
            report.failed.append((row["id"], str(exc)))
            continue
        repo.update(conn, row["id"], notion_synced=1, notion_page_id=page["id"], notion_error=None,
                    notion_last_edited=page.get("last_edited_time"),
                    notion_resume_hash=row["resume_hash"] if upload_id else row["notion_resume_hash"])
        conn.commit()
        (report.created if creating else report.updated).append(row["id"])


def _pull(conn: sqlite3.Connection, api: NotionAPI, mapping: NotionMapping, report: SyncReport) -> None:
    """Import pages helper hasn't seen, and take status changes you made by hand in Notion."""
    since = repo.get_state(conn, "notion_last_pull")
    started = datetime.now(timezone.utc).replace(microsecond=0)
    query_filter = None
    if since:
        after = (datetime.fromisoformat(since) - timedelta(minutes=2)).isoformat()
        query_filter = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": after}}
    known = {_norm_id(r["notion_page_id"]): r for r in
             conn.execute("SELECT * FROM applications WHERE notion_page_id IS NOT NULL").fetchall()}
    for page in api.query(mapping.data_source_id, query_filter):
        if page.get("in_trash") or page.get("archived"):
            continue
        data = row_from_page(page, mapping)
        row = known.get(_norm_id(page["id"]))
        if row is None:
            if not report.dry_run:
                repo.insert(conn, source="notion", notion_synced=1, history_note="imported from Notion", **data)
            report.imported += 1
            continue
        if row["notion_synced"] == 0 or row["notion_last_edited"] == data["notion_last_edited"]:
            continue  # a local change waits to be pushed, or the page hasn't changed
        current_name = mapping.status_to_notion.get(row["status"])
        notion_name = next((k for k, v in mapping.status_from_notion.items() if v == data["status"]), None)
        changes: dict[str, Any] = {"notion_last_edited": data["notion_last_edited"]}
        if row["source"] == "notion":
            changes.update({k: data[k] for k in ("company", "role_title", "job_url", "location") if data[k]})
        if data["date_applied"] and data["date_applied"] != row["date_applied"]:
            changes["date_applied"] = data["date_applied"]
        status_changed = current_name is not None and notion_name is not None and notion_name != current_name
        if report.dry_run:
            if status_changed:
                report.status_changes.append((row["id"], row["status"], data["status"]))
            continue
        repo.update(conn, row["id"], **changes)
        if status_changed:
            repo.set_status(conn, row["id"], data["status"], from_notion=True)
            report.status_changes.append((row["id"], row["status"], data["status"]))
    if not report.dry_run:
        repo.set_state(conn, "notion_last_pull", started.isoformat())
        conn.commit()


def format_report(report: SyncReport, mapping: NotionMapping) -> str:
    verb = "Would send" if report.dry_run else "Sent"
    lines = [f"Notion sync: {report.data_source}{' (dry run, nothing written)' if report.dry_run else ''}"]
    lines.append(f"  Columns: all {len(mapping.columns.model_dump())} mapped columns found; "
                 f"untouched: {', '.join(mapping.never_touch) or 'none'}")
    lines.append(f"  {verb} to Notion: {len(report.created)} new, {len(report.updated)} updated")
    lines.append(f"  From Notion: {report.imported} imported, {len(report.status_changes)} status change(s)")
    for app_id, old, new in report.status_changes[:20]:
        lines.append(f"    #{app_id}: {old} -> {new}")
    if report.failed:
        lines.append(f"  Still pending ({len(report.failed)}), retried by `helper sync-notion`:")
        lines += [f"    #{app_id}: {error}" for app_id, error in report.failed]
    return "\n".join(lines)
