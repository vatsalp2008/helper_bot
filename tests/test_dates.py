from __future__ import annotations

from typing import Optional

import pytest

from helper.dates import MonthYear, display_month_year, parse_month_year, parse_timeline


@pytest.mark.parametrize(
    "text,display,start,end,note",
    [
        ("December 2025 - January 2026", "Dec. 2025 -- Jan. 2026", "2025-12", "2026-01", None),
        ("July 2026 - Present", "Jul. 2026 -- Present", "2026-07", "present", None),
        ("September 2024 - December 2024", "Sept. 2024 -- Dec. 2024", "2024-09", "2024-12", None),
        ("June 2026 - July 2026 (hackathon, 6 days; eval harness added August 2026)",
         "Jun. 2026 -- Jul. 2026", "2026-06", "2026-07", "hackathon, 6 days; eval harness added August 2026"),
        ("Ongoing (long-term, multi-year)", "Ongoing", None, "ongoing", "long-term, multi-year"),
        ("Dec 2025 - Jan 2026", "Dec. 2025 -- Jan. 2026", "2025-12", "2026-01", None),
    ],
)
def test_parse_timeline(text: str, display: str, start: Optional[str], end: str, note: Optional[str]) -> None:
    timeline = parse_timeline(text)
    assert timeline is not None
    assert timeline.display() == display
    assert (timeline.start_iso(), timeline.end_iso(), timeline.note) == (start, end, note)


def test_month_abbreviations_match_the_reference_resume() -> None:
    labels = [MonthYear(2026, m).display().split()[0] for m in range(1, 13)]
    assert labels == ["Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.", "Jul.", "Aug.", "Sept.", "Oct.", "Nov.", "Dec."]


def test_grad_date_display() -> None:
    assert display_month_year("December 2027") == "Dec. 2027"
    assert display_month_year("May 2027") == "May 2027"
    with pytest.raises(ValueError):
        display_month_year("Fall 2027")


def test_unparseable_text_returns_none() -> None:
    assert parse_timeline("sometime next year") is None
    assert parse_month_year("Smarch 2026") is None
