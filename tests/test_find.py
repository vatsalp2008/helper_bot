"""`helper find` (the job hunt, 2026-11-16): the public feeds and lists, read from saved samples; nothing is fetched."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from helper.find import (Found, dedupe, parse_list, scan_ashby, scan_greenhouse, scan_lever, scan_smartrecruiters,
                         scan_workday, wanted, workday_req)

NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(hours=24)


def iso(delta_hours: float) -> str:
    return (NOW - timedelta(hours=delta_hours)).isoformat()


def test_only_early_career_tech_roles_in_the_us() -> None:
    assert wanted("Software Engineer Intern (Summer 2027)", "Seattle, WA")
    assert wanted("Data Science Intern", "Remote - US")
    assert wanted("Machine Learning Engineer I", "")  # no location shown: kept for the eligibility check
    assert not wanted("Senior Software Engineer", "New York, NY")
    assert not wanted("Mechanical Engineering Intern", "Boston, MA")
    assert not wanted("[2027] Data Scientist - PhD Intern", "San Mateo, CA")
    assert not wanted("Software Engineer Intern", "Toronto, ON, Canada")
    assert not wanted("Software Engineer (Platform Team)", "Palo Alto, CA")  # not early career
    assert wanted("Software Engineer Intern", "Toronto, Canada; New York, NY, United States")


def test_a_state_written_out_is_in_the_us() -> None:  # Garmin, 2026-11-22: "Salem, Oregon" read as outside the U.S.
    assert wanted("Software Engineer 1", "Salem, Oregon")
    assert wanted("Data Analyst Intern", "Des Moines, Iowa")
    assert wanted("Software Engineer Intern", "Albuquerque, New Mexico")
    assert not wanted("Software Engineer 1", "Waterloo, Ontario")


def test_research_roles_are_left_out() -> None:  # Vatsal, 2026-11-17: no research positions
    assert not wanted("Automated Driving Advanced Development Intern, Machine Learning Research (Cambridge)", "Cambridge, MA")
    assert not wanted("Research Scientist Intern, LLMs", "Remote - US")
    assert not wanted("AI Researcher Intern", "Seattle, WA")
    assert wanted("Machine Learning Engineer Intern", "Seattle, WA")


def test_autonomy_roles_count_as_software() -> None:  # Neros, 2026-11-22: its "Autonomy Intern" writes C++ and Python
    assert wanted("Autonomy Intern - Summer 2027", "Torrance, California, United States")
    assert wanted("Autonomous Vehicles Engineering Intern", "Pittsburgh, PA")
    assert not wanted("Autonomy Hardware Intern", "Boston, MA")
    assert not wanted("Autonomy Research Intern", "Boston, MA")
    assert not wanted("Electrical Engineer Intern - Summer 2027", "Torrance, California, United States")


def test_product_management_roles_are_searched() -> None:  # First Resonance, 2026-11-23
    assert wanted("Product Management Intern (Summer 2027)", "El Segundo, CA")
    assert wanted("Associate Product Manager, New Grad 2027", "New York, NY")
    assert not wanted("Senior Product Manager", "New York, NY")
    assert not wanted("Product Manager, Manufacturing Intern", "Austin, TX")
    assert wanted("Product Manager Intern", "Seattle, WA")
    assert not wanted("Engineering Manager, New Grad Programs", "Seattle, WA")


def test_german_style_gender_tags_mean_not_us() -> None:  # GE Aerospace, 2026-11-17: "Remote" hid Germany
    assert not wanted("Applied AI Engineer Intern (m/f/d) – Spring 2027", "Remote")
    assert not wanted("Data Science Intern (m/w/d)", "Remote")
    assert not wanted("Software Engineer Intern (all genders)", "")
    assert not wanted("Software Engineer Intern", "Remote, Germany")
    assert wanted("Software Engineer Intern (AI/ML)", "Remote - US")


def test_remote_in_another_country_is_not_us() -> None:  # NVIDIA Armenia, 2026-11-23
    assert not wanted("Software Engineering Intern", "Armenia, Yerevan; Armenia, Remote")
    assert not wanted("Software Engineering Intern", "Remote - Ukraine")
    assert not wanted("Data Engineer Intern", "Istanbul, Turkey")
    assert not wanted("Software Engineer Intern", "Lisbon, Portugal")
    assert not wanted("Software Engineer Intern", "", url="https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/"
                                                         "job/Armenia-Yerevan/Software-Engineering-Intern_JR2030226")
    # U.S. places that share a name with a country or city elsewhere still count
    assert wanted("Software Engineer Intern", "Atlanta, Georgia")
    assert wanted("Software Engineer Intern", "Remote - US")
    assert wanted("Software Engineer Intern", "Remote, United States; Oslo, Norway")


def test_greenhouse_counts_new_postings_and_reused_ones() -> None:
    jobs = {"jobs": [
        {"id": 1, "title": "Software Engineer Intern", "first_published": iso(3), "updated_at": iso(3),
         "location": {"name": "New York, NY"}, "company_name": "Acme"},
        {"id": 2, "title": "Data Intern", "first_published": iso(24 * 400), "updated_at": iso(2),  # Octaura: reused
         "location": {"name": "New York"}, "company_name": "Acme"},
        {"id": 3, "title": "Software Engineer Intern", "first_published": iso(24 * 30), "updated_at": iso(2),
         "location": {"name": "Austin, TX"}, "company_name": "Acme"},  # a month old, only refreshed
    ]}
    found = scan_greenhouse("acme", SINCE, lambda url: jobs)
    assert [f.job_id for f in found] == ["1", "2"]
    assert found[0].url == "https://job-boards.greenhouse.io/acme/jobs/1" and found[0].ats == "greenhouse"


def test_lever_ashby_smartrecruiters_and_workday_feeds() -> None:
    lever = [{"id": "a1", "text": "Data Analyst Intern", "createdAt": (NOW - timedelta(hours=5)).timestamp() * 1000,
              "categories": {"location": "Chicago, IL"}, "hostedUrl": "https://jobs.lever.co/acme/a1"},
             {"id": "a2", "text": "Old Intern", "createdAt": (NOW - timedelta(days=9)).timestamp() * 1000}]
    assert [f.job_id for f in scan_lever("acme", SINCE, lambda url: lever)] == ["a1"]
    ashby = {"jobs": [{"id": "b1", "title": "AI Engineer Intern", "publishedAt": iso(1), "location": "Remote",
                       "jobUrl": "https://jobs.ashbyhq.com/acme/b1", "isListed": True}]}
    assert scan_ashby("acme", SINCE, lambda url: ashby)[0].url.endswith("/b1")
    sr = {"content": [{"id": "744000000000001", "name": "Software Engineer Intern", "releasedDate": iso(2),
                       "location": {"city": "Austin", "region": "TX", "country": "us"}, "company": {"name": "Acme"}}]}
    found = scan_smartrecruiters("Acme", SINCE, lambda url: sr)
    assert len(found) == 1 and found[0].url == "https://jobs.smartrecruiters.com/Acme/744000000000001"

    def post(url: str, payload: dict[str, Any]) -> Any:
        return {"jobPostings": [
            {"title": "Software Engineer Intern", "postedOn": "Posted Today", "locationsText": "Boston, MA",
             "externalPath": "/job/Boston-MA/Software-Engineer-Intern_JR123-1"},
            {"title": "Data Intern", "postedOn": "Posted 5 Days Ago", "externalPath": "/job/X/Data-Intern_JR9"}]}
    wd = scan_workday(("acme.wd1.myworkdayjobs.com", "acme", "External"), SINCE, post)
    assert len(wd) == 1 and wd[0].job_id == "jr123"
    assert wd[0].url == "https://acme.wd1.myworkdayjobs.com/External/job/Boston-MA/Software-Engineer-Intern_JR123-1"
    assert workday_req("/job/X/Intern---Data-Science--SMAI_JR117444") == "jr117444"


ROW = ('<tr>\n<td><strong><a href="https://simplify.jobs/c/Acme">Acme</a></strong></td>\n<td>Software Engineer Intern'
       ' 🎓</td>\n<td>Austin, TX</td>\n<td><div align="center"><a href="https://jobs.smartrecruiters.com/Acme/'
       '744000000000002?utm_source=Simplify&ref=Simplify"><img src="x.png" width="50" alt="Apply"></a></div></td>\n'
       '<td>0d</td>\n</tr>\n'
       '<tr>\n<td>↳</td>\n<td>Data Intern</td>\n<td>NYC</td>\n<td><div align="center"><a href="https://job-boards.'
       'greenhouse.io/acme/jobs/55"><img src="x.png" alt="Apply"></a></div></td>\n<td>3d</td>\n</tr>')


def test_lists_give_todays_rows_and_teach_feeds() -> None:
    found, links = parse_list(ROW, "list: SimplifyJobs/New-Grad-Positions")
    assert [(f.company, f.title, f.ats) for f in found] == [("Acme", "Software Engineer Intern", "smartrecruiters")]
    assert found[0].url == "https://jobs.smartrecruiters.com/Acme/744000000000002"  # tracking removed
    assert "https://job-boards.greenhouse.io/acme/jobs/55" in links  # an older row still teaches Acme's board


def test_lists_also_give_yesterdays_rows() -> None:
    # 2026-11-22: New-Grad-Positions didn't update from Nov 18 to Nov 21 4:30pm, so Friday's postings (Garmin, Oracle,
    # TransCore...) came in already 1d old and no search saw them. Rows an earlier search saw are dropped as known.
    found, _ = parse_list(ROW.replace("<td>0d</td>", "<td>1d</td>"), "list: SimplifyJobs/New-Grad-Positions")
    assert [f.title for f in found] == ["Software Engineer Intern"]
    assert found[0].posted_at == (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    assert parse_list(ROW.replace("<td>0d</td>", "<td>2d</td>"), "list: x")[0] == []


def test_a_list_row_splits_cities_on_any_line_break() -> None:  # JP Morgan, 2026-11-23: "Columbus, OH</br>NYC"
    for br in ("</br>", "<br>", "<br/>", "<br />"):
        found, _ = parse_list(ROW.replace("<td>Austin, TX</td>", f"<td>Columbus, OH{br}NYC</td>"), "list: x")
        assert found[0].location == "Columbus, OH; NYC"
        assert wanted(found[0].title, found[0].location, found[0].url)


def test_list_city_abbreviations_are_in_the_us() -> None:  # Benchling's "SF", 2026-11-23
    assert wanted("Software Engineer Intern", "SF")
    assert wanted("Software Engineer Intern", "SF Bay Area")
    assert wanted("Data Science Intern", "Bay Area, CA")

def test_packaging_means_the_engineering_discipline_only() -> None:  # Calyx Containers, 2026-11-23
    assert wanted("Junior Software Developer - Print & Packaging", "West Valley City, UT")
    assert not wanted("Packaging Engineer Intern", "Austin, TX")
    assert not wanted("Packaging Engineering Co-op", "Austin, TX")

def test_dedupe_drops_applied_listed_and_repeated_postings() -> None:
    a = Found(url="https://job-boards.greenhouse.io/acme/jobs/1", company="Acme", title="SWE Intern", location="NY",
              ats="greenhouse", source="greenhouse", job_id="1")
    same_from_list = Found(url="https://acme.com/careers/1", company="Acme", title="SWE Intern", location="NY",
                           ats="generic", source="list: x")
    applied = Found(url="https://jobs.lever.co/acme/x9", company="Acme", title="Data Intern", location="NY",
                    ats="lever", source="lever", job_id="x9")
    sr = Found(url="https://jobs.smartrecruiters.com/Acme/7", company="Acme", title="ML Intern", location="NY",
               ats="smartrecruiters", source="smartrecruiters", job_id="7")
    out = dedupe([same_from_list, a, applied, sr], known_urls={"https://jobs.lever.co/acme/x9"}, known_ids=set())
    assert [f.url for f in out] == [a.url, sr.url]  # the feed's link wins over the list's copy
    assert [f.how for f in out] == ["helper fills", "you apply"]


def test_dedupe_matches_a_lists_application_link_to_the_feeds_posting() -> None:  # DriveWealth, 2026-11-18
    feed = Found(url="https://jobs.ashbyhq.com/drivewealth/85147395-3dfa-4c16-880f-8c7522a538ec",
                 company="DriveWealth", title="AI CAP People Analytics Intern - Summer 2027", location="Chicago, IL",
                 ats="ashby", source="ashby", job_id="85147395-3dfa-4c16-880f-8c7522a538ec")
    listed = Found(url="https://jobs.ashbyhq.com/drivewealth/85147395-3dfa-4c16-880f-8c7522a538ec/application?embed=true",
                   company="DriveWealth", title="AI CAP People Analytics Intern", location="Chicago, IL; NYC",
                   ats="ashby", source="list: SimplifyJobs/Summer2027-Internships")
    assert [f.url for f in dedupe([listed, feed], known_urls=set(), known_ids=set())] == [feed.url]
    # and once it is on the list, the application link is the same posting
    assert dedupe([listed], known_urls={feed.url}, known_ids=set()) == []


def test_dedupe_keeps_new_postings_behind_a_page_address_every_job_shares() -> None:  # 0 new postings, 2026-11-18
    known = {"https://boards.greenhouse.io/embed/job_app?for=acme&token=4606014005",
             "https://www.jumptrading.com/hr/job?jid=7", "https://www.vantage.sh/careers?gh_jid=5551234"}
    new = [Found(url="https://boards.greenhouse.io/embed/job_app?for=beta&token=4608584101", company="Beta",
                 title="SWE Intern", location="NY", ats="greenhouse", source="list: x"),
           Found(url="https://www.jumptrading.com/hr/job?jid=8", company="Jump Trading", title="Quant Dev Intern",
                 location="Chicago, IL", ats="generic", source="list: x"),
           Found(url="https://www.example.com/careers?gh_jid=7771234", company="Example", title="Data Intern",
                 location="Remote", ats="generic", source="list: x")]
    assert [f.url for f in dedupe(new, known_urls=known, known_ids=set())] == [f.url for f in new]
    # a path that names the job is still the same posting on another host
    moved = Found(url="https://boards.greenhouse.io/acme/jobs/4606014005", company="Acme", title="SWE Intern",
                  location="NY", ats="generic", source="list: x")
    assert dedupe([moved], known_urls={"https://job-boards.greenhouse.io/acme/jobs/4606014005"}, known_ids=set()) == []


def test_noise_from_the_first_live_run_is_filtered() -> None:  # 2026-11-16
    assert not wanted("Water Resources Engineer – Entry Level (Hybrid)", "Minneapolis, MN")
    assert not wanted("Associate Scientist, Combination Product Characterization", "USA - New Jersey - Rahway")
    assert not wanted("Entry Level Software Engineer - SECRET", "US-VA-Dahlgren")
    assert not wanted("Associate Forward Deployed Solution Engineer Intern (Bachelors Summer 2027)", "Santa Clara, CA")
    assert wanted("Forward Deployed Solution Engineer Intern (Masters Summer 2027)", "Santa Clara, CA")
    assert not wanted("Graduate Test Software Engineer", "2 Locations",
                      "https://leonardocompany.wd3.myworkdayjobs.com/LeonardoCareerSite/job/GB---Edinburgh/Graduate_R1")
    assert wanted("Software Engineer Intern", "2 Locations", "https://acme.wd1.myworkdayjobs.com/x/job/Boston-MA/SWE_R1")
    assert not wanted("Summer 2027 Business Development, Physical & Edge AI Internship", "Bellevue, Washington")
    assert wanted("Summer 2027 AI Engineering Internship", "Bellevue, Washington")


def test_supply_chain_and_business_analyst_roles_count() -> None:  # Vatsal, 2026-11-17: a wide range of roles
    assert wanted("Supply Chain Analyst Intern", "Atlanta, GA")
    assert wanted("Business Analyst Intern (Summer 2027)", "New York, NY")
    assert wanted("Enterprise AI Associate", "New York City")
