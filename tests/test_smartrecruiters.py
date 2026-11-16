"""SmartRecruiters, read only: the posting from its public API, for `helper resume` (2026-11-16)."""

from __future__ import annotations

from typing import Any

import pytest

from helper.adapters import adapter_for, generic, smartrecruiters
from helper.errors import HelperError, NeedsUser

LINK = "https://jobs.smartrecruiters.com/Mockco/744000000000001?utm_source=x"


class FakeResponse:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code, self._body = status, body

    def json(self) -> Any:
        return self._body


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response, self.asked = response, []

    def get(self, url: str, params: Any = None, timeout: int = 0) -> FakeResponse:
        self.asked.append(url)
        return self.response


def test_links_route_to_the_reader_for_resumes_only() -> None:
    assert smartrecruiters.parse_ref(LINK) == smartrecruiters.SmartRecruitersRef("Mockco", "744000000000001")
    assert adapter_for(LINK, purpose="resume") is smartrecruiters
    with pytest.raises(NeedsUser, match="helper doesn't fill SmartRecruiters"):
        adapter_for(LINK)


def test_the_posting_comes_from_the_api() -> None:
    body = {"name": "AI Intern", "company": {"name": "Mockco"}, "location": {"fullLocation": "Kirkland, Washington"},
            "applyUrl": "https://jobs.smartrecruiters.com/Mockco/744000000000001-ai-intern?oga=true",
            "jobAd": {"sections": {"jobDescription": {"text": "<p>" + "Build AI identity tools in Python. " * 15 + "</p>"},
                                   "qualifications": {"text": "<ul><li>Pursuing a Masters in CS</li></ul>"}}}}
    session = FakeSession(FakeResponse(200, body))
    job = smartrecruiters.fetch_job(LINK, session)
    assert (job.company, job.role_title, job.location, job.ats) == ("Mockco", "AI Intern", "Kirkland, Washington",
                                                                    "smartrecruiters")
    assert "- Pursuing a Masters in CS" in job.description_text
    assert session.asked == ["https://api.smartrecruiters.com/v1/companies/Mockco/postings/744000000000001"]
    with pytest.raises(HelperError, match="no open posting"):
        smartrecruiters.fetch_job(LINK, FakeSession(FakeResponse(404, {})))


def test_an_unsupported_browser_page_is_not_a_description() -> None:
    assert not generic.readable("Sorry, Internet Explorer 11 is no longer supported",
                                "Please use a modern browser to continue. " * 30)
