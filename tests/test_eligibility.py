"""Eligibility (spec 8) and company caps (8.3)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import pytest
import yaml

from helper.dates import parse_month_year
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
    # HP, 2026-11-18
    "HP, Inc. will not provide any assistance or sign documentation in support of immigration sponsorship including "
    "Curricular Practical Training (CPT) or Optional Practical Training (OPT).",
    # Cigna, 2026-11-20
    "Therefore, this position is open only to individuals who are eligible for employment in the United States and who "
    "do not now, or in the future, require visa sponsorship including, but not limited to, H-1B, CPT/OPT/STEM.",
    "Candidates must be able to work without sponsorship, including CPT or OPT.",
    # Cigna's Business Analytics Intern, 2026-11-23: "visa support" rather than "visa sponsorship"
    "Therefore, this temporary internship is open only to individuals who are eligible for employment in the United "
    "States and who would not require visa support including H-1B, CPT/OPT/STEM, etc., now or in the future.",
    # Veeva's Analytics Development Program, 2026-11-23
    "Veeva does not provide sponsorship for employment visa status (e.g., H-1B, OPT, or TN status) for this "
    "employment position.",
    # Sanofi's NCES statistician intern, 2026-11-24
    "Students currently on CPT, OPT, or STEM OPT usually require future sponsorship for long term employment and do "
    "not meet the Internal requirements for this program unless eligible for an alternative long-term status that "
    "does not require company sponsorship.",
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


def test_no_sponsorship_with_cpt_opt_welcome_is_not_an_exclusion() -> None:  # the HP rule's near miss
    e = check("We do not provide visa sponsorship for this role, but OPT and CPT students are welcome.")
    assert "visa_excluded" not in levels(e)
    assert levels(e)["visa_welcome"] == "note"
    # the Cigna rule's near miss: OPT counted among those who don't need sponsorship
    cigna = check("Candidates who do not require sponsorship, including students on OPT, are welcome to apply.")
    assert "visa_excluded" not in levels(cigna)


def test_cpt_opt_welcome_and_unclear_mentions() -> None:
    welcome = check("International students on CPT/OPT are welcome to apply.")
    assert levels(welcome) == {"visa_welcome": "note", "grad_date": "note"}
    unclear = check("Please tell us whether you will need CPT or OPT paperwork.")
    assert levels(unclear)["cpt_opt_mentioned"] == "warning"
    spelled = check("Tell us whether you will need Optional Practical Training.")
    assert levels(spelled)["cpt_opt_mentioned"] == "warning"


@pytest.mark.parametrize("text", [
    "Many opt-in culture events across our diverse community.",  # Formlabs, 2026-11-19
    "There is no opt-out for the safety training.",
    "Employees can opt in to the commuter benefit, which is welcome news.",
])
def test_opting_in_or_out_is_not_cpt_or_opt(text: str) -> None:
    assert not {"cpt_opt_mentioned", "visa_excluded", "visa_welcome"} & set(levels(check(text)))


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


def test_a_us_person_requirement_blocks_a_visa_holder() -> None:  # Anduril, 2026-11-16
    text = ("Must be a U.S. Person due to required access to U.S. export-controlled information or facilities; U.S. "
            "clearance eligibility may be required depending on program.")
    anduril = check(text)
    assert anduril.blocked and levels(anduril)["us_person_required"] == "blocker"
    citizen = PROFILE.model_copy(deep=True)
    citizen.tier2.us_citizen_or_permanent_resident.value = "Yes"
    assert "us_person_required" not in levels(check(text, profile=citizen))
    # a mention without a requirement stays a warning
    assert levels(check("This role involves export-controlled data; deemed export rules may apply."))["export_control"] \
        == "warning"


def test_spelled_out_us_person_statuses_block_a_visa_holder() -> None:  # Atlantic Health, 2026-11-19
    atlantic = check("Must be legally authorized to work in the United States as a U.S. citizen, national, asylee, "
                     "refugee, or lawful permanent resident.", title="Pharmacy (Data Analytics) Intern")
    assert atlantic.blocked and levels(atlantic)["us_person_required"] == "blocker"
    # the immigration law notice many postings carry names the same statuses without requiring them
    notice = check("U.S. citizens, nationals, lawful permanent residents, asylees and refugees are protected from "
                   "citizenship status discrimination.")
    assert "us_person_required" not in levels(notice)


def test_only_us_persons_are_eligible_blocks_a_visa_holder() -> None:  # Moderna, 2026-11-23
    moderna = check("Export Control Notice. This position may involve access to technology or data that is subject to "
                    "U.S. export control laws, including the Export Administration Regulations (EAR). Due to the nature "
                    "of the work and regulatory requirements, only individuals who qualify as U.S. persons (citizens, "
                    "permanent residents, asylees, or refugees) are eligible for this position.")
    assert moderna.blocked and levels(moderna)["us_person_required"] == "blocker"


def test_citizen_or_permanent_resident_blocks_a_visa_holder() -> None:  # Johns Hopkins APL, 2026-11-24
    apl = check("You meet our minimum qualifications for the job if you...\n- Have a minimum 3.0 GPA on a 4.0 scale.\n"
                "- Are a US citizen or permanent resident. To access APL's facilities and networks, US Citizenship or "
                "permanent resident status is required, subject to applicable laws.\n",
                title="2027 Internship - Artificial Intelligence and Complex Systems")
    assert apl.blocked and levels(apl)["us_person_required"] == "blocker"
    assert levels(check("- Are a U.S. citizen or permanent resident"))["us_person_required"] == "blocker"
    citizen = PROFILE.model_copy(deep=True)
    citizen.tier2.us_citizen_or_permanent_resident.value = "Yes"
    assert "us_person_required" not in levels(check("US Citizenship or permanent resident status is required.",
                                                    profile=citizen))


def test_us_abbreviation_does_not_split_the_sentence() -> None:  # ICF, 2026-11-16
    icf = check("Bachelor's degree in computer science.\n- Due to contract requirements, U.S. Citizenship is required\n"
                "Preferred: Python.")
    assert icf.blocked and levels(icf)["citizenship_required"] == "blocker"


# --------------------------------------------------------------------------- 2. degree level


def test_degree_level() -> None:
    # a note, not a warning: he applies anyway and auto-submit still sends it (Vatsal, 2026-11-25, Bandwidth)
    bachelors = levels(check("Currently pursuing a Bachelor's degree in Computer Science."))
    assert bachelors["undergrad_only"] == "note" and "warning" not in bachelors.values()
    assert levels(check("Open to rising juniors or seniors."))["undergrad_only"] == "note"
    both = check("Currently pursuing a BS or MS in Computer Science.")
    assert "undergrad_only" not in levels(both)
    curly = check("Currently pursuing a Bachelor’s or Master’s degree in Computer Science.")
    assert "undergrad_only" not in levels(curly)  # postings often use the curly apostrophe
    higher = check("Currently pursuing a bachelor’s degree (or higher) from an accredited college or university.")
    assert "undergrad_only" not in levels(higher)  # Honeywell Aerospace, 2026-11-20
    gpa = check("Currently pursuing a bachelor's degree in Computer Science with a GPA of 3.0 or higher.")
    assert levels(gpa)["undergrad_only"] == "note" and "grad_students_considered" not in levels(gpa)
    pay = check("Hourly rate: Bachelor's students $40/hr, Master's students $45/hr, PhD students $50/hr.")
    assert levels(pay)["grad_students_considered"] == "note"


# --------------------------------------------------------------------------- 3. timing


def test_semester_roles_raise_the_cpt_hours_question() -> None:
    fall = check("This is a full-time, on-site internship in Seattle.", title="Software Engineer Intern (Fall 2026)")
    flag = next(f for f in fall.flags if f.id == "semester_timing")
    assert flag.level == "warning" and "full-time hours" in flag.message and "on site" in flag.message
    assert "semester_timing" in levels(check("Join our team.", title="Software Co-op, Fall 2027"))


def test_spring_2027_and_co_ops_are_fine_anywhere() -> None:  # Vatsal, 2026-11-16: free from January, open to co-ops
    spring = check("Location: Milwaukee, WI (on-site)", title="Software Co-op, Spring 2027")
    assert "semester_timing" not in levels(spring) and "far_office" not in levels(spring)
    assert levels(spring)["relocation"] == "note" and not spring.blocked
    co_op = check("Location: Durham, NC. 15-20 hours per week during the academic year.", title="Software Engineer Co-op")
    assert "far_office" not in levels(co_op) and not co_op.blocked
    assert levels(check("Location: New York, NY (on-site)", title="Data Intern (Fall 2027)"))["far_office"] == "note"
    assert "semester_timing" not in levels(check("Join our team."))  # summer
    # Winter 2027 runs January to March 2027, inside the free time (Atoms, 2026-11-18); a later winter still clashes
    winter = check("This is a full-time, on-site internship.", title="Software Engineering Intern - 2027 Winter Term")
    assert "semester_timing" not in levels(winter)
    assert "semester_timing" in levels(check("Join our team.", title="Software Engineering Intern - 2028 Winter Term"))


def test_immediate_start_is_checked_against_the_target() -> None:
    title = "New Grad Software Engineer"
    text = "We need someone who can start immediately."
    target = parse_month_year(CFG.immediate_start_target)
    assert levels(check(text, title=title))["immediate_start"] == "warning"  # full-time: from the target month
    assert "immediate_start" not in levels(check(text, title=title, today=date(target.year, target.month, 10)))
    # internships can start now (Vatsal, 2026-11-15: free from January)
    assert levels(check(text, title="Software Engineer Intern"))["immediate_start"] == "note"


# --------------------------------------------------------------------------- 4. graduation date


@pytest.mark.parametrize(
    "text,title,expected",
    [
        ("Join our team.", SUMMER, "December 2027"),  # Vatsal, 2026-11-15: summer internships
        ("Candidates must be graduating between December 2027 and June 2028.", SUMMER, "December 2027"),
        ("Must have a graduation date between December 2026 and June 2027.", SUMMER, "May 2027"),
        ("You must be returning to school after the internship.", SUMMER, "December 2027"),
        ("You must be returning to school after the internship.", "Software Engineer Intern (Summer 2026)", "May 2027"),
        ("Open to the class of 2027.", SUMMER, "December 2027"),
        ("Open to the class of 2027.", "New Grad Software Engineer", "May 2027"),
        ("Expected graduation in Fall 2027.", SUMMER, "December 2027"),
        ("Graduating before or in June 2027.", "Systems Engineer, New Grad 2027", "May 2027"),
        ("Looking for engineers graduating before or by June 2027.", "New Grad 2027", "May 2027"),
        ("Must graduate on or before December 2027.", SUMMER, "December 2027"),
        ("Must graduate on or before June 2027.", SUMMER, "May 2027"),  # the window rules December out
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


def test_far_office_during_the_semester_is_only_a_note() -> None:  # Vatsal, 2026-11-17: he can relocate
    fall = "Software Engineer Intern (Fall 2026)"
    far = check("Location: New York, NY (on-site)", title=fall)
    assert levels(far)["far_office"] == "note" and "far_office" not in [f.id for f in far.flags if f.level == "blocker"]
    assert "far_office" not in levels(check("Location: Bellevue, WA", title=fall))
    assert "far_office" not in levels(check("Location: Remote (US)", title=fall))
    assert levels(check("Location: New York, NY"))["relocation"] == "note"  # summer


# --------------------------------------------------------------------------- 6. deadlines


def test_deadlines_and_urgency() -> None:
    assert levels(check("Apply by March 1, 2027."))["deadline"] == "note"
    assert levels(check("Application deadline: October 1, 2026"))["deadline_passed"] == "warning"
    assert levels(check("Applications are reviewed on a rolling basis."))["urgency"] == "note"
    assert levels(check("We have an urgent need for interns this summer."))["urgency"] == "note"
    # a mission statement isn't a hiring deadline (GE Vernova, 2026-11-24)
    assert "urgency" not in levels(check("Addressing the climate crisis is an urgent global priority."))


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


# --------------------------------------------------------------------------- duplicates (spec 9 step 1)


def dup(n: int, status: str = "applied", url: Optional[str] = None, company: str = "Acme",
        role: str = "Software Engineer Intern", location: Optional[str] = None) -> CompanyApplication:
    return CompanyApplication(id=n, company=company, role_title=role, status=status, date_applied="2026-10-01",
                              job_url=url, location=location)


GH = "https://boards.greenhouse.io/acme/jobs/4567890"


def check_url(url: Optional[str], apps: list[CompanyApplication], role: str = "Software Engineer Intern",
              location: Optional[str] = None) -> Eligibility:
    job = Job(url=url, company="Acme, Inc.", role_title=role, location=location, description_text="Join us.",
              source="jd_file", fetched_at="2026-11-14")
    return check_eligibility(job, CFG, caps=CAPS, applications=apps, profile=PROFILE, today=TODAY)


def test_applying_twice_to_the_same_posting_blocks() -> None:
    by_id = check_url(GH + "?gh_src=linkedin", [dup(1, url="https://job-boards.greenhouse.io/acme/jobs/4567890")])
    assert levels(by_id)["duplicate"] == "blocker" and "same job id" in by_id.flags[0].message
    by_link = check_url("https://acme.com/careers/swe-intern/", [dup(2, url="https://www.acme.com/careers/swe-intern")])
    assert levels(by_link)["duplicate"] == "blocker" and "same link" in by_link.flags[0].message


def test_a_tracked_but_unsent_posting_is_only_noted() -> None:
    e = check_url(GH, [dup(3, status="drafted", url=GH)])
    assert levels(e)["already_tracked"] == "note" and not e.blocked


def test_the_same_title_with_another_id_link_or_location_is_a_different_posting() -> None:
    other_id = check_url(GH, [dup(4, url="https://boards.greenhouse.io/acme/jobs/1111111")])
    assert not {"duplicate", "possible_duplicate"} & set(levels(other_id))
    other_link = check_url("https://acme.com/careers/swe-intern-seattle", [dup(5, url="https://acme.com/careers/swe-intern-nyc")])
    assert not {"duplicate", "possible_duplicate"} & set(levels(other_link))
    no_link_other_place = check_url(None, [dup(6, url=GH, location="New York, NY")], location="Seattle, WA")
    assert "possible_duplicate" not in levels(no_link_other_place)
    no_location = check_url(None, [dup(7, url=GH, location="Seattle, WA")])
    assert "possible_duplicate" not in levels(no_location)  # can't confirm, so no flag


def test_without_a_link_only_company_title_and_location_together_suggest_a_duplicate() -> None:
    e = check_url(None, [dup(8, url=GH, location="Seattle, WA")], location="Seattle, WA")
    assert levels(e)["possible_duplicate"] == "note" and not e.blocked


def test_greenhouse_embed_links_compare_by_token() -> None:
    a = "https://boards.greenhouse.io/embed/job_app?for=acme&token=7654321"
    b = "https://boards.greenhouse.io/embed/job_app?for=acme&token=1234567"
    assert "duplicate" not in levels(check_url(a, [dup(9, url=b)]))
    assert levels(check_url(a, [dup(10, url=a)]))["duplicate"] == "blocker"


def test_a_limit_the_posting_states_is_flagged_but_never_blocks() -> None:
    text = "Application limits: Candidates may submit up to 5 applications during any rolling 30-day period."
    calm = check(text, company="Figma", apps=[app(1, "Figma", when="2026-11-01")])
    assert levels(calm)["posting_limit"] == "note"
    full = check(text, company="Figma", apps=[app(i, "Figma", when="2026-11-01") for i in range(5)])
    assert levels(full)["posting_limit"] == "warning" and full.recommendation == "build"
    old = check(text, company="Figma", apps=[app(i, "Figma", when="2026-09-01") for i in range(5)])
    assert levels(old)["posting_limit"] == "note"


def test_the_title_decides_the_role_type_and_the_season_can_come_after_the_year() -> None:  # 2026-11-15
    sharkninja = check("Our co-op program is famous. This summer internship runs May to August.",
                       title="Summer 2027: Data Science Intern (May to August)", location="Needham, MA")
    assert sharkninja.employment == "internship" and not sharkninja.blocked
    formlabs = check("Join us for the summer.", title="2027 (Summer) - Manufacturing - Manufacturing Test Software Intern")
    assert (formlabs.season, formlabs.season_year, formlabs.grad_date) == ("summer", 2027, "December 2027")


def test_a_calendar_year_graduation_window_is_read() -> None:  # Riot, 2026-11-15
    riot = check("Graduating during the 2028 calendar year with an MS or PhD.",
                 title="Machine Learning Engineering Intern (Masters/PhD) - Summer 2027")
    assert riot.blocked and "grad_window" in levels(riot)


def test_an_internship_that_mentions_summer_without_a_year_is_next_summer() -> None:  # ASML, 2026-11-16
    asml = check("Interns present their findings at the end of summer.", title="Internship - EUV Data Science")
    assert (asml.season, asml.season_year, asml.grad_date) == ("summer", 2027, "December 2027")
    assert check("Join our team.", title="Intern - AI Engineer Platform").grad_date == "May 2027"  # no season named


def test_a_co_op_the_posting_calls_a_summer_internship_is_a_summer_term() -> None:  # Draper, 2026-11-20
    draper = check("Join our team at the Microfabrication Facility in Cambridge, MA as a Summer Intern focusing on "
                   "process automation! There is flexibility regarding hours and program duration.",
                   title="Process Automation Co-Op")
    assert (draper.season, draper.season_year, draper.grad_date) == ("summer", 2027, "December 2027")
    spring = check("A six-month co-op from January to June.", title="Software Engineer Co-op")
    assert spring.grad_date == "May 2027"  # a co-op with no summer named keeps the default


@pytest.mark.parametrize("text,title", [
    # The Aerospace Corporation, 2026-11-20: no season named, but back to school after the internship
    ("Availability to work full-time for a minimum of 10 weeks outside of university term and ability to return to "
     "a degree program full-time after completion of the internship.",
     "2027 Software Systems Engineer and Acquisition Grad Intern"),
    # Rockwell Automation, 2026-11-20: a January to August co-op taken as a term away from school
    ("This is a full-time role from January to August 2027. You will have to take off an academic term for this role.",
     "Co-op, Process Standardization & Delivery Automation (LCS)"),
    # T-Mobile, 2026-11-20
    ("Must have one or more course(s) remaining in a Bachelors or Graduate degree program post internship.",
     "Software Engineering Internship"),
    # Intuitive, 2026-11-20: adjectives between "an" and "academic program"
    ("University Enrollment: Must be currently enrolled in and returning to an accredited degree-seeking academic "
     "program after the internship.", "Data Engineering Intern"),
])
def test_other_ways_of_saying_back_to_school_after_it(text: str, title: str) -> None:
    e = check(text, title=title)
    assert e.grad_date == "December 2027" and not e.blocked, e.grad_date_reason
    assert "returning to school" in e.grad_date_reason


def test_years_listed_before_graduation_year_are_a_window() -> None:  # Great American, 2026-11-20
    gaig = check("Education:\n- Pursuing a degree in Computer Science, Business Administration (with technical focus), "
                 "or related\n- 2028 or 2029 graduation year", title="Desktop AI Intern - Summer 2027")
    assert levels(gaig)["grad_window"] == "blocker"
    assert "Jan 2028 to Dec 2029" in next(f.message for f in gaig.flags if f.id == "grad_window")
    fits = check("Open to 2027 graduates.", title="Software Engineer Intern")
    assert not fits.blocked and fits.grad_date == "May 2027"


def test_a_title_with_words_between_season_and_year_is_read() -> None:  # Kensho, 2026-11-16
    kensho = check("Join our team in Cambridge.", title="Machine Learning Engineer - Summer Intern 2027")
    assert (kensho.season, kensho.season_year, kensho.grad_date) == ("summer", 2027, "December 2027")


def test_a_phd_only_posting_pauses() -> None:  # ASML, 2026-11-16
    asml = check("We are seeking a PhD research intern to explore AI methods.\n- Currently pursuing a PhD in Physics, "
                 "Computer Science, or a related field.", title="Internship- AI Research and Development")
    assert asml.blocked and levels(asml)["phd_only"] == "blocker"
    both = check("Currently pursuing a Master's or PhD degree in Computer Science.", title="ML Intern")
    assert "phd_only" not in levels(both)
    assert levels(check("Join our team.", title="[2027] Data Scientist - PhD Intern"))["phd_only"] == "blocker"


def test_graduating_no_sooner_than_is_a_lower_bound() -> None:  # Micron, 2026-11-16
    micron = check("Must be graduating no sooner than September 2027.", title="Intern - Segment Marketing Data Analyst")
    assert micron.grad_date == "December 2027" and not micron.blocked


def test_slash_joined_seasons_make_one_window() -> None:  # Atoms, 2026-11-18
    atoms = check("Graduating in Summer/Fall/Winter 2027 or Spring/Summer 2028.",
                  title="Software Engineering Intern - 2027 Winter Term")
    assert atoms.grad_date == "December 2027" and not atoms.blocked  # May 2027 is before the window
    assert "Jun 2027 to Aug 2028" in atoms.grad_date_reason
    early = check("Graduating in Spring/Summer 2026.", title="Software Engineering Intern - 2027 Winter Term")
    assert levels(early)["grad_window"] == "blocker"


def test_or_joined_seasons_make_one_window() -> None:  # MongoDB, 2026-11-19: Spring 2028 was dropped
    mongodb = check("Have a minimum of one quarter/semester remaining in university studies after the internship "
                    "concludes. Graduation date in Fall 2027 or Spring 2028.",
                    title="2027 - AI and People Transformation Intern, NYC")
    assert mongodb.grad_date == "December 2027" and not mongodb.blocked
    assert "Sep 2027 to Jun 2028" in mongodb.grad_date_reason
    one = check("Graduation date in Fall 2027.", title="Software Engineering Intern")
    assert one.grad_date == "December 2027" and "Sep 2027 to Dec 2027" in one.grad_date_reason


def test_a_residency_requirement_is_a_note_and_or_later_graduation_blocks() -> None:  # Quest, ServiceNow, 2026-11-17
    quest = check("We are looking for interns who live in the Kansas City area.\nRequirements:\n- Must live in the Kansas "
                  "City or Rolla metro area\n- Must be pursuing a degree in Computer Science")
    assert levels(quest)["residency"] == "note" and not quest.blocked  # Vatsal, 2026-11-17: he can relocate
    assert "residency" not in levels(check("Candidates must reside in the United States."))
    assert "residency" not in levels(check("Must live in the Seattle area or be willing to relocate."))
    servicenow = check("Expected Masters graduation date of May 2028 or later.", title="Solution Engineer Intern")
    assert servicenow.blocked and "grad_window" in levels(servicenow)


def test_bare_citizenship_and_long_clearance_requirements_block() -> None:  # Technology Service Corp, 2026-11-17
    tsc = check("Required Qualifications\n- Coursework using C or C++.\n- U.S. citizenship.\n- Ability to obtain and "
                "maintain a U.S. Department of Defense Secret security clearance.", title="Software Engineer I")
    assert tsc.blocked and {"citizenship_required", "clearance_required"} <= set(levels(tsc))
    assert "citizenship_required" not in levels(check("We value U.S. citizenship education programs."))
    assert "clearance_required" not in levels(check("Ability to obtain results quickly is key."))


def test_eligibility_for_a_clearance_blocks() -> None:  # Applied Research Associates, 2026-11-22
    for text in ("Applicants selected will be subject to a government security investigation and must meet eligibility "
                 "requirements for obtaining a DoD Security Clearance.",
                 "Required Qualifications:\n- Eligibility to obtain a DoD security clearance, which includes U.S. "
                 "citizenship"):
        ara = check(text, title="Junior Software Developer")
        assert ara.blocked and levels(ara)["clearance_required"] == "blocker", text
    assert "clearance_required" not in levels(check("Check your eligibility to obtain a visa for travel."))


def test_a_polygraph_clearance_without_the_word_clearance_blocks() -> None:  # Freedom TSG, 2026-11-24
    freedom = check("Required Qualifications: TS/SCI with Full Scope Polygraph   A bachelor's degree in a technical "
                    "discipline and three (3) years of experience.", title="Junior Software Engineer 817")
    assert freedom.blocked and levels(freedom)["clearance_required"] == "blocker"
    assert "clearance_required" not in levels(check("TS/SCI with polygraph preferred but not required."))


def test_fully_cleared_individuals_is_a_clearance_requirement() -> None:  # Amentum, Hanover MD, 2026-11-24
    amentum = check("The DevOps team positions require fully cleared individuals with an extensive range of "
                    "experience and abilities to develop applications for Computer Network Operations.",
                    title="Software Engineer 1")
    assert amentum.blocked and levels(amentum)["clearance_required"] == "blocker"
    assert "clearance_required" not in levels(check("Our cleared and uncleared engineers work side by side."))


def test_a_posting_in_another_country_is_blocked() -> None:  # GE Aerospace, 2026-11-17: "Remote, Germany"
    ge = check("This is a remote position open only to candidates who live in Germany.", location="Remote, Germany")
    assert ge.blocked and levels(ge)["outside_us"] == "blocker"
    for place in ("Remote", "Boston, MA (New England)", "Albuquerque, New Mexico", "Poland, OH", "Remote - US or Canada",
                  "Toronto, Canada; New York, NY, United States"):
        assert "outside_us" not in levels(check("Build data tools.", location=place)), place


def test_indefinite_work_authorization_blocks_a_visa_holder() -> None:  # Bosch, 2026-11-17
    bosch = check("Indefinite U.S. work authorized individuals only. Future sponsorship for work authorization "
                  "unavailable.")
    assert levels(bosch)["permanent_auth_required"] == "note" and not bosch.blocked  # Vatsal: apply anyway
    assert levels(check("Must be permanently authorized to work in the U.S."))["permanent_auth_required"] == "note"
    assert "permanent_auth_required" not in levels(check("You must be authorized to work in the U.S."))
    assert "permanent_auth_required" not in levels(check("Open to permanent residents and visa holders."))
    citizen = PROFILE.model_copy(deep=True)
    citizen.tier2.us_citizen_or_permanent_resident.value = "Yes"
    assert "permanent_auth_required" not in levels(check("Indefinite U.S. work authorized individuals only.",
                                                         profile=citizen))


def test_class_of_two_years_spans_both() -> None:  # Duetti, 2026-11-17: "Class of 2026 or 2027"
    duetti = check("Recent college graduate (Class of 2026 or 2027) or rising senior, with a degree in Computer Science.")
    assert "grad_window" not in [f.id for f in duetti.flags if f.level == "blocker"]
    assert duetti.grad_date == "December 2027"  # a Summer 2027 internship, inside the window
    only_2026 = check("Open to the Class of 2026.")
    assert "grad_window" in [f.id for f in only_2026.flags if f.level == "blocker"]


def test_a_bare_citizenship_item_with_a_reason_blocks() -> None:  # Amentum, 2026-11-17
    amentum = check("Required Qualifications\n- US Citizenship (due to project requirements)\n- Python")
    assert amentum.blocked and "citizenship_required" in levels(amentum)


def test_indefinite_right_to_work_and_not_eligible_for_sponsorship() -> None:  # S&P Global, Samsung, 2026-11-17
    sp = check("This role is limited to persons with indefinite right to work in the United States.")
    assert levels(sp)["permanent_auth_required"] == "note" and not sp.blocked
    samsung = check("This role is not eligible for immigration sponsorship.")
    assert levels(samsung)["no_sponsorship"] == "warning" and not samsung.blocked


def test_public_trust_blocks_a_recent_arrival() -> None:  # Laansu, 2026-11-18
    laansu = check("Public Trust Clearance: Must have lived in the US for at least 3 years to obtain a Public Trust "
                   "Clearance.")
    assert laansu.blocked and levels(laansu)["public_trust"] == "blocker"


def test_a_june_to_august_date_range_is_a_summer_internship() -> None:  # Constellation, 2026-11-18
    constellation = check("The internship runs from June 7th, 2027 through August 13th, 2027.",
                          title="2027 IT Development Intern - San Diego, CA")
    assert constellation.grad_date == "December 2027"  # a Summer 2027 internship: back at school after


def test_rising_senior_and_bachelors_enrollment_wording_is_noticed() -> None:  # Attain Partners, 2026-11-18
    attain = check("An eight-week paid internship program onsite for undergraduate students who are rising seniors.\n"
                   "- Current enrollment in an accredited Bachelor's degree program")
    assert "undergrad" in " ".join(levels(attain))


def test_a_continuing_education_benefit_is_no_return_to_school() -> None:  # webAI, 2026-11-23
    from helper.eligibility import _RETURNING_RE

    assert not _RETURNING_RE.search("Benefits: 401k match, Flexible PTO Policy, Continuing education support")
    assert _RETURNING_RE.search("You must continue your studies after the internship.")


def test_the_title_s_season_beats_another_in_the_text() -> None:  # Lifeway, 2026-11-23
    from datetime import date

    from helper.eligibility import _season

    text = ("This intern supports UI improvements planned for Winter-Spring 2027. Program Duration: June 7 - August 11, "
            "2027 (11 weeks).")
    assert _season("Summer Internship | Front-End Engineer", text, date(2026, 11, 23)) == ("summer", 2027)
    assert _season("Summer Internship | Back-End", "We hire every summer.", date(2026, 11, 23)) == ("summer", 2027)
    assert _season("Software Engineer Intern - Summer 2027", text, date(2026, 11, 23)) == ("summer", 2027)
    assert _season("Spring 2027 Co-op", "Summer 2026 alumni say hi.", date(2026, 11, 23)) == ("spring", 2027)
    # AMD, 2026-11-24: "Spring/Summer 2027" co-op runs January to August, a summer term, so December 2027
    amd = "Spring/Summer 2027 Masters AI Rack/Cluster Design Engineering Co-Op"
    assert _season(amd, "Duration: January 25, 2027 to August 13, 2027", date(2026, 11, 24)) == ("summer", 2027)
    assert check("You are currently pursuing a Master's degree in Computer Science.", title=amd).grad_date == \
        "December 2027"


def test_dated_seasons_joined_by_a_slash_or_after_graduating_make_a_window() -> None:  # Illumio, 2026-11-23
    from helper.eligibility import _grad_window

    lo, hi, _ = _grad_window(["Currently enrolled in a full-time bachelors or masters Computer Science program with an "
                              "expected graduation date in Winter 2026/Spring 2027"])
    assert lo == (2026, 12) and hi == (2027, 6)  # May 2027 fits
    lo, hi, _ = _grad_window(["Currently pursuing a full-time Bachelor's or Master's in Computer Science, graduating "
                              "Winter 2027 or Spring 2028"])
    assert lo == (2027, 12) and hi == (2028, 6)  # December 2027 fits, May 2027 doesn't


def test_a_summer_start_named_only_by_its_date_is_summer() -> None:  # Illumio and ASML, 2026-11-23
    from helper.eligibility import _season

    illumio = "THIS IS A 12 WEEK INTERNSHIP PROGRAM BEGINNING ON MAY 24TH 2027 OR JUNE 21ST 2027."
    assert _season("AI-Based Scale & Performance Test Automation Intern", illumio) == ("summer", 2027)
    asml = ("Must be available for 12 weeks for one of these sessions starting with the following dates: 6/2/2027 or "
            "6/14/2027")
    assert _season("Internship - Data Analyst", asml) == ("summer", 2027)
    assert _season("Data Analyst", "Start date: June 1, 2027. A full-time role.") == (None, None)  # not an internship


@pytest.mark.parametrize("title", [
    "Markets Quantitative Analyst Summer Internship Program 2028 New York",  # Barclays, 2026-11-24
    "Software Engineer Intern (Summer 2028)",
    "Spring 2028 Co-op: Data Engineering",
])
def test_an_internship_term_after_his_graduation_is_blocked(title):
    e = check("Join our 10 week program. Target graduation within one year of the internship's end.", title=title)
    assert levels(e).get("term_after_graduation") == "blocker"
    assert e.recommendation != "build"


@pytest.mark.parametrize("title", [SUMMER, "Winter 2027 Co-op: Data Engineering", "Software Engineer Intern"])
def test_a_term_before_his_graduation_is_fine(title):
    assert "term_after_graduation" not in levels(check("Join our 10 week program.", title=title))


def test_degree_completed_between_window_blocks_a_later_graduate():
    """General Motors, 2026-11-24: 'Degree completed between May 2025 and August 2026, with availability to begin
    employment in 2026.'"""
    e = check("Degree completed between May 2025 and August 2026, with availability to begin employment in 2026.",
              title="Software Systems Engineer - Early Career")
    assert levels(e).get("grad_window") == "blocker"


def test_a_june_to_august_program_with_no_year_is_the_coming_summer() -> None:  # Cboe, 2026-11-24
    from datetime import date

    from helper.eligibility import _RETURNING_RE, _season

    text = ("To be eligible for this internship, applicants must be enrolled in a university or college program and "
            "should not be scheduled to graduate before December of the year in which the internship takes place. Our "
            "internship program runs from June to August and you will wrap up your internship with a final "
            "presentation.")
    assert _season("Software Engineer Intern (C++)", text, date(2026, 11, 24)) == ("summer", 2027)
    assert _RETURNING_RE.search(text)
    assert _season("Software Engineer", "Our busy season runs from June to August.", date(2026, 11, 24)) == (None, None)
