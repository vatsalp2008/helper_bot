"""SQLite schema (spec section 11). SQLite is the system of record."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

# Statuses a run sets (spec 11).
PIPELINE_STATUSES = ("drafted", "filled", "applied", "submitted_unverified", "skipped", "failed")
# Statuses you set later with `helper mark <id> <status>`.
OUTCOME_STATUSES = (
    "oa", "phone_screen", "interview", "offer", "accepted", "declined", "rejected", "withdrawn", "ghosted",
)
ALL_STATUSES = PIPELINE_STATUSES + OUTCOME_STATUSES

# Spec 11 columns in order, then build additions (run_id, timestamps).
APPLICATION_COLUMNS = (
    "id", "company", "role_title", "location", "job_url", "job_id", "ats", "date_applied", "status",
    "resume_path", "resume_hash", "projects_used", "keyword_match_pct", "confirmation_screenshot",
    "notes", "flags", "notion_page_id", "notion_synced", "run_id", "created_at", "updated_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    company                 TEXT NOT NULL,
    role_title              TEXT NOT NULL,
    location                TEXT,
    job_url                 TEXT,
    job_id                  TEXT,
    ats                     TEXT,
    date_applied            TEXT,                       -- ISO date of the submit
    status                  TEXT NOT NULL DEFAULT 'drafted',
    resume_path             TEXT,
    resume_hash             TEXT,                       -- sha256 of the submitted PDF
    projects_used           TEXT,                       -- JSON array of project ids
    keyword_match_pct       REAL,
    confirmation_screenshot TEXT,
    notes                   TEXT,
    flags                   TEXT,                       -- JSON array of strings
    notion_page_id          TEXT,
    notion_synced           INTEGER NOT NULL DEFAULT 0, -- 0 = notion_pending
    run_id                  TEXT UNIQUE,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_url ON applications (job_url);
CREATE INDEX IF NOT EXISTS idx_applications_job ON applications (ats, job_id);
CREATE INDEX IF NOT EXISTS idx_applications_company_role
    ON applications (company COLLATE NOCASE, role_title COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS status_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications (id) ON DELETE CASCADE,
    status         TEXT NOT NULL,
    changed_at     TEXT NOT NULL,
    note           TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path) -> bool:
    """Create the schema if needed. Returns True when the database file was created."""
    created = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
    return created


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
