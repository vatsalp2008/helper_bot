from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterator

import pytest

from helper.errors import HelperError
from helper.tracker import repo
from helper.tracker.db import connect, init_db


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "helper.db"
    init_db(db)
    c = connect(db)
    yield c
    c.close()


def test_insert_reads_the_link_and_records_history(conn: sqlite3.Connection) -> None:
    app_id = repo.insert(conn, company="Acme", role_title="SWE Intern", job_url="https://boards.greenhouse.io/acme/jobs/123",
                         projects_used=["pulsetrader", "chef"])
    row = repo.get(conn, app_id)
    assert (row["ats"], row["job_id"], row["status"], row["source"]) == ("greenhouse", "123", "drafted", "helper")
    assert json.loads(row["projects_used"]) == ["pulsetrader", "chef"]
    assert [h["status"] for h in repo.history(conn, app_id)] == ["drafted"]
    with pytest.raises(HelperError, match="unknown application fields"):
        repo.insert(conn, company="Acme", role_title="X", salary=1)


def test_status_changes_date_the_application_and_queue_it_for_notion(conn: sqlite3.Connection) -> None:
    app_id = repo.insert(conn, company="Acme", role_title="SWE Intern", notion_synced=1)
    applied = repo.set_status(conn, app_id, "applied")
    assert applied["date_applied"] == date.today().isoformat() and applied["notion_synced"] == 0
    later = repo.set_status(conn, app_id, "interview", note="recruiter call")
    assert later["date_applied"] == date.today().isoformat()
    assert [(h["status"], h["note"]) for h in repo.history(conn, app_id)] == [
        ("drafted", "created"), ("applied", None), ("interview", "recruiter call")]

    other = repo.insert(conn, company="Beta", role_title="Data Intern", notion_synced=1)
    assert repo.set_status(conn, other, "skipped")["notion_synced"] == 1  # nothing was sent, nothing to send
    assert repo.set_status(conn, other, "applied", applied_on="2026-11-01")["date_applied"] == "2026-11-01"
    from_notion = repo.insert(conn, company="Gamma", role_title="ML Intern", status="applied", notion_synced=1)
    assert repo.set_status(conn, from_notion, "rejected", from_notion=True)["notion_synced"] == 1
    with pytest.raises(HelperError, match="not a status"):
        repo.set_status(conn, app_id, "hired")
    with pytest.raises(HelperError, match="no application #999"):
        repo.get(conn, 999)


def test_listing_and_stats(conn: sqlite3.Connection) -> None:
    repo.insert(conn, company="Acme", role_title="A", status="applied", date_applied="2026-11-10", ats="greenhouse")
    repo.insert(conn, company="Acme", role_title="B", status="rejected", date_applied="2026-11-03", ats="greenhouse")
    repo.insert(conn, company="Beta", role_title="C", status="applied", date_applied="2026-11-12", ats="lever")
    repo.insert(conn, company="Beta", role_title="D", status="drafted")
    assert [r["role_title"] for r in repo.list_applications(conn, company="acme")] == ["A", "B"]
    assert [r["role_title"] for r in repo.list_applications(conn, status="applied")] == ["C", "A"]
    s = repo.stats(conn, weeks=2, today=date(2026, 11, 14))
    assert s["per_week"] == {"2026-11-02": 1, "2026-11-09": 2}
    assert (s["total"], s["sent"]) == (4, 3)
    assert s["by_ats"] == {"greenhouse": 2, "lever": 1}
    assert s["by_status"]["applied"] == 2


def test_sync_state(conn: sqlite3.Connection) -> None:
    assert repo.get_state(conn, "notion_last_pull") is None
    repo.set_state(conn, "notion_last_pull", "2026-11-14T10:00:00+00:00")
    repo.set_state(conn, "notion_last_pull", "2026-11-14T11:00:00+00:00")
    assert repo.get_state(conn, "notion_last_pull") == "2026-11-14T11:00:00+00:00"
