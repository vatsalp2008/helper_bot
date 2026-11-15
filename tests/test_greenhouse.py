"""Greenhouse intake (spec 9 step 2) and which sites helper takes on (spec 10.2, rules 7 and 8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from helper.adapters import adapter_for
from helper.adapters import greenhouse as gh
from helper.errors import HelperError, NeedsUser, NotBuiltYet

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIGMA = json.loads((FIXTURES / "greenhouse_figma_job.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "url,board,job_id",
    [
        ("https://job-boards.greenhouse.io/figma/jobs/6178851004", "figma", "6178851004"),
        ("https://boards.greenhouse.io/figma/jobs/6178851004?gh_jid=6178851004", "figma", "6178851004"),
        ("https://boards.greenhouse.io/embed/job_app?for=abacus&token=7654321", "abacus", "7654321"),
        ("https://careers.airbnb.com/positions/8380007?gh_jid=8380007", None, "8380007"),
    ],
)
def test_links_are_parsed(url: str, board: Optional[str], job_id: str) -> None:
    ref = gh.parse_ref(url)
    assert ref is not None and (ref.board, ref.job_id) == (board, job_id)


def test_non_greenhouse_links() -> None:
    assert gh.parse_ref("https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b") is None


def test_job_from_the_api() -> None:
    job = gh.job_from_api(FIGMA, "https://boards.greenhouse.io/figma/jobs/6178851004", "figma")
    assert (job.company, job.role_title, job.ats, job.job_id) == ("Figma", "Data Engineer Intern (2027)", "greenhouse",
                                                                  "6178851004")
    assert job.apply_url == "https://job-boards.greenhouse.io/figma/jobs/6178851004"
    assert "What you'll do\n\n- Build data pipelines with Python and SQL\n- Work with Spark and Airflow" in job.description_text
    by_field = {q.field_name: q for q in job.questions}
    assert by_field["first_name"].required and by_field["first_name"].type == "text"
    assert by_field["resume"].type == "file"
    assert by_field["question_19835368004"].type == "textarea"
    assert by_field["question_19835369004"].options == ["Yes", "No"]
    assert by_field["location"].section == "location"
    assert {"latitude", "longitude"}.isdisjoint(by_field)  # hidden fields are not questions
    eeoc = [q.field_name for q in job.questions if q.section == "eeoc"]
    assert eeoc.index("hispanic_ethnicity") == eeoc.index("race") - 1  # asked first on the form


class FakeResponse:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code, self._body = status, body

    def json(self) -> Any:
        return self._body


class FakeSession:
    def __init__(self, boards: dict[str, Any]) -> None:
        self.boards, self.asked = boards, []

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
        self.asked.append(url)
        board = url.split("/boards/")[1].split("/")[0]
        return FakeResponse(200, self.boards[board]) if board in self.boards else FakeResponse(404, {})


def test_company_career_links_find_their_board() -> None:
    session = FakeSession({"airbnb": {**FIGMA, "company_name": "Airbnb"}})
    job = gh.fetch_job("https://careers.airbnb.com/positions/6178851004?gh_jid=6178851004", session)
    assert job.company == "Airbnb" and job.apply_url.startswith("https://job-boards.greenhouse.io/airbnb/jobs/")


def test_a_closed_posting_says_so() -> None:
    with pytest.raises(HelperError, match="no open job 1"):
        gh.fetch_job("https://job-boards.greenhouse.io/acme/jobs/1", FakeSession({}))


def test_which_sites_helper_takes_on() -> None:
    assert adapter_for("https://job-boards.greenhouse.io/figma/jobs/1") is gh
    with pytest.raises(NeedsUser, match="never automates LinkedIn Easy Apply"):
        adapter_for("https://www.linkedin.com/jobs/view/123")
    with pytest.raises(NeedsUser, match="Handshake"):
        adapter_for("https://app.joinhandshake.com/jobs/123")
    with pytest.raises(NeedsUser, match="never creates"):
        adapter_for("https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/X_R1")
    with pytest.raises(NotBuiltYet, match="milestone 8"):
        adapter_for("https://jobs.lever.co/acme/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b")
