"""SQLite schema (spec section 11). SQLite is the system of record."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 5

# Statuses a run sets (spec 11).
PIPELINE_STATUSES = ("drafted", "filled", "applied", "submitted_unverified", "skipped", "failed")
# Statuses you set later with `helper mark <id> <status>`.
OUTCOME_STATUSES = (
    "oa", "phone_screen", "interview", "offer", "accepted", "declined", "rejected", "withdrawn", "ghosted",
)
ALL_STATUSES = PIPELINE_STATUSES + OUTCOME_STATUSES

# Statuses that mean the application was sent; they count toward caps and appear in Notion.
SUBMITTED_STATUSES = ("applied", "submitted_unverified") + OUTCOME_STATUSES
# Never written to Notion: nothing was sent yet (or ever).
LOCAL_ONLY_STATUSES = ("drafted", "filled", "skipped", "failed")

# Spec 11 columns in order, then build additions (run_id, timestamps; v2: source and Notion sync details).
APPLICATION_COLUMNS = (
    "id", "company", "role_title", "location", "job_url", "job_id", "ats", "date_applied", "status",
    "resume_path", "resume_hash", "projects_used", "keyword_match_pct", "confirmation_screenshot",
    "notes", "flags", "notion_page_id", "notion_synced", "run_id", "created_at", "updated_at",
    "source", "notion_last_edited", "notion_error", "notion_resume_hash",
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
    updated_at              TEXT NOT NULL,
    source                  TEXT NOT NULL DEFAULT 'helper', -- helper | notion (imported from the tracker)
    notion_last_edited      TEXT,                       -- the Notion page's last_edited_time at the last sync
    notion_error            TEXT,                       -- why the last Notion sync of this row failed
    notion_resume_hash      TEXT                        -- sha256 of the PDF attached in Notion
);
CREATE INDEX IF NOT EXISTS idx_applications_url ON applications (job_url);
CREATE INDEX IF NOT EXISTS idx_applications_job ON applications (ats, job_id);
CREATE INDEX IF NOT EXISTS idx_applications_company_role
    ON applications (company COLLATE NOCASE, role_title COLLATE NOCASE);

CREATE INDEX IF NOT EXISTS idx_applications_notion ON applications (notion_page_id);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fill_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    ats         TEXT NOT NULL,
    mode        TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    clean       INTEGER NOT NULL,
    problems    TEXT,
    finished_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS postings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url            TEXT NOT NULL UNIQUE,
    job_id         TEXT,
    company        TEXT NOT NULL,
    title          TEXT NOT NULL,
    location       TEXT,
    ats            TEXT,
    source         TEXT,
    posted_at      TEXT,
    found_at       TEXT NOT NULL,
    status         TEXT NOT NULL,
    reason         TEXT,
    run_id         TEXT,
    application_id INTEGER,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_postings_run ON postings (run_id);

CREATE TABLE IF NOT EXISTS inbox_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id     TEXT NOT NULL UNIQUE,
    received_at    TEXT,
    sender         TEXT,
    subject        TEXT,
    category       TEXT NOT NULL,
    application_id INTEGER,
    summary        TEXT,
    action         TEXT,
    deadline       TEXT,
    run_id         TEXT NOT NULL,
    processed_at   TEXT NOT NULL,
    done           INTEGER NOT NULL DEFAULT 1
);

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


# Statements that bring a database from version N-1 to N. A new database gets _SCHEMA directly.
_MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE applications ADD COLUMN source TEXT NOT NULL DEFAULT 'helper'",
        "ALTER TABLE applications ADD COLUMN notion_last_edited TEXT",
        "ALTER TABLE applications ADD COLUMN notion_error TEXT",
        "ALTER TABLE applications ADD COLUMN notion_resume_hash TEXT",
        "CREATE INDEX IF NOT EXISTS idx_applications_notion ON applications (notion_page_id)",
        "CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ],
    3: [
        "CREATE TABLE IF NOT EXISTS fill_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
        "ats TEXT NOT NULL, mode TEXT NOT NULL, outcome TEXT NOT NULL, clean INTEGER NOT NULL, problems TEXT, "
        "finished_at TEXT NOT NULL)",
    ],
    4: [  # the job hunt's list of postings (Vatsal, 2026-11-16)
        "CREATE TABLE IF NOT EXISTS postings (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, job_id TEXT, "
        "company TEXT NOT NULL, title TEXT NOT NULL, location TEXT, ats TEXT, source TEXT, posted_at TEXT, "
        "found_at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, run_id TEXT, application_id INTEGER, "
        "updated_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_postings_run ON postings (run_id)",
    ],
    5: [  # application emails `helper inbox` checked (Vatsal, 2026-11-27); done = 0 marks a step still to take
        "CREATE TABLE IF NOT EXISTS inbox_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE, "
        "received_at TEXT, sender TEXT, subject TEXT, category TEXT NOT NULL, application_id INTEGER, summary TEXT, "
        "action TEXT, deadline TEXT, run_id TEXT NOT NULL, processed_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 1)",
    ],
}


def init_db(db_path: Path) -> bool:
    """Create the schema, or migrate an older database to SCHEMA_VERSION. True when the file was created."""
    created = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'applications'").fetchone() is not None
        if not has_table:
            conn.executescript(_SCHEMA)
        else:
            for target in range(max(version, 1) + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[target]:
                    conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
    return created


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
