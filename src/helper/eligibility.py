"""Eligibility check before a resume is built (spec 8): posting flags, the graduation date, and company caps (8.3).

Explicit or physical blockers mean "recommend skipping and ask Vatsal" (exit 4):
- a named F-1/OPT/CPT exclusion, or a citizenship or clearance requirement his profile doesn't meet;
- an on-site office far from Seattle during the semester;
- a graduation window that neither May 2027 nor December 2027 fits.
Everything else is flagged and the resume is built. Company limits never block: they are flagged first, then the
resume is built and the application goes ahead (Vatsal, 2026-11-14).
"""

from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field

from .ats import detect_ats, job_id_from_url, normalize_url
from .companies import contains_word, norm_title, same_company
from .dates import parse_month_year
from .keywords import Keywords
from .models import Cap, CapsFile, Config, Job, Profile, StrictModel

FlagLevel = Literal["blocker", "warning", "note"]
Employment = Literal["internship", "co_op", "full_time", "unknown"]
SUBMITTED = ("applied", "submitted_unverified")  # these count toward a cap
IN_PROGRESS = ("drafted", "filled")


class Flag(StrictModel):
    id: str
    level: FlagLevel
    message: str
    evidence: list[str] = Field(default_factory=list)  # posting text, quoted


class CompanyApplication(StrictModel):
    """An application row as the eligibility and duplicate checks see it."""

    id: int
    company: str
    role_title: str
    status: str
    date_applied: Optional[str] = None
    keyword_match_pct: Optional[float] = None
    job_url: Optional[str] = None
    job_id: Optional[str] = None
    ats: Optional[str] = None
    run_id: Optional[str] = None
    notion_page_id: Optional[str] = None
    location: Optional[str] = None


class Duplicate(StrictModel):
    application: CompanyApplication
    kind: Literal["exact", "possible"]  # exact: same job id or link; possible: see find_duplicates
    reason: str


def _norm_location(text: Optional[str]) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def find_duplicates(url: Optional[str], company: str, role_title: str, applications: list[CompanyApplication],
                    location: Optional[str] = None) -> list[Duplicate]:
    """Spec 9 step 1. A posting is identified by its job id or its link. Postings often share a title while having
    different job ids, links or locations, so a title never makes two postings the same (Vatsal, 2026-11-14). Only
    when there is no link or id to compare does a match on company, title AND location count, as "possible"."""
    ats, job_id, norm, title = detect_ats(url), job_id_from_url(url), normalize_url(url), norm_title(role_title)
    place = _norm_location(location)
    found: list[Duplicate] = []
    for a in applications:
        a_ats, a_id = a.ats or detect_ats(a.job_url), a.job_id or job_id_from_url(a.job_url)
        if job_id and a_id and ats == a_ats and ats != "unknown":
            if job_id == a_id:
                found.append(Duplicate(application=a, kind="exact", reason="same job id"))
            continue  # both have ids: different ids are different postings
        a_norm = normalize_url(a.job_url)
        if norm and a_norm:
            if norm == a_norm:
                found.append(Duplicate(application=a, kind="exact", reason="same link"))
            continue  # both have links: different links are different postings
        if (title and place and same_company(a.company, company) and norm_title(a.role_title) == title
                and _norm_location(a.location) == place):
            found.append(Duplicate(application=a, kind="possible",
                                   reason="same company, title and location; no link to compare"))
    return found


class Eligibility(StrictModel):
    recommendation: Literal["build", "skip_and_ask"]
    flags: list[Flag]
    grad_date: str
    grad_date_reason: str
    employment: Employment
    season: Optional[str] = None  # "summer", "fall", "spring", "winter"
    season_year: Optional[int] = None
    location: Optional[str] = None
    keyword_coverage_pct: Optional[float] = None  # share of the posting's technologies the master list has
    company_applications: list[CompanyApplication] = Field(default_factory=list)
    duplicates: list[Duplicate] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.level == "blocker" for f in self.flags)


# --------------------------------------------------------------------------- posting text

_MONTHS = (r"January|February|March|April|May|June|July|August|September|October|November|December"
           r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec")
_SEASONS = r"Spring|Summer|Fall|Autumn|Winter"
_WHEN = rf"(?:(?:{_MONTHS})\.?\s+\d{{4}}|(?:{_SEASONS})\s+\d{{4}}|\d{{4}})"

_VISA = r"(?:F-?1|STEM OPT|OPT|CPT|student visas?|international students?)"
_EXCLUSION_RES = [
    re.compile(rf"(?i)\b{_VISA}\b[^.\n]{{0,80}}?\b(?:(?:are|is)\s+not\s+(?:eligible|considered|accepted|supported)"
               r"|ineligible|not eligible|(?:will|can)\s*not be considered|won't be considered|cannot be considered)"),
    re.compile(rf"(?i)\b(?:not eligible|ineligible|not open to|not available (?:to|for)|(?:unable to|cannot|can't|will not|"
               rf"won't|do not|does not|not able to) (?:consider|accept|hire|support|sponsor|employ))\b[^.\n]{{0,100}}?\b{_VISA}\b"),
    re.compile(r"(?i)\bno\s+(?:F-?1|OPT|CPT)\b"),
]
_VISA_POSITIVE_RE = re.compile(rf"(?i)\b{_VISA}\b[^.\n]{{0,60}}?\b(?:welcome|are eligible|is eligible|encouraged|supported)\b")
_CPT_OPT_RE = re.compile(r"(?i)\b(?:CPT|OPT)\b")
_NO_SPONSOR_RE = re.compile(
    r"(?i)\bwithout (?:the need for |requiring |needing |requiring the need for )?(?:current or future |future |"
    r"employer )?(?:visa |immigration )?sponsorship|\bnot require (?:visa |immigration )?sponsorship|\bnow (?:and|or) in "
    r"the future\b|\b(?:will not|won't|unable to|cannot|can't|do not|does not|not able to) (?:offer |provide |support )?"
    r"(?:visa |immigration |employment visa )?sponsor|\bno (?:visa |immigration )?sponsorship|\bsponsorship (?:is )?not "
    r"(?:available|offered|provided)|\bmust be (?:legally )?authorized to work in the (?:U\.?S\.?|United States)"
)
_EXPORT_RE = re.compile(
    r"(?i)\bexport[- ]control|\bITAR\b|\bexport administration regulations|\bU\.?S\.? persons?\b|\bdeemed export"
    r"|\bgovernment authori[sz]ation|\bexport licen[cs]e"
)
_US_PERSON_RE = re.compile(
    r"(?i)\b(?:must be|required to be|need to be|candidates? must be)\b[^.\n]{0,20}?\bU\.?S\.? persons?\b"
    r"|\bU\.?S\.? persons? (?:status )?(?:is |are )?(?:required|only)\b|\bonly (?:open to )?U\.?S\.? persons\b"
)
_CITIZEN_RE = re.compile(
    r"(?i)\b(?:must be|requires?|required to be|only|open to)\b[^.\n]{0,40}?\bU\.?S\.? citizens?(?:hip)?\b"
    r"|\bU\.?S\.? citizens?(?:hip)? (?:is |are )?(?:required|only)\b"
)
_CLEARANCE_RE = re.compile(
    r"(?i)\b(?:active|current|secret|top secret|TS/SCI|security) clearance\b[^.\n]{0,60}?\b(?:required|must|needed)\b"
    r"|\b(?:must|required to|ability to|able to|eligible to) (?:hold|have|obtain|maintain)[^.\n]{0,30}?\bclearance\b"
)
_UNDERGRAD_RE = re.compile(
    r"(?i)\b(?:pursuing|enrolled in|working towards?|candidates? for|currently in)\s+(?:a |an |your )?"
    r"(?:bachelor['’]?s|B\.?S\.?|B\.?A\.?|undergraduate)(?: degree| program)?\b"
    r"|\bundergraduate (?:students?|candidates?) only\b|\bonly (?:open to )?undergraduates?\b"
    r"|\b(?:rising )?(?:sophomores?|juniors? or seniors?|sophomore or junior)\b"
)
_PHD_ONLY_RE = re.compile(
    r"(?i)\b(?:pursuing|enrolled in|candidates? for|currently in|working towards?)\s+(?:a |an |your )?"
    r"(?:Ph\.?D|doctora)|\bPh\.?D\.?\s*(?:research\s+)?(?:intern|internship|students?|candidates?)\b"
)
_MASTERS_RE = re.compile(r"(?i)\b(?:master['’]?s|M\.?S\.?(?!\w)|MSc|graduate students?)")
_DEGREE_ASK_RE = re.compile(r"(?i)\b(?:pursuing|enrolled|degree|students?)\b")
_GRAD_LEVEL_RE = re.compile(r"(?i)\b(?:master['’]?s|M\.?S\.?|graduate (?:students?|degree|program)|Ph\.?D\.?|MBA)\b")
_IMMEDIATE_RE = re.compile(
    r"(?i)\bimmediate(?:ly)? start|\bstart immediately\b|\bstart(?:ing)? (?:ASAP|as soon as possible)\b"
    r"|\bimmediate (?:opening|availability|start date)\b"
)
_FULL_TIME_HOURS_RE = re.compile(r"(?i)\bfull[- ]time\b|\b40\s*(?:hours|hrs)\b")
_ONSITE_RE = re.compile(r"(?i)\bon[- ]?site\b|\bin[- ]office\b|\bin[- ]person\b|\bhybrid\b|\brelocat")
_REMOTE_RE = re.compile(r"(?i)\b(?:fully |100% )?remote\b")
_NOT_REMOTE_RE = re.compile(r"(?i)\b(?:not|no|non)[- ]remote\b|\bremote work is not\b")
_SEATTLE_RE = re.compile(
    r"(?i)\b(?:Seattle|Bellevue|Redmond|Kirkland|Bothell|Renton|Issaquah|Sammamish|Lynnwood|Everett|Tukwila"
    r"|Mercer Island|SeaTac|Shoreline|Woodinville|Kenmore|Puget Sound)\b"
)
_LOCATION_LINE_RE = re.compile(r"(?im)^\s*(?:location|locations|office|based in)\s*:\s*(?P<loc>.+?)\s*$")
_RETURNING_RE = re.compile(
    r"(?i)\breturn(?:ing)? to (?:school|university|college|your studies|campus|your degree program)"
    r"|\bcontinu(?:e|ing) (?:your |their )?(?:studies|education|enrollment|degree)"
    r"|\b(?:remain|still be) enrolled\b|\bat least one (?:semester|quarter|term) (?:of school )?(?:remaining|left)"
)
_SEASON_RE = re.compile(rf"(?i)\b({_SEASONS})\s*(?:of\s+)?('?\d{{2}}|\d{{4}})\b")
_YEAR_SEASON_RE = re.compile(rf"(?i)\b(\d{{4}})\s*[(\[-]?\s*({_SEASONS})\b")  # "2027 (Summer)" (Formlabs)
# Titles only: a season and a year a few words apart ("Summer Intern 2027", "Summer Internship Program 2027").
_TITLE_SEASON_GAP_RE = re.compile(rf"(?i)\b({_SEASONS})\b(?:[\s,/&-]+[A-Za-z]+){{1,3}}[\s,(-]+(20\d\d)\b")
_DEADLINE_RE = re.compile(
    rf"(?i)(?:apply by|application deadline|deadline(?: to apply)?|applications? (?:close|closes|are due|due|will "
    rf"close)|posting (?:closes|will close|expires)|accepting applications (?:until|through))\s*(?:is|on|by|:)?\s*"
    rf"(?P<date>(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}|\d{{1,2}}/\d{{1,2}}/\d{{2,4}}"
    rf"|(?:{_MONTHS})\.?\s+\d{{4}})"
)
_STATED_LIMIT_RE = re.compile(
    r"(?i)submit (?:up to|a maximum of|no more than) (?P<n>\d+) applications? (?:during|in|within|per|every) "
    r"(?:any |a )?(?:rolling )?(?P<days>\d+)[- ]day"
)
_URGENCY_RE = re.compile(r"(?i)\brolling basis\b|\bapply early\b|\bfill(?:ed)? quickly\b|\burgent(?:ly)?\b"
                         r"|\bpriority deadline\b|\blimited (?:number of )?(?:spots|openings)\b")


# A period after these doesn't end a sentence: "Due to contract requirements, U.S. Citizenship is required" (ICF).
_ABBREV_END = re.compile(r"(?i)(?:\bU\.S|\bU\.K|\be\.g|\bi\.e|\bInc|\bLtd|\bCorp|\bCo|\bJr|\bSr|\bDr|\bMr|\bMs|\bSt|\bNo|"
                         r"\bvs|\bapprox)\.$")


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•*·▪◦ \t").strip()
        if not line:
            continue
        parts: list[str] = []
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'])", line):
            if parts and _ABBREV_END.search(parts[-1]):
                parts[-1] = f"{parts[-1]} {s}"
            else:
                parts.append(s)
        out.extend(s.strip() for s in parts if s.strip())
    return out


def _quote(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _matching(sentences: list[str], *patterns: re.Pattern[str]) -> list[str]:
    return [_quote(s) for s in sentences if any(p.search(s) for p in patterns)]


def _first_month(text: str) -> Optional[tuple[int, int]]:
    """(year, month) where a month, season or year starts."""
    r = _when_range(text)
    return r[0] if r else None


def _when_range(text: str) -> Optional[tuple[tuple[int, int], tuple[int, int]]]:
    t = text.strip().rstrip(".,")
    my = parse_month_year(t)
    if my:
        return (my.year, my.month), (my.year, my.month)
    m = re.fullmatch(rf"(?i)({_SEASONS})\s+(\d{{4}})", t)
    if m:
        y = int(m.group(2))
        season = m.group(1).lower()
        return {
            "spring": ((y, 3), (y, 6)),
            "summer": ((y, 6), (y, 8)),
            "fall": ((y, 9), (y, 12)),
            "autumn": ((y, 9), (y, 12)),
            "winter": ((y, 12), (y + 1, 2)),
        }[season]
    m = re.fullmatch(r"\d{4}", t)
    if m:
        y = int(t)
        return (y, 1), (y, 12)
    return None


def _grad_window(sentences: list[str]) -> tuple[Optional[tuple[int, int]], Optional[tuple[int, int]], list[str]]:
    """Lower and upper bounds on the graduation date the posting asks for, with the sentences that say so."""
    lower: Optional[tuple[int, int]] = None
    upper: Optional[tuple[int, int]] = None
    evidence = []
    grad = re.compile(r"(?i)\bgraduat\w*|\bclass of\b|\bdegree (?:completion|conferral)")
    for s in sentences:
        if not grad.search(s):
            continue
        found = False
        for m in re.finditer(rf"(?i)\b(?:between|from)\s+({_WHEN})\s+(?:and|to|through|-|–)\s+({_WHEN})"
                             rf"|({_WHEN})\s*(?:-|–|to|through)\s*({_WHEN})", s):
            a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            ra, rb = _when_range(a), _when_range(b)
            if ra and rb:
                lower = max(lower, ra[0]) if lower else ra[0]
                upper = min(upper, rb[1]) if upper else rb[1]
                found = True
        if not found:
            # Longest phrases first: "before or in June 2027" is an upper bound, not "in June 2027".
            for m in re.finditer(rf"(?i)\b(before or (?:in|by|on)|on or before|by or before|no later than|prior to"
                                 rf"|on or after|no (?:earlier|sooner) than|not (?:earlier than|before)|by|before|in|during"
                                 rf"|after)\s+(?:the\s+)?"
                                 rf"(?:calendar\s+year\s+(?:of\s+)?)?({_WHEN})"  # "during the 2028 calendar year" (Riot)
                                 rf"|\bclass of\s+(\d{{4}})", s):
                if m.group(3):
                    r = _when_range(m.group(3))
                    word = "in"
                else:
                    r = _when_range(m.group(2))
                    word = " ".join(m.group(1).lower().split())
                if not r:
                    continue
                if word in ("by", "no later than", "in", "during", "on or before", "by or before") or word.startswith(
                        "before or"):
                    upper = min(upper, r[1]) if upper else r[1]
                if word in ("before", "prior to"):
                    upper = min(upper, _month_before(r[0])) if upper else _month_before(r[0])
                if word in ("in", "during", "after", "no earlier than", "no sooner than", "not earlier than",
                            "not before", "on or after"):  # "graduating no sooner than September 2027" (Micron)
                    start = r[0] if word != "after" else _month_after(r[1])
                    lower = max(lower, start) if lower else start
                found = True
        if found:
            evidence.append(_quote(s))
    return lower, upper, evidence


def _month_before(ym: tuple[int, int]) -> tuple[int, int]:
    y, m = ym
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _month_after(ym: tuple[int, int]) -> tuple[int, int]:
    y, m = ym
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _fmt(ym: tuple[int, int]) -> str:
    return f"{calendar.month_abbr[ym[1]]} {ym[0]}"


def _employment(title: str, text: str) -> Employment:
    head = f"{title}\n{text[:1500]}"
    if re.search(r"(?i)\bco-?op\b", title):  # the title decides first: SharkNinja's intern posting mentions co-ops
        return "co_op"
    if re.search(r"(?i)\bintern(?:ship)?s?\b", title):
        return "internship"
    if re.search(r"(?i)\bco-?op\b", head):
        return "co_op"
    if re.search(r"(?i)\binternship\b", head):
        return "internship"
    if re.search(r"(?i)\bnew grad|\bfull[- ]time\b|\bentry[- ]level\b|\buniversity grad", head):
        return "full_time"
    return "unknown"


def _season(title: str, text: str, today: Optional[date] = None) -> tuple[Optional[str], Optional[int]]:
    for source in (title, text[:1500]):
        m = _SEASON_RE.search(source)
        if m:
            raw = m.group(2).lstrip("'")
            year = int(raw) + 2000 if len(raw) == 2 else int(raw)
            season = m.group(1).lower().replace("autumn", "fall")
            return season, year
        m = _YEAR_SEASON_RE.search(source)
        if m:
            return m.group(2).lower().replace("autumn", "fall"), int(m.group(1))
    m = _TITLE_SEASON_GAP_RE.search(title)
    if m:  # "Machine Learning Engineer - Summer Intern 2027" (Kensho)
        return m.group(1).lower().replace("autumn", "fall"), int(m.group(2))
    if today is not None and re.search(r"(?i)\bintern", title) and re.search(r"(?i)\bsummer\b", text):
        # "an end of summer presentation" (ASML): the coming summer; from August on, next year's
        return "summer", today.year + 1 if today.month >= 8 else today.year
    return None, None


def _term_end(season: Optional[str], year: Optional[int]) -> Optional[tuple[int, int]]:
    if not season or not year:
        return None
    return {"spring": (year, 5), "summer": (year, 8), "fall": (year, 12), "winter": (year, 3)}[season]


def _parse_deadline(text: str) -> Optional[date]:
    t = re.sub(r"(\d)(st|nd|rd|th)", r"\1", text.replace(",", " ")).replace(".", "")
    t = " ".join(t.split())
    for fmt in ("%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%m/%d/%y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(t.replace("Sept ", "Sep "), fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- companies and caps


def cap_for(company: str, caps: CapsFile) -> Optional[Cap]:
    return next((c for c in caps.caps if any(contains_word(company, f) for f in [c.company, *c.match])), None)


def window_start(today: date, cap: Cap) -> date:
    if cap.window_days:
        return today - timedelta(days=cap.window_days)
    months = cap.window_months or 0
    y, m = today.year, today.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(today.day, calendar.monthrange(y, m)[1]))


def _window_text(cap: Cap) -> str:
    return f"{cap.window_days} days" if cap.window_days else f"{cap.window_months} months"


def load_applications(db_path: Path) -> list[CompanyApplication]:
    """Every application row (read-only). An absent database means no applications yet."""
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, company, role_title, status, COALESCE(date_applied, substr(created_at, 1, 10)) AS date_applied, "
            "keyword_match_pct, job_url, job_id, ats, run_id, notion_page_id, location FROM applications"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [CompanyApplication(**dict(r)) for r in rows]


# --------------------------------------------------------------------------- the check


def check_eligibility(
    job: Job,
    cfg: Config,
    *,
    caps: CapsFile,
    applications: list[CompanyApplication],
    profile: Optional[Profile] = None,
    keywords: Optional[Keywords] = None,
    today: Optional[date] = None,
) -> Eligibility:
    today = today or date.today()
    text = job.description_text
    sentences = _sentences(f"{job.role_title}\n{text}")
    flags: list[Flag] = []

    def flag(fid: str, level: FlagLevel, message: str, evidence: Optional[list[str]] = None) -> None:
        flags.append(Flag(id=fid, level=level, message=message, evidence=evidence or []))

    # 0. Duplicates (spec 9 step 1): by job id or link; a title alone never counts.
    posting_location = job.location or (m.group("loc").strip() if (m := _LOCATION_LINE_RE.search(text)) else None)
    duplicates = find_duplicates(job.url, job.company, job.role_title, applications, posting_location)
    for kind, level, fid, lead in (
        ("exact", "blocker", "duplicate", "You already applied to this posting"),
        ("exact", "note", "already_tracked", "This posting is already in your tracker"),
        ("possible", "note", "possible_duplicate", "Possibly already in your tracker"),
    ):
        sent = fid != "already_tracked"
        hits = [d for d in duplicates if d.kind == kind and
                (fid == "possible_duplicate" or (d.application.status not in IN_PROGRESS + ("skipped", "failed")) == sent)]
        if hits:
            shown = "; ".join(f"#{d.application.id} {d.application.company}, {d.application.role_title} "
                              f"({d.application.status}{', ' + d.application.date_applied if d.application.date_applied else ''}"
                              f", {d.reason})" for d in hits[:3])
            more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""
            flag(fid, level, f"{lead}: {shown}{more}.")

    # 1. Work authorization.
    excluded = _matching(sentences, *_EXCLUSION_RES)
    if excluded:
        flag("visa_excluded", "blocker", "The posting names F-1, OPT or CPT candidates as ineligible.", excluded)
    else:
        positive = _matching(sentences, _VISA_POSITIVE_RE)
        if positive:
            flag("visa_welcome", "note", "The posting welcomes CPT/OPT or international students.", positive)
        no_sponsor = _matching(sentences, _NO_SPONSOR_RE)
        if no_sponsor:
            flag("no_sponsorship", "warning", "It won't sponsor visas. CPT/OPT isn't sponsorship, so the resume still "
                 "gets built; confirm with recruiting that CPT/OPT is fine. Your sponsorship answer stays No.", no_sponsor)
        unclear = [s for s in _matching(sentences, _CPT_OPT_RE) if s not in positive]
        if unclear:
            flag("cpt_opt_mentioned", "warning", "It mentions CPT/OPT without saying clearly whether they count. Read "
                 "the wording; the form's sponsorship question may pause the run.", unclear)
    is_citizen = bool(profile and (profile.tier2.us_citizen_or_permanent_resident.value or "").lower() == "yes")
    # "Must be a U.S. Person due to required access to U.S. export-controlled information" (Anduril, 2026-11-16):
    # a U.S. person is a citizen, permanent resident, refugee or asylee, which a visa holder isn't
    us_person = _matching(sentences, _US_PERSON_RE)
    if us_person and not is_citizen:
        flag("us_person_required", "blocker", "It requires U.S. person status (citizen, permanent resident, refugee "
             "or asylee), which your profile says you don't have.", us_person)
    export = [s for s in _matching(sentences, _EXPORT_RE) if s not in us_person or is_citizen]
    if export:
        flag("export_control", "warning", "Export control: the role may require government authorization.", export)
    citizen = _matching(sentences, _CITIZEN_RE)
    if citizen and not is_citizen:
        flag("citizenship_required", "blocker", "It requires U.S. citizenship, which your profile says you don't have.",
             citizen)
    clearance = _matching(sentences, _CLEARANCE_RE)
    has_clearance = bool(profile and (profile.tier2.security_clearance.value or "").lower() not in ("", "no", "none"))
    if clearance and not has_clearance:
        flag("clearance_required", "blocker", "It requires a security clearance; you have none, and getting one needs "
             "U.S. citizenship.", clearance)

    # 2. Degree level.
    undergrad = [s for s in _matching(sentences, _UNDERGRAD_RE) if not _GRAD_LEVEL_RE.search(s)]
    if undergrad:
        flag("undergrad_only", "warning", "It asks for undergraduates; as an MS student you may be screened out.",
             undergrad)
    # "We are seeking a PhD research intern ... Currently pursuing a PhD in ..." (ASML, 2026-11-16)
    phd_only = [s for s in _matching([job.role_title, *sentences], _PHD_ONLY_RE) if not _MASTERS_RE.search(s)]
    if phd_only and not any(_MASTERS_RE.search(s) for s in _matching(sentences, _DEGREE_ASK_RE)):
        flag("phd_only", "blocker", "It's for PhD students; you're in a master's program.", phd_only[:2])
    grad_ok = _matching(sentences, re.compile(_GRAD_LEVEL_RE.pattern + r"[^.\n]{0,60}?\b(?:students?|candidates?|"
                                              r"degree|program|rate|/hr|per hour)\b", re.I))
    if grad_ok and not undergrad:
        flag("grad_students_considered", "note", "Graduate students are considered.", grad_ok[:2])

    # 3. Timing.
    employment = _employment(job.role_title, text)
    season, season_year = _season(job.role_title, text, today)
    # Vatsal, 2026-11-16: free from January 2027 and open to co-ops, so Spring 2027 roles and co-ops without a
    # named term are fine anywhere; fall terms still clash with classes (back at school after the summer)
    free = parse_month_year(cfg.free_from)
    spring_free = bool(free and season == "spring" and season_year == free.year)
    in_semester = not spring_free and season in ("fall", "spring", "winter") and employment in ("co_op", "internship")
    if in_semester:
        term = f"{season.title()} {season_year}" if season and season_year else "This co-op"
        extra = []
        if _FULL_TIME_HOURS_RE.search(text):
            extra.append("it asks for full-time hours")
        if _ONSITE_RE.search(text):
            extra.append("it is on site or hybrid")
        detail = f" ({'; '.join(extra)})" if extra else ""
        flag("semester_timing", "warning", f"{term} runs during your Seattle classes{detail}. CPT hours are limited in "
             "the semester: ask how many hours a week it needs.")
    immediate = _matching(sentences, _IMMEDIATE_RE)
    target = parse_month_year(cfg.immediate_start_target)
    if immediate and employment == "internship":  # Vatsal, 2026-11-15: internships can start now (free from January)
        flag("immediate_start", "note", "It wants an immediate start; fine for an internship (you can start now).",
             immediate)
    elif immediate and target and (today.year, today.month) < (target.year, target.month):
        flag("immediate_start", "warning", f"It wants an immediate start; your target for immediate-start roles is "
             f"{cfg.immediate_start_target}.", immediate)

    # 4. Graduation window.
    grad_date, grad_reason, grad_flags = _choose_grad_date(cfg, sentences, employment, season, season_year)
    flags.extend(grad_flags)

    # 5. Location.
    location = job.location
    if not location:
        m = _LOCATION_LINE_RE.search(text)
        location = m.group("loc").strip() if m else None
    remote = bool(location and _REMOTE_RE.search(location)) or (
        bool(_REMOTE_RE.search(text)) and not _NOT_REMOTE_RE.search(text) and not _ONSITE_RE.search(text))
    if location and not remote and not _SEATTLE_RE.search(location):
        place = re.sub(r"\s*\([^)]*\)", "", location).strip() or location
        if in_semester:
            flag("far_office", "blocker", f"It needs you in {place} during the semester, far from your Seattle "
                 "classes.", [_quote(location)])
        elif employment == "internship" and season == "summer":
            flag("relocation", "note", f"Based in {place}; relocating for the summer is fine (you said yes to "
                 "relocation).")
        elif spring_free or employment == "co_op":
            flag("relocation", "note", f"Based in {place}; fine, you're free from {cfg.free_from} and open to "
                 "co-ops (you said yes to relocation).")

    # 6. Deadlines and urgency.
    for m in _DEADLINE_RE.finditer(text):
        when = _parse_deadline(m.group("date"))
        if when and when < today:
            flag("deadline_passed", "warning", f"The deadline was {m.group('date')}.", [_quote(m.group(0))])
        else:
            flag("deadline", "note", f"Deadline: {m.group('date')}.", [_quote(m.group(0))])
        break
    urgency = _matching(sentences, _URGENCY_RE)
    if urgency:
        flag("urgency", "note", "Apply soon: the posting signals rolling or limited review.", urgency[:1])

    # 7b. A limit the posting states itself ("submit up to 5 applications during any rolling 30-day period").
    stated = _STATED_LIMIT_RE.search(text)
    if stated:
        limit, days = int(stated.group("n")), int(stated.group("days"))
        since = (today - timedelta(days=days)).isoformat()
        recent = [a for a in applications if same_company(a.company, job.company) and a.date_applied
                  and a.date_applied[:10] >= since and a.status not in IN_PROGRESS + ("skipped", "failed")]
        level: FlagLevel = "warning" if len(recent) + 1 > limit else "note"
        flag("posting_limit", level, f"The posting allows {limit} applications per {days} days; you have {len(recent)} "
             f"at {job.company} since {since}." + (" Its site may refuse this one; building anyway." if level == "warning"
                                                    else ""), [_quote(stated.group(0))])

    # 7. Company caps (8.3) and earlier applications.
    coverage = None
    if keywords and keywords.tech:
        known = sum(1 for t in keywords.tech if t.status != "not_in_master")
        coverage = round(100 * known / len(keywords.tech), 1)
    company_rows = [a for a in applications if same_company(a.company, job.company)]
    flags.extend(_cap_flags(job.company, caps, company_rows, today))
    if company_rows:
        ranked = sorted(company_rows, key=lambda a: -(a.keyword_match_pct or -1))
        parts = [f"{a.role_title} ({a.status}{', ' + a.date_applied if a.date_applied else ''}"
                 f"{', ' + format(a.keyword_match_pct, 'g') + '% keyword match' if a.keyword_match_pct is not None else ''})"
                 for a in ranked]
        here = f"this posting (about {coverage:g}% of its technologies are in your master list)" if coverage is not None \
            else "this posting"
        flag("company_applications", "note", f"{len(company_rows)} earlier application(s) at {job.company}. Strongest "
             f"fits first: {'; '.join(parts)}. Compare with {here}.")

    return Eligibility(
        recommendation="skip_and_ask" if any(f.level == "blocker" for f in flags) else "build",
        flags=flags,
        grad_date=grad_date,
        grad_date_reason=grad_reason,
        employment=employment,
        season=season,
        season_year=season_year,
        location=location,
        keyword_coverage_pct=coverage,
        company_applications=company_rows,
        duplicates=duplicates,
    )


def _choose_grad_date(cfg: Config, sentences: list[str], employment: Employment, season: Optional[str],
                      season_year: Optional[int]) -> tuple[str, str, list[Flag]]:
    """Spec 8 item 4: May 2027 by default; December 2027 when the posting's window or a returning-to-school rule
    needs it; May 2027 when a full-time start date needs it."""
    default, alt = cfg.default_grad_date, cfg.alt_grad_date
    options = {d: parse_month_year(d) for d in (default, alt)}
    allowed = [d for d, my in options.items() if my is not None]
    reasons: list[str] = []
    flags: list[Flag] = []

    lower, upper, evidence = _grad_window(sentences)
    if lower or upper:
        fits = [d for d in allowed if (not lower or (options[d].year, options[d].month) >= lower)  # type: ignore[union-attr]
                and (not upper or (options[d].year, options[d].month) <= upper)]  # type: ignore[union-attr]
        window = f"{_fmt(lower) if lower else 'any time'} to {_fmt(upper) if upper else 'any time'}"
        if not fits:
            flags.append(Flag(id="grad_window", level="blocker", message=f"It wants graduation between {window}; yours "
                              f"is {default} or {alt}.", evidence=evidence))
            return default, f"no fit for the posting's window ({window})", flags
        allowed = fits
        reasons.append(f"the posting's graduation window ({window})")

    returning = _matching(sentences, _RETURNING_RE)
    if returning:
        end = _term_end(season, season_year)
        if end is None:
            keep = [d for d in allowed if d == alt] or allowed
            reasons.append("the posting requires returning to school; the internship term is unclear, so the later date")
        else:
            keep = [d for d in allowed if (options[d].year, options[d].month) > end]  # type: ignore[union-attr]
            reasons.append(f"the posting requires returning to school after {season.title()} {season_year}")  # type: ignore[union-attr]
        if not keep:
            flags.append(Flag(id="grad_window", level="blocker", message="It requires returning to school after the "
                              f"internship, and neither {default} nor {alt} leaves a term after it.", evidence=returning))
            return default, "no fit for the returning-to-school rule", flags
        allowed = keep

    if employment == "full_time" and default in allowed:
        reasons.append("full-time start")
    chosen = default if default in allowed else allowed[0]
    alt_year = options[alt].year if options.get(alt) else None
    if employment == "internship" and season == "summer" and alt in allowed and season_year in (None, alt_year) \
            and chosen != alt:
        # Vatsal, 2026-11-15: always December 2027 for Summer 2027 internships (back at school after the summer),
        # unless the posting's window rules it out
        chosen = alt
        reasons.append("a Summer 2027 internship: back at school after the summer")
    reason = "default" if not reasons else "; ".join(reasons)
    flags.append(Flag(id="grad_date", level="note", message=f"Graduation date: {chosen} ({reason})."))
    return chosen, reason, flags


def _cap_flags(company: str, caps: CapsFile, rows: list[CompanyApplication], today: date) -> list[Flag]:
    flags: list[Flag] = []
    for note in caps.notes:
        if contains_word(company, note.company):
            flags.append(Flag(id="cap_note", level="note", message=f"{note.company}: {note.note}."))
    cap = cap_for(company, caps)
    if cap is None:
        return flags
    start = window_start(today, cap).isoformat()
    in_window = [r for r in rows if r.date_applied and r.date_applied[:10] >= start]
    submitted = [r for r in in_window if r.status not in IN_PROGRESS + ("skipped", "failed")]
    pending = [r for r in in_window if r.status in IN_PROGRESS]
    window = _window_text(cap)
    kind = "allows" if cap.enforced else "recommends at most"
    if len(submitted) >= cap.max_applications:
        consequence = ("Their site may refuse this one; building and applying anyway." if cap.enforced
                       else "It's a recommendation, so building and applying anyway.")
        flags.append(Flag(id="cap_reached", level="warning", message=f"{cap.company} {kind} {cap.max_applications} "
                          f"application(s) per {window}, and you have {len(submitted)} since {start}. {consequence}"))
    elif len(submitted) + 1 >= cap.max_applications:
        flags.append(Flag(id="cap_near", level="warning", message=f"This would be application {len(submitted) + 1} of "
                          f"the {cap.max_applications} {cap.company} {kind.replace('at most', '').strip()} per "
                          f"{window}."))
    if cap.note:
        flags.append(Flag(id="cap_note", level="note", message=f"{cap.company}: {' '.join(cap.note.split())}"))
    if pending:
        flags.append(Flag(id="cap_pending", level="note", message=f"{len(pending)} more {cap.company} application(s) "
                          f"in progress, not counted yet."))
    return flags


def format_eligibility(e: Eligibility) -> str:
    head = "build" if e.recommendation == "build" else "recommend skipping; ask Vatsal"
    lines = [f"Eligibility (spec 8): {head}"]
    order = {"blocker": 0, "warning": 1, "note": 2}
    for f in sorted(e.flags, key=lambda f: order[f.level]):
        lines.append(f"  [{f.level}] {f.message}")
        for quote in f.evidence[:2]:
            lines.append(f'      "{quote}"')
    return "\n".join(lines)

