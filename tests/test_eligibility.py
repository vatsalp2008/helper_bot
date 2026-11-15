"""Eligibility (spec 8) and company caps (8.3)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import pytest
import yaml

from helper.eligibility import (
    CompanyApplication,
    Eligibility,
    check_eligibility,
    format_eligibility,
    load_applications,
    same_company,
    window_start,
)
from helper.models import Cap, CapsFile, Config, Job, Profile
from helper.tracker.db import connect, init_db, now_iso

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CFG = Config()
CAPS = CapsFile.model_validate(yaml.safe_load((REPO / "data" / "caps.yaml").read_text(encoding="utf-8")))
PROFILE = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
TODAY = date(2026, 11, 14)
SUMMER = "Software Engineer Intern (Summer 2027)"


def check(text: str, title: str = SUMMER, company: str = "Acme", location: Optional[str] = None,
          apps: Optional[list[CompanyApplication]] = None, profile: Profile = PROFILE, today: date = TODAY) -> Eligibility:
    job = Job(company=company, role_title=title, location=location, description_text=text, source="jd_file",
              fetched_at="2026-11-14")
    return check_eligibility(job, CFG, caps=CAPS, applications=apps or [], profile=profile, today=today)


def levels(e: Eligibility) -> dict[str, str]:
    return {f.id: f.level for f in e.flags}


# --------------------------------------------------------------------------- 1. work authorization


@pytest.mark.parametrize("text", [
    "F-1 and OPT candidates are not eligible for this position.",
    "We are unable to sponsor or take over sponsorship of an employment visa, including F-1 OPT.",
    "This role is not open to candidates on CPT/OPT.",
    "No CPT or OPT.",
])
def test_named_visa_exclusions_block(text: str) -> None:
    e = check(text)
    assert levels(e)["visa_excluded"] == "blocker"
    assert e.recommendation == "skip_and_ask" and e.blocked
    assert e.flags[0].evidence


def test_ambiguous_no_sponsorship_is_flagged_and_built() -> None:
    e = check("Applicants must be authorized to work in the U.S. without sponsorship now or in the future.")
    assert levels(e)["no_sponsorship"] == "warning"
    assert e.recommendation == "build"


def test_cpt_opt_welcome_and_unclear_mentions() -> None:
    welcome = check("International students on CPT/OPT are welcome to apply.")
    assert levels(welcome) == {"visa_welcome": "note", "grad_date": "note"}
    unclear = check("Please tell us whether you will need CPT or OPT paperwork.")
    assert levels(unclear)["cpt_opt_mentioned"] == "warning"


def test_export_control_is_flagged_and_built() -> None:
    e = check("This position involves technology subject to U.S. export control regulations (ITAR).")
    assert levels(e)["export_control"] == "warning"
    assert e.recommendation == "build"


def test_citizenship_and_clearance_follow_the_profile() -> None:
    assert levels(check("Must be a U.S. citizen."))["citizenship_required"] == "blocker"
    citizen = PROFILE.model_copy(deep=True)
    citizen.tier2.us_citizen_or_permanent_resident.value = "Yes"
    assert "citizenship_required" not in levels(check("Must be a U.S. citizen.", profile=citizen))
    assert levels(check("Candidates must be able to obtain a security clearance."))["clearance_required"] == "blocker"


# --------------------------------------------------------------------------- 2. degree level


def test_degree_level() -> None:
    assert levels(check("Currently pursuing a Bachelor's degree in Computer Science."))["undergrad_only"] == "warning"
    assert levels(check("Open to rising juniors or seniors."))["undergrad_only"] == "warning"
    both = check("Currently pursuing a BS or MS in Computer Science.")
    assert "undergrad_only" not in levels(both)
    pay = check("Hourly rate: Bachelor's students $40/hr, Master's students $45/hr, PhD students $50/hr.")
    assert levels(pay)["grad_students_considered"] == "note"


# --------------------------------------------------------------------------- 3. timing


def test_semester_roles_raise_the_cpt_hours_question() -> None:
    fall = check("This is a full-time, on-site internship in Seattle.", title="Software Engineer Intern (Fall 2026)")
    flag = next(f for f in fall.flags if f.id == "semester_timing")
    assert flag.level == "warning" and "full-time hours" in flag.message and "on site" in flag.message
    assert "semester_timing" in levels(check("Join our team.", title="Software Co-op, Spring 2027"))
    assert "semester_timing" not in levels(check("Join our team."))  # summer


def test_immediate_start_is_checked_against_the_target() -> None:
    title = "New Grad Software Engineer"
    text = "We need someone who can start immediately."
    assert levels(check(text, title=title))["immediate_start"] == "warning"
    assert "immediate_start" not in levels(check(text, title=title, today=date(2027, 1, 10)))


# --------------------------------------------------------------------------- 4. graduation date


@pytest.mark.parametrize(
    "text,title,expected",
    [
        ("Join our team.", SUMMER, "May 2027"),
        ("Candidates must be graduating between December 2027 and June 2028.", SUMMER, "December 2027"),
        ("Must have a graduation date between December 2026 and June 2027.", SUMMER, "May 2027"),
        ("You must be returning to school after the internship.", SUMMER, "December 2027"),
        ("You must be returning to school after the internship.", "Software Engineer Intern (Summer 2026)", "May 2027"),
        ("Open to the class of 2027.", SUMMER, "May 2027"),
        ("Expected graduation in Fall 2027.", SUMMER, "December 2027"),
    ],
)
def test_graduation_date(text: str, title: str, expected: str) -> None:
    e = check(text, title=title)
    assert e.grad_date == expected, e.grad_date_reason
    assert not e.blocked


def test_graduation_window_that_fits_neither_date_blocks() -> None:
    e = check("Expected graduation between December 2025 and May 2026.")
    assert levels(e)["grad_window"] == "blocker"


# --------------------------------------------------------------------------- 5. location


def test_far_office_during_the_semester_blocks() -> None:
    fall = "Software Engineer Intern (Fall 2026)"
    assert levels(check("Location: New York, NY (on-site)", title=fall))["far_office"] == "blocker"
    assert "far_office" not in levels(check("Location: Bellevue, WA", title=fall))
    assert "far_office" not in levels(check("Location: Remote (US)", title=fall))
    assert levels(check("Location: New York, NY"))["relocation"] == "note"  # summer


# --------------------------------------------------------------------------- 6. deadlines


def test_deadlines_and_urgency() -> None:
    assert levels(check("Apply by March 1, 2027."))["deadline"] == "note"
    assert levels(check("Application deadline: October 1, 2026"))["deadline_passed"] == "warning"
    assert levels(check("Applications are reviewed on a rolling basis."))["urgency"] == "note"


# --------------------------------------------------------------------------- 7. caps (8.3)


def app(n: int, company: str, status: str = "applied", when: str = "2026-10-01", pct: Optional[float] = None,
        role: str = "SWE Intern") -> CompanyApplication:
    return CompanyApplication(id=n, company=company, role_title=role, status=status, date_applied=when,
                              keyword_match_pct=pct)


def test_caps_flag_but_never_block() -> None:
    three = [app(i, "Salesforce") for i in range(3)]
    reached = check("x", company="Salesforce", apps=three)
    assert levels(reached)["cap_reached"] == "warning" and reached.recommendation == "build"
    assert "recommendation, so building and applying anyway" in next(f.message for f in reached.flags
                                                                    if f.id == "cap_reached")
    two = check("x", company="Salesforce", apps=three[:2])
    near = next(f for f in two.flags if f.id == "cap_near")
    assert near.level == "warning" and "application 3 of the 3 Salesforce recommends per 12 months" in near.message
    google = check("x", company="Google", apps=[app(i, "Google", when="2026-11-01") for i in range(3)])
    assert google.recommendation == "build"
    assert "Their site may refuse this one" in next(f.message for f in google.flags if f.id == "cap_reached")
    assert levels(check("x", company="ByteDance"))["cap_note"] == "note"  # TikTok's limit covers affiliates
    assert "cap_near" not in levels(check("x", company="Salesforce", apps=three[:1]))
    old = [app(1, "Salesforce", when="2025-06-01")] * 3  # outside the 12-month window
    assert "cap_reached" not in levels(check("x", company="Salesforce", apps=old))


def test_caps_by_days_aliases_drafts_and_notes() -> None:
    assert levels(check("x", company="Databricks"))["cap_near"] == "warning"  # this would use the only slot
    recent = [app(1, "Databricks", when="2026-11-01")]
    assert levels(check("x", company="Databricks", apps=recent))["cap_reached"] == "warning"
    stale = [app(1, "Databricks", when="2026-09-01")]
    assert "cap_reached" not in levels(check("x", company="Databricks", apps=stale))
    drafts = [app(i, "Samsung Electronics", status="drafted") for i in range(12)]
    samsung = levels(check("x", company="Samsung Electronics America", apps=drafts))
    assert samsung.get("cap_pending") == "note" and "cap_reached" not in samsung
    assert levels(check("x", company="Google"))["cap_note"] == "note"


def test_several_applications_at_one_company_are_ranked() -> None:
    apps = [app(1, "Salesforce, Inc.", pct=60, role="Data Intern"), app(2, "Salesforce", pct=85, role="Backend Intern")]
    e = check("x", company="Salesforce", apps=apps)
    note = next(f for f in e.flags if f.id == "company_applications")
    assert note.message.index("Backend Intern") < note.message.index("Data Intern")


def test_company_matching_and_windows() -> None:
    assert same_company("Salesforce", "Salesforce, Inc.")
    assert not same_company("Meta", "Metabase")
    assert window_start(TODAY, Cap(company="X", max_applications=1, window_months=12)) == date(2025, 11, 14)
    assert window_start(date(2026, 3, 31), Cap(company="X", max_applications=1, window_months=1)) == date(2026, 2, 28)
    assert window_start(TODAY, Cap(company="X", max_applications=1, window_days=30)) == date(2026, 10, 15)


def test_applications_are_read_from_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "helper.db"
    assert load_applications(db) == []
    init_db(db)
    conn = connect(db)
    try:
        conn.execute("INSERT INTO applications (company, role_title, status, date_applied, keyword_match_pct, "
                     "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("Salesforce", "Backend Intern", "applied", "2026-10-01", 82.5, now_iso(), now_iso()))
        conn.commit()
    finally:
        conn.close()
    (row,) = load_applications(db)
    assert (row.company, row.status, row.keyword_match_pct) == ("Salesforce", "applied", 82.5)


def test_report_lists_blockers_first() -> None:
    text = format_eligibility(check("Applications are reviewed on a rolling basis. F-1 candidates are not eligible."))
    lines = text.splitlines()
    assert lines[0] == "Eligibility (spec 8): recommend skipping; ask Vatsal"
    assert lines[1].startswith("  [blocker]")
