"""Month/year parsing and the resume date format (spec 6).

Months are abbreviated exactly as on the reference resume (Jan., Feb., ..., Sept., ...), ranges use
LaTeX `--`, and "Present" stays "Present".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

ABBREVIATIONS = {
    1: "Jan.", 2: "Feb.", 3: "Mar.", 4: "Apr.", 5: "May", 6: "Jun.",
    7: "Jul.", 8: "Aug.", 9: "Sept.", 10: "Oct.", 11: "Nov.", 12: "Dec.",
}
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_MONTH_YEAR_RE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$")
_RANGE_RE = re.compile(
    r"^(?P<start>[A-Za-z]{3,9}\.?\s+\d{4})\s*[-–]+\s*(?P<end>[A-Za-z]{3,9}\.?\s+\d{4}|Present)"
    r"\s*(?:\((?P<note>.*)\))?$",
    re.IGNORECASE,
)
_ONGOING_RE = re.compile(r"^Ongoing\b\s*(?:\((?P<note>.*)\))?$", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class MonthYear:
    year: int
    month: int

    def iso(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    def display(self) -> str:
        return f"{ABBREVIATIONS[self.month]} {self.year}"


def parse_month_year(text: str) -> Optional[MonthYear]:
    m = _MONTH_YEAR_RE.match(text.strip())
    if not m:
        return None
    month = _MONTHS.get(m.group(1).lower())
    return MonthYear(int(m.group(2)), month) if month else None


@dataclass(frozen=True)
class Timeline:
    start: Optional[MonthYear]
    end: Optional[MonthYear]
    end_word: Optional[str]  # "Present" or "Ongoing" when there is no end month
    note: Optional[str]  # the parenthetical after the range, verbatim

    def display(self) -> str:
        if self.start is None:
            return self.end_word or ""
        end = self.end.display() if self.end else (self.end_word or "")
        return f"{self.start.display()} -- {end}"

    def start_iso(self) -> Optional[str]:
        return self.start.iso() if self.start else None

    def end_iso(self) -> Optional[str]:
        if self.end:
            return self.end.iso()
        return self.end_word.lower() if self.end_word else None


def parse_timeline(text: str) -> Optional[Timeline]:
    """'December 2025 - January 2026', 'July 2026 - Present', 'Ongoing (...)', each with an optional (note)."""
    text = text.strip()
    m = _RANGE_RE.match(text)
    if m:
        start = parse_month_year(m.group("start"))
        end_raw = m.group("end")
        if start is None:
            return None
        if end_raw.lower() == "present":
            return Timeline(start, None, "Present", m.group("note"))
        end = parse_month_year(end_raw)
        return Timeline(start, end, None, m.group("note")) if end else None
    m = _ONGOING_RE.match(text)
    if m:
        return Timeline(None, None, "Ongoing", m.group("note"))
    return None


def display_month_year(text: str) -> str:
    """'December 2027' -> 'Dec. 2027' (used for grad_date_display)."""
    parsed = parse_month_year(text)
    if parsed is None:
        raise ValueError(f"not a month and year: {text!r}")
    return parsed.display()
