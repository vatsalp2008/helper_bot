from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from helper.tracker.db import (
    APPLICATION_COLUMNS,
    PIPELINE_STATUSES,
    SCHEMA_VERSION,
    connect,
    init_db,
    table_columns,
)

SPEC_COLUMNS = [
    "id", "company", "role_title", "location", "job_url", "job_id", "ats", "date_applied", "status",
    "resume_path", "resume_hash", "projects_used", "keyword_match_pct", "confirmation_screenshot",
    "notes", "flags", "notion_page_id", "notion_synced",
]


def _insert(conn: sqlite3.Connection, run_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO applications (company, role_title, run_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("Acme", "Intern", run_id, "2026-11-13T10:00:00", "2026-11-13T10:00:00"),
    )
    conn.commit()


def test_schema_has_every_spec_column_in_order(tmp_path: Path) -> None:
    db = tmp_path / "helper.db"
    assert init_db(db) is True
    conn = connect(db)
    try:
        columns = table_columns(conn, "applications")
        assert columns[: len(SPEC_COLUMNS)] == SPEC_COLUMNS
        assert columns == list(APPLICATION_COLUMNS)
        assert "status_history" in {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_init_is_idempotent_and_keeps_rows(tmp_path: Path) -> None:
    db = tmp_path / "helper.db"
    init_db(db)
    conn = connect(db)
    _insert(conn)
    conn.close()
    assert init_db(db) is False
    conn = connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
        row = conn.execute("SELECT status, notion_synced FROM applications").fetchone()
        assert (row["status"], row["notion_synced"]) == ("drafted", 0)  # new rows start drafted, notion_pending
    finally:
        conn.close()


def test_run_id_is_unique(tmp_path: Path) -> None:
    db = tmp_path / "helper.db"
    init_db(db)
    conn = connect(db)
    try:
        _insert(conn, "run-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, "run-1")
    finally:
        conn.close()


def test_pipeline_statuses_match_the_spec() -> None:
    assert PIPELINE_STATUSES == ("drafted", "filled", "applied", "submitted_unverified", "skipped", "failed")


V1_SCHEMA = """
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL, role_title TEXT NOT NULL, location TEXT,
    job_url TEXT, job_id TEXT, ats TEXT, date_applied TEXT, status TEXT NOT NULL DEFAULT 'drafted', resume_path TEXT,
    resume_hash TEXT, projects_used TEXT, keyword_match_pct REAL, confirmation_screenshot TEXT, notes TEXT, flags TEXT,
    notion_page_id TEXT, notion_synced INTEGER NOT NULL DEFAULT 0, run_id TEXT UNIQUE, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE status_history (id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER NOT NULL, status TEXT NOT NULL,
    changed_at TEXT NOT NULL, note TEXT);
PRAGMA user_version = 1;
"""


def test_a_version_1_database_is_migrated_and_keeps_its_rows(tmp_path: Path) -> None:
    db = tmp_path / "helper.db"
    conn = sqlite3.connect(db)
    conn.executescript(V1_SCHEMA)
    conn.execute("INSERT INTO applications (company, role_title, created_at, updated_at) VALUES ('Acme', 'Intern', 'x', 'x')")
    conn.commit()
    conn.close()
    assert init_db(db) is False
    conn = connect(db)
    try:
        assert table_columns(conn, "applications") == list(APPLICATION_COLUMNS)
        assert conn.execute("SELECT source FROM applications").fetchone()["source"] == "helper"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
        assert conn.execute("SELECT COUNT(*) FROM fill_runs").fetchone()[0] == 0  # added in version 3
        assert conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0  # added in version 4 (the job hunt)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'sync_state'").fetchone() is not None
    finally:
        conn.close()
    assert init_db(db) is False  # running it again changes nothing
