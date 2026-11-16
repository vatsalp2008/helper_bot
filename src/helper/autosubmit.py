"""Auto-submit (spec 10.5, milestone 9). Off by default (`submit_mode: review`).

In `auto` mode helper clicks Submit itself, exactly once (rule 10), and only when every condition holds:
- this ATS has at least `auto_submit_min_clean_runs` clean review-mode runs (helper filled every field, nothing needed
  fixing by hand, and the submit was confirmed);
- every required field was answered from the profile, an enabled Tier 2 item, a fixed rule, or an approved answer;
- no answer still needs Vatsal's review;
- no eligibility warning or blocker, and no possible duplicate;
- no CAPTCHA or verification step appeared;
- the form shows no errors before Submit.
Otherwise the run falls back to review mode, and helper says why. Workday stays review-only: Vatsal checks its Review
page himself (2026-11-14).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from .tracker.db import now_iso

REVIEW_ONLY = {"icims": "iCIMS runs always stop before Submit for you",
               "generic": "company career sites and other unfamiliar forms always stop before Submit for you"}
AUTO_CAPABLE = ("greenhouse", "lever", "ashby", "workday")  # Workday from 2026-11-16 (Vatsal)


@dataclass
class Gate:
    ats: str
    clean_runs: int
    min_clean_runs: int
    answers: list[Any]  # PlannedAnswer
    results: list[Any]  # FieldResult
    eligibility: dict[str, Any] = field(default_factory=dict)
    captcha_seen: bool = False
    page_errors: list[str] = field(default_factory=list)
    has_submit_button: bool = True
    best_guesses_ok: bool = False  # Workday (Vatsal, 2026-11-16): filled best guesses don't stop it


def blockers(g: Gate) -> list[str]:
    """Why helper must not submit this run by itself. Empty when every spec 10.5 condition holds."""
    reasons: list[str] = []
    if g.ats in REVIEW_ONLY:
        return [REVIEW_ONLY[g.ats]]
    if g.ats not in AUTO_CAPABLE:
        return [REVIEW_ONLY["generic"]]
    if g.clean_runs < g.min_clean_runs:
        reasons.append(f"{g.ats} has {g.clean_runs} clean review-mode run(s) so far; auto-submit needs "
                       f"{g.min_clean_runs}")
    filled = {r.field: r for r in g.results}
    for a in g.answers:
        label = a.label if len(a.label) <= 60 else a.label[:57] + "..."
        if a.status == "needs_review":
            reasons.append(f"'{label}' still needs your review")
        if not a.required or a.kind == "skip":
            continue
        if a.kind == "confirm" and g.best_guesses_ok and a.value is not None:
            pass
        elif a.kind not in ("auto", "file") or (a.kind == "auto" and a.value is None):
            reasons.append(f"'{label}' wasn't answered from your profile or an approved answer")
        result = filled.get(a.field)
        if result is None or not result.ok:
            reasons.append(f"'{label}' wasn't filled")
    flags = [f.get("message", "") for f in g.eligibility.get("flags", []) if f.get("level") in ("warning", "blocker")]
    if flags:
        reasons.append("eligibility flags: " + "; ".join(m[:90] for m in flags))
    if g.eligibility.get("duplicates"):
        reasons.append("the duplicate check found a matching application")
    if g.captcha_seen:
        reasons.append("a CAPTCHA or verification step appeared")
    if g.page_errors:
        reasons.append("the form shows errors: " + "; ".join(e[:80] for e in g.page_errors[:3]))
    if not g.has_submit_button:
        reasons.append("helper couldn't find exactly one Submit button")
    return list(dict.fromkeys(reasons))


def run_problems(results: list[Any], captcha_seen: bool, notes: Optional[list[str]] = None) -> list[str]:
    """What kept a run from being clean: fields helper couldn't fill, a CAPTCHA, or pauses for Vatsal."""
    problems = [f"not filled: {r.label}" for r in results if not r.ok]
    if captcha_seen:
        problems.append("a CAPTCHA or verification step appeared")
    return problems + list(notes or [])


def record_fill_run(conn: sqlite3.Connection, run_id: str, ats: Optional[str], mode: str, outcome: str,
                    problems: list[str]) -> bool:
    """Log a finished fill. A run is clean when the submit was confirmed and nothing needed fixing by hand."""
    clean = outcome == "applied" and not problems
    conn.execute("INSERT INTO fill_runs (run_id, ats, mode, outcome, clean, problems, finished_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (run_id, ats or "unknown", mode, outcome, int(clean), json.dumps(problems), now_iso()))
    return clean


def clean_review_runs(conn: sqlite3.Connection, ats: Optional[str]) -> int:
    row = conn.execute("SELECT COUNT(*) FROM fill_runs WHERE ats = ? AND mode = 'review' AND clean = 1",
                       (ats or "unknown",)).fetchone()
    return int(row[0])


def readiness(conn: sqlite3.Connection, min_clean_runs: int, by_site: Optional[dict[str, int]] = None) -> str:
    """One line for `helper stats`: how close each site is to auto-submit."""
    parts = []
    for ats in AUTO_CAPABLE:
        need = (by_site or {}).get(ats, min_clean_runs)
        parts.append(f"{ats} ready (no count needed)" if need == 0 else
                     f"{ats} {min(clean_review_runs(conn, ats), need)}/{need}")
    return ("Auto-submit readiness (clean review-mode runs): " + ", ".join(parts)
            + "; iCIMS and unfamiliar sites: review only")
