"""Notion sync against a fake API: what is written, when, and what comes back."""

from __future__ import annotations

import copy
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest
import requests
import yaml

from helper.errors import HelperError
from helper.tracker import repo
from helper.tracker.db import connect, init_db
from helper.tracker.notion import NotionClient, NotionError, NotionMapping, check_mapping, row_from_page, sync

REPO = Path(__file__).resolve().parents[1]
MAPPING = NotionMapping.model_validate(yaml.safe_load((REPO / "data" / "notion_mapping.yaml").read_text(encoding="utf-8")))
STATUSES = ["To apply", "Applied", "Online assessment", "Interviewing", "Offer", "Rejected"]
SCHEMA: dict[str, Any] = {
    "title": [{"plain_text": "Internship Applications Tracker"}],
    "properties": {
        "Title": {"type": "title"}, "Company": {"type": "rich_text"}, "Application link": {"type": "url"},
        "Location": {"type": "rich_text"}, "Resume version": {"type": "files"}, "Applied on": {"type": "date"},
        "Status": {"type": "status", "status": {"options": [{"name": n} for n in STATUSES]}},
        "Deadline": {"type": "date"}, "Referral": {"type": "checkbox"}, "Referral name": {"type": "rich_text"},
    },
}


def page(pid: str, title: str, company: str, status: Optional[str], applied: Optional[str] = None,
         url: Optional[str] = None, edited: str = "2026-11-01T00:00:00.000Z") -> dict[str, Any]:
    return {"id": pid, "created_time": "2026-10-01T00:00:00.000Z", "last_edited_time": edited, "properties": {
        "Title": {"type": "title", "title": [{"plain_text": title}]},
        "Company": {"type": "rich_text", "rich_text": [{"plain_text": company}]},
        "Application link": {"type": "url", "url": url},
        "Location": {"type": "rich_text", "rich_text": [{"plain_text": "Remote"}]},
        "Status": {"type": "status", "status": {"name": status} if status else None},
        "Applied on": {"type": "date", "date": {"start": applied} if applied else None},
        "Resume version": {"type": "files", "files": [{"name": "VatsalPatel_Resume.pdf"}]},
    }}


class FakeAPI:
    def __init__(self, pages: Optional[list[dict[str, Any]]] = None, fail: bool = False) -> None:
        self.pages = {p["id"]: p for p in pages or []}
        self.fail = fail
        self.calls: list[tuple[Any, ...]] = []
        self.uploads: list[str] = []

    def data_source(self, data_source_id: str) -> dict[str, Any]:
        return SCHEMA

    def query(self, data_source_id: str, filter: Optional[dict[str, Any]] = None) -> Iterator[dict[str, Any]]:
        self.calls.append(("query", filter))
        return iter([copy.deepcopy(p) for p in self.pages.values()])

    def create_page(self, data_source_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise NotionError("Notion said 403 restricted_resource: Insufficient permissions")
        self.calls.append(("create", properties))
        new = {"id": f"new-{len(self.pages) + 1}", "created_time": "2026-11-14T10:00:00.000Z",
               "last_edited_time": "2026-11-14T10:00:00.000Z", "properties": {}}
        self.pages[new["id"]] = new
        return new

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("update", page_id, properties))
        self.pages[page_id]["last_edited_time"] = "2026-11-14T11:00:00.000Z"
        return self.pages[page_id]

    def upload_pdf(self, path: Path) -> str:
        self.uploads.append(path.name)
        return "upload-1"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    init_db(tmp_path / "helper.db")
    c = connect(tmp_path / "helper.db")
    yield c
    c.close()


def writes(api: FakeAPI) -> list[tuple[Any, ...]]:
    return [c for c in api.calls if c[0] != "query"]


def test_the_mapping_matches_the_tracker_and_problems_are_named() -> None:
    assert check_mapping(SCHEMA, MAPPING) == []
    broken = copy.deepcopy(SCHEMA)
    del broken["properties"]["Applied on"]
    broken["properties"]["Company"] = {"type": "number"}
    broken["properties"]["Status"]["status"]["options"] = [{"name": "Applied"}]
    problems = check_mapping(broken, MAPPING)
    assert any("'Applied on'" in p and "never creates columns" in p for p in problems)
    assert any("'Company' is number" in p for p in problems)
    assert any("'Interviewing' isn't in your tracker" in p for p in problems)


def test_the_first_sync_imports_the_tracker_and_writes_nothing(conn: sqlite3.Connection, tmp_path: Path) -> None:
    api = FakeAPI([page("p1", "SWE Intern", "Acme", "Applied", "2026-10-01", "https://boards.greenhouse.io/acme/jobs/1"),
                   page("p2", "Data Intern", "Beta", "Rejected", "2026-09-01"),
                   page("p3", "ML Intern", "Gamma", "To apply")])
    report = sync(conn, api, MAPPING, tmp_path)
    assert report.imported == 3 and writes(api) == []
    rows = {r["notion_page_id"]: r for r in conn.execute("SELECT * FROM applications")}
    assert (rows["p1"]["status"], rows["p1"]["source"], rows["p1"]["job_id"], rows["p1"]["notion_synced"]) == (
        "applied", "notion", "1", 1)
    assert (rows["p2"]["status"], rows["p3"]["status"]) == ("rejected", "drafted")
    again = sync(conn, api, MAPPING, tmp_path)
    assert again.imported == 0 and again.status_changes == []
    assert api.calls[-1][1]["timestamp"] == "last_edited_time"  # later pulls ask only for edited pages


def test_hand_edits_in_notion_come_back(conn: sqlite3.Connection, tmp_path: Path) -> None:
    api = FakeAPI([page("p1", "SWE Intern", "Acme", "Applied", "2026-10-01")])
    sync(conn, api, MAPPING, tmp_path)
    api.pages["p1"] = page("p1", "SWE Intern", "Acme", "Interviewing", "2026-10-01", edited="2026-11-10T00:00:00.000Z")
    report = sync(conn, api, MAPPING, tmp_path)
    (row,) = conn.execute("SELECT * FROM applications").fetchall()
    assert row["status"] == "interview" and report.status_changes == [(row["id"], "applied", "interview")]
    assert repo.history(conn, row["id"])[-1]["note"] == "from Notion"


def test_a_sent_application_becomes_a_page_with_its_resume(conn: sqlite3.Connection, tmp_path: Path) -> None:
    pdf = tmp_path / "resumes" / "run-1" / "VatsalPatel_Resume.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.5 test")
    drafted = repo.insert(conn, company="Beta", role_title="Draft only", status="drafted")
    app_id = repo.insert(conn, company="Acme", role_title="SWE Intern", status="applied", date_applied="2026-11-14",
                         job_url="https://boards.greenhouse.io/acme/jobs/9", location="Seattle, WA",
                         resume_path="resumes/run-1/VatsalPatel_Resume.pdf", resume_hash="abc")
    api = FakeAPI()
    report = sync(conn, api, MAPPING, tmp_path, pull=False)
    assert report.created == [app_id] and api.uploads == [pdf.name]
    (_, props), = writes(api)
    assert props["Title"]["title"][0]["text"]["content"] == "SWE Intern"
    assert props["Company"]["rich_text"][0]["text"]["content"] == "Acme"
    assert props["Application link"] == {"url": "https://boards.greenhouse.io/acme/jobs/9"}
    assert props["Status"] == {"status": {"name": "Applied"}}
    assert props["Applied on"] == {"date": {"start": "2026-11-14"}}
    assert props["Resume version"]["files"][0] == {"type": "file_upload", "file_upload": {"id": "upload-1"},
                                                  "name": pdf.name}
    assert not {"Deadline", "Referral", "Referral name"} & set(props)
    row = repo.get(conn, app_id)
    assert (row["notion_synced"], row["notion_page_id"], row["notion_resume_hash"]) == (1, "new-1", "abc")
    assert repo.get(conn, drafted)["notion_page_id"] is None  # drafts never reach Notion


def test_an_imported_to_apply_row_is_updated_not_duplicated(conn: sqlite3.Connection, tmp_path: Path) -> None:
    api = FakeAPI([page("p3", "ML Intern", "Gamma", "To apply")])
    sync(conn, api, MAPPING, tmp_path)
    (row,) = conn.execute("SELECT * FROM applications").fetchall()
    repo.set_status(conn, row["id"], "applied", applied_on="2026-11-14")
    sync(conn, api, MAPPING, tmp_path)
    (call,) = writes(api)
    assert call[0] == "update" and call[1] == "p3"
    assert call[2] == {"Status": {"status": {"name": "Applied"}}, "Applied on": {"date": {"start": "2026-11-14"}}}
    assert repo.get(conn, row["id"])["status"] == "applied"  # the pull after the push doesn't undo it


def test_failures_keep_the_row_pending(conn: sqlite3.Connection, tmp_path: Path) -> None:
    app_id = repo.insert(conn, company="Acme", role_title="SWE Intern", status="applied", date_applied="2026-11-14")
    report = sync(conn, FakeAPI(fail=True), MAPPING, tmp_path, pull=False)
    row = repo.get(conn, app_id)
    assert report.failed and row["notion_synced"] == 0 and "Insufficient permissions" in row["notion_error"]
    assert sync(conn, FakeAPI(), MAPPING, tmp_path, pull=False).created == [app_id]  # retried later
    assert repo.get(conn, app_id)["notion_error"] is None


def test_dry_run_writes_nothing(conn: sqlite3.Connection, tmp_path: Path) -> None:
    repo.insert(conn, company="Acme", role_title="SWE Intern", status="applied", date_applied="2026-11-14")
    api = FakeAPI([page("p1", "Data Intern", "Beta", "Applied", "2026-10-01")])
    report = sync(conn, api, MAPPING, tmp_path, dry_run=True)
    assert len(report.created) == 1 and report.imported == 1 and writes(api) == []
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1
    assert repo.get_state(conn, "notion_last_pull") is None


def test_statuses_notion_has_no_option_for_stay_local(conn: sqlite3.Connection, tmp_path: Path) -> None:
    api = FakeAPI([page("p1", "SWE Intern", "Acme", "Offer", "2026-10-01")])
    sync(conn, api, MAPPING, tmp_path)
    (row,) = conn.execute("SELECT * FROM applications").fetchall()
    repo.set_status(conn, row["id"], "declined")
    sync(conn, api, MAPPING, tmp_path)
    assert writes(api)[0][2] == {"Applied on": {"date": {"start": "2026-10-01"}}}  # Status untouched
    api.pages["p1"]["last_edited_time"] = "2026-11-20T00:00:00.000Z"
    sync(conn, api, MAPPING, tmp_path)
    assert repo.get(conn, row["id"])["status"] == "declined"


def test_row_from_page_handles_empty_cells() -> None:
    data = row_from_page({"id": "p", "properties": {}}, MAPPING)
    assert (data["company"], data["role_title"], data["status"]) == ("(no company)", "(untitled)", "drafted")


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any], headers: Optional[dict[str, str]] = None) -> None:
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, ""

    def json(self) -> dict[str, Any]:
        return self._body


class FakeSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.seen: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
        self.seen.append({"method": method, "url": url, **kw})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt  # type: ignore[no-any-return]


def test_client_retries_rate_limits_and_hides_the_token() -> None:
    sleeps: list[float] = []
    session = FakeSession([FakeResponse(429, {}, {"Retry-After": "1"}), FakeResponse(200, {"ok": True})])
    client = NotionClient("ntn_secret_value", "2025-09-03", session=session, sleep=sleeps.append)
    assert client.request("GET", "users/me") == {"ok": True}
    assert sleeps == [1.0] and session.seen[0]["headers"]["Notion-Version"] == "2025-09-03"
    bad = NotionClient("ntn_secret_value", "2025-09-03", sleep=sleeps.append, session=FakeSession(
        [FakeResponse(400, {"code": "validation_error", "message": "Title is not a property"})]))
    with pytest.raises(NotionError) as err:
        bad.request("POST", "pages", json_body={})
    assert "validation_error: Title is not a property" in str(err.value) and "ntn_secret" not in str(err.value)
    offline = NotionClient("t", "v", sleep=sleeps.append, max_retries=1,
                           session=FakeSession([requests.ConnectionError(), requests.ConnectionError()]))
    with pytest.raises(NotionError, match="can't reach Notion"):
        offline.request("GET", "users/me")


def test_a_mapping_problem_stops_the_sync(conn: sqlite3.Connection, tmp_path: Path) -> None:
    api = FakeAPI()
    api.data_source = lambda _id: {"properties": {}}  # type: ignore[method-assign]
    with pytest.raises(HelperError, match="doesn't match data/notion_mapping.yaml"):
        sync(conn, api, MAPPING, tmp_path)
