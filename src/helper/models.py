"""Pydantic models for config, profile, job, run state, and Claude's tailor output.

These models check types and shapes only. Rules that need readable messages (bullet counts,
allowed numbers, wording rules, and so on) live in the tailor validator.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
MONTH_YEAR_RE = re.compile(r"^(%s) (\d{4})$" % "|".join(MONTHS))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def find_gpa_keys(data: Any, path: str = "") -> list[str]:
    """Dotted paths of every key that looks like a GPA field. Rule 3: no GPA anywhere."""
    hits: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            here = f"{path}.{key}" if path else str(key)
            if re.search(r"gpa", str(key), re.IGNORECASE):
                hits.append(here)
            hits.extend(find_gpa_keys(value, here))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            hits.extend(find_gpa_keys(value, f"{path}[{i}]"))
    return hits


# --------------------------------------------------------------------------- config.yaml


class Config(StrictModel):
    projects_per_resume: int = Field(5, ge=1, le=8)
    bullets_per_item: int = Field(2, ge=1, le=4)
    submit_mode: Literal["review", "auto"] = "review"
    # before_fill: Claude's drafted answers wait for Vatsal's OK in the session (spec 10.4). in_form: they go straight
    # into the form and Vatsal reviews them in the window before Submit (Vatsal, 2026-11-15).
    answer_review: Literal["before_fill", "in_form"] = "before_fill"
    # True: Claude's drafted answers count as unreviewed, so auto-submit waits for Vatsal (spec 10.5). False: they count
    # as approved, and auto-submit may send them (Vatsal, 2026-11-15).
    drafts_need_review: bool = True
    # sites with their own clean-run minimum for auto-submit (Vatsal, 2026-11-16: Workday starts now, 0)
    auto_submit_min_clean_runs_by_site: dict[str, int] = Field(default_factory=dict)
    auto_submit_min_clean_runs: int = Field(5, ge=1)
    default_grad_date: str = "May 2027"
    alt_grad_date: str = "December 2027"
    browser_profile_dir: str = "~/.helper/browser-profile"
    headless: bool = False
    # added during the build
    master_list: Optional[str] = None
    db_path: str = "helper.db"
    latex_engine: Literal["pdflatex", "xelatex"] = "pdflatex"
    immediate_start_target: str = "December 2026"
    # free of classes from this month: spring work and co-ops anywhere are fine (Vatsal, 2026-11-16)
    free_from: str = "January 2027"
    match_auto_threshold: float = Field(0.85, gt=0, le=1)
    match_ask_threshold: float = Field(0.60, gt=0, le=1)
    fill_wait_timeout_minutes: int = Field(30, ge=1)
    fill_certifications: bool = False  # Workday's Certifications section (Vatsal, 2026-11-15: off for now)
    max_browser_windows: int = Field(0, ge=0)  # 0 = no limit (Vatsal, 2026-11-16: the job hunt opens as many as it needs)
    stuck_window_hours: float = Field(24, gt=0)  # `helper fill --keep-open`: how long a window that stopped stays open
    pause_ms: tuple[int, int] = (150, 600)

    @field_validator("default_grad_date", "alt_grad_date", "immediate_start_target", "free_from")
    @classmethod
    def _month_year(cls, v: str) -> str:
        if not MONTH_YEAR_RE.match(v):
            raise ValueError(f"must look like 'May 2027' (full month name and year), got {v!r}")
        return v

    @model_validator(mode="after")
    def _ranges(self) -> "Config":
        if self.match_ask_threshold >= self.match_auto_threshold:
            raise ValueError("match_ask_threshold must be lower than match_auto_threshold")
        lo, hi = self.pause_ms
        if lo < 0 or hi < lo:
            raise ValueError("pause_ms must be [min, max] with 0 <= min <= max")
        return self


# --------------------------------------------------------------------------- profile.yaml


class Header(StrictModel):
    """Printed at the top of every resume. Values come from the reference resume."""

    name: str
    location: str
    phone: str
    email: str
    linkedin_url: str
    linkedin_label: str
    github_url: str
    github_label: str
    portfolio_url: str


class Address(StrictModel):
    street: Optional[str] = None
    unit: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None  # Workday's "County" box (Great American, 2026-11-18)
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


class EducationEntry(StrictModel):
    school: str
    college: Optional[str] = None
    degree: str
    major: str
    location: str
    start: str
    end: str  # "per_run" = graduation date chosen for each application


class Tier1(StrictModel):
    """Auto-filled without asking. None means the field pauses the run."""

    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    preferred_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Address = Field(default_factory=Address)
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    current_school: Optional[str] = None
    degree: Optional[str] = None
    major: Optional[str] = None
    expected_graduation: Optional[str] = None  # "per_run" = the grad date chosen for each application
    how_did_you_hear: Optional[str] = None
    nuid: Optional[str] = None  # Northeastern ID: only on Northeastern campus job letters
    citizenship: Optional[str] = None  # country of citizenship
    citizenship_since: Optional[str] = None  # the year it was granted (by birth: the birth year)
    willing_to_relocate: Optional[str] = None
    work_location_preferences: Optional[str] = None


class Tier2Item(StrictModel):
    """Auto-filled only when auto_answer is true AND a value is set."""

    auto_answer: bool = False
    value: Optional[str] = None
    note: Optional[str] = None


class Sponsorship(Tier2Item):
    # Answer "Yes" instead of `value` when the question explicitly says sponsorship includes CPT/OPT.
    # If CPT/OPT is mentioned but it is unclear whether it counts, the run pauses.
    yes_when_question_includes_cpt_opt: bool = True


class StartDates(StrictModel):
    auto_answer: bool = False
    summer_internship: Optional[str] = None  # "May 1": year comes from the posting's season
    spring_internship: Optional[str] = None
    fall_internship: Optional[str] = None
    co_op: Optional[str] = None
    full_time: Optional[str] = None


class EEO(StrictModel):
    auto_answer: bool = False
    gender: Optional[str] = None
    race_ethnicity: Optional[str] = None
    veteran_status: Optional[str] = None
    disability_status: Optional[str] = None
    hispanic_latino: Optional[str] = None  # unset: default_for_other
    default_for_other: str = "Decline to self identify"


class Tier2(StrictModel):
    work_authorization: Tier2Item = Field(default_factory=Tier2Item)
    sponsorship: Sponsorship = Field(default_factory=Sponsorship)
    us_citizen_or_permanent_resident: Tier2Item = Field(default_factory=Tier2Item)
    f1_student: Tier2Item = Field(default_factory=Tier2Item)  # "Are you an F1 student ... CPT, OPT?" (2026-11-17)
    earliest_start_date: StartDates = Field(default_factory=StartDates)
    salary_expectation: Tier2Item = Field(default_factory=Tier2Item)
    salary_from_posting: bool = False  # answer with the posting's own pay (midpoint, rounded) when it states one
    ai_screening_consent: Tier2Item = Field(default_factory=Tier2Item)  # e.g. HiredScore: let AI review the application
    keep_for_other_roles: Tier2Item = Field(default_factory=Tier2Item)  # keep the application for other openings
    relatives_at_company: Tier2Item = Field(default_factory=Tier2Item)  # relatives who work or worked there
    employment_restrictions: Tier2Item = Field(default_factory=Tier2Item)  # a non-compete or similar restriction
    eeo: EEO = Field(default_factory=EEO)
    background_check_consent: Tier2Item = Field(default_factory=Tier2Item)
    age_18_or_older: Tier2Item = Field(default_factory=Tier2Item)
    security_clearance: Tier2Item = Field(default_factory=Tier2Item)


class Documents(StrictModel):
    """Files helper uploads when a form requires them. Paths are from the repo folder (git ignores top-level PDFs)."""

    transcript: Optional[str] = None  # the current program's transcript
    undergrad_transcript: Optional[str] = None


class Profile(StrictModel):
    header: Header
    tier1: Tier1 = Field(default_factory=Tier1)
    education: list[EducationEntry] = Field(default_factory=list)
    tier2: Tier2 = Field(default_factory=Tier2)
    documents: Documents = Field(default_factory=Documents)

    @model_validator(mode="before")
    @classmethod
    def _no_gpa(cls, data: Any) -> Any:
        hits = find_gpa_keys(data)
        if hits:
            raise ValueError(f"GPA fields are not allowed (rule 3): {', '.join(hits)}")
        return data

    def missing_values(self) -> list[str]:
        """Dotted paths of values that will pause a run because they are not set."""
        missing: list[str] = []
        for name, value in self.tier1:
            if name == "address":
                missing += [f"tier1.address.{k}" for k, v in value if v is None and k != "unit"]
            elif value is None:
                missing.append(f"tier1.{name}")
        for name, item in self.tier2:
            if isinstance(item, Tier2Item) and item.auto_answer and item.value is None:
                missing.append(f"tier2.{name}.value")
        start = self.tier2.earliest_start_date
        if start.auto_answer:
            missing += [f"tier2.earliest_start_date.{k}" for k, v in start if k != "auto_answer" and v is None]
        eeo = self.tier2.eeo
        if eeo.auto_answer:
            missing += [
                f"tier2.eeo.{k}" for k, v in eeo if k not in ("auto_answer", "default_for_other") and v is None
            ]
        return missing


# --------------------------------------------------------------------------- job.json


class FormQuestion(StrictModel):
    label: str
    field_name: Optional[str] = None
    type: str = "text"  # text | textarea | select | multi_select | radio | checkbox | file | date | ...
    required: bool = False
    options: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    max_length: Optional[int] = None
    section: str = "questions"  # questions | location | eeoc | demographic


class Job(StrictModel):
    url: Optional[str] = None
    ats: str = "unknown"  # greenhouse | lever | ashby | workday | ... | jd_file
    company: str
    role_title: str
    location: Optional[str] = None
    job_id: Optional[str] = None
    description_text: str
    description_html: Optional[str] = None
    questions: list[FormQuestion] = Field(default_factory=list)
    source: str  # greenhouse_api | lever_api | ashby_api | browser | jd_file
    fetched_at: str
    apply_url: Optional[str] = None  # where the application form is (can differ from the posting link)


# --------------------------------------------------------------------------- runs/<id>/state.json

StepStatus = Literal["pending", "running", "waiting_for_claude", "waiting_for_user", "done", "failed", "skipped"]


class StepState(StrictModel):
    status: StepStatus = "pending"
    updated_at: Optional[str] = None
    message: Optional[str] = None


class RunState(StrictModel):
    run_id: str
    created_at: str
    command: Literal["apply", "resume"]
    url: Optional[str] = None
    options: dict[str, Any] = Field(default_factory=dict)
    steps: dict[str, StepState] = Field(default_factory=dict)
    application_id: Optional[int] = None


# --------------------------------------------------------------------------- tailor_output.json (spec 7.6)


class TailorExperience(StrictModel):
    entry_id: str
    title: str
    bullets: list[str]


class TailorProject(StrictModel):
    project_id: str
    subtitle: str
    tech_tags: list[str]
    bullets: list[str]
    reason: Optional[str] = None  # one line for the pre-build message (spec 9, step 7)


class SkillsLine(StrictModel):
    label: str
    items: list[str]


class CoverLetterOutput(StrictModel):
    """What Claude writes for a cover letter (cover_letter_output.json)."""

    hook: Optional[str] = None  # the posting phrase paragraph 1 quotes
    stories: list[str]  # the resume items the letter goes deep on
    paragraphs: list[str]
    flags: list[str] = Field(default_factory=list)


class KeywordAudit(StrictModel):
    matched: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    skills_only: list[str] = Field(default_factory=list)


class TailorOutput(StrictModel):
    role_type: str
    grad_date: str
    experience: list[TailorExperience]
    projects: list[TailorProject]
    skills_lines: list[SkillsLine]
    coursework: list[str]
    keyword_audit: KeywordAudit
    flags: list[str] = Field(default_factory=list)
    fit_paragraph: Optional[str] = None  # pre-build message (spec 9, step 7)

    @model_validator(mode="before")
    @classmethod
    def _no_gpa(cls, data: Any) -> Any:
        hits = find_gpa_keys(data)
        if hits:
            raise ValueError(f"GPA fields are not allowed (rule 3): {', '.join(hits)}")
        return data


# --------------------------------------------------------------------------- data/master.yaml (generated)


class DateInfo(StrictModel):
    start: Optional[str] = None  # "2025-12"
    end: Optional[str] = None  # "2026-01", "present", or "ongoing"
    display: str  # resume format, e.g. "Dec. 2025 -- Jan. 2026"


class RequireRule(StrictModel):
    when: str  # regex: if a bullet matches this...
    must: str  # ...it must also match this


class Conflict(StrictModel):
    a: str  # regex
    b: str  # regex; text that matches both breaks the rule


class WordingRule(StrictModel):
    id: str
    source: str  # where the rule comes from: verbatim master-list text, "spec 5.1", or the rule's name
    description: str
    # bullet: checked against each bullet. entry: checked against the whole entry (tech line and all bullets).
    scope: Literal["bullet", "entry"] = "bullet"
    forbid: list[str] = Field(default_factory=list)  # regexes the checked text must not match
    require: list[RequireRule] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    title_prefix: Optional[str] = None


class MasterItem(StrictModel):
    """One project or experience entry. Text fields are verbatim from the master list."""

    id: str
    kind: Literal["project", "experience"]
    number: Optional[int] = None
    name: str
    heading: str
    subtitle: Optional[str] = None
    category: Optional[str] = None
    status: Optional[str] = None
    status_kind: Optional[Literal["complete", "merged", "in_progress", "ongoing", "other"]] = None
    timeline: Optional[str] = None
    timeline_note: Optional[str] = None
    dates: Optional[DateInfo] = None
    github_url: Optional[str] = None
    organization: Optional[str] = None
    location: Optional[str] = None
    technologies: list[str] = Field(default_factory=list)
    fields: dict[str, str] = Field(default_factory=dict)
    sections: dict[str, list[str]] = Field(default_factory=dict)
    metrics: dict[str, list[str]] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    variants: list[str] = Field(default_factory=list)
    wording_notes: list[str] = Field(default_factory=list)
    wording_rules: list[WordingRule] = Field(default_factory=list)
    implied_tech: list[str] = Field(default_factory=list)  # e.g. PostgreSQL for a Supabase project (lexicon `implies`)
    framing_additions: list[str] = Field(default_factory=list)
    allowed_tech: list[str] = Field(default_factory=list)
    allowed_numbers: list[str] = Field(default_factory=list)
    allowed_tokens: list[str] = Field(default_factory=list)

    def bullets(self) -> list[str]:
        return [b for section in self.sections.values() for b in section]


class SkillItem(StrictModel):
    name: str
    note: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)


class SkillGroup(StrictModel):
    name: str
    items: list[SkillItem] = Field(default_factory=list)
    subgroups: list["SkillGroup"] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def all_items(self) -> list[SkillItem]:
        return self.items + [i for g in self.subgroups for i in g.all_items()]


class FramingRule(StrictModel):
    built_with: str
    also: str
    examples: str
    applies_to: list[str]


class DatabaseFraming(StrictModel):
    text: str
    rules: list[FramingRule]


class SourceInfo(StrictModel):
    file: str
    sha256: str
    version: Optional[str] = None
    last_updated: Optional[str] = None
    declared_projects: Optional[int] = None
    declared_experience: Optional[int] = None


class MasterData(StrictModel):
    source: SourceInfo
    projects: list[MasterItem]
    experience: list[MasterItem]
    skills: list[SkillGroup]
    languages: list[str]
    database_framing: Optional[DatabaseFraming] = None
    other_sections: dict[str, str] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)

    def items(self) -> list[MasterItem]:
        return self.projects + self.experience

    def item(self, item_id: str) -> MasterItem:
        for item in self.items():
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def skill_names(self) -> list[str]:
        return [i.name for g in self.skills for i in g.all_items()]


# --------------------------------------------------------------------------- hand-edited data files


def _unique_nonempty(values: list[str], what: str) -> list[str]:
    cleaned = [v.strip() for v in values]
    if any(not v for v in cleaned):
        raise ValueError(f"{what} must not contain empty entries")
    dupes = sorted({v for v in cleaned if cleaned.count(v) > 1})
    if dupes:
        raise ValueError(f"duplicate {what}: {', '.join(dupes)}")
    return cleaned


class CoursesFile(StrictModel):
    """data/courses.yaml: courses actually taken (spec 5.3)."""

    courses: list[str]

    @field_validator("courses")
    @classmethod
    def _check(cls, v: list[str]) -> list[str]:
        v = _unique_nonempty(v, "courses")
        if len(v) < 5:
            raise ValueError("Relevant Coursework needs at least 5 courses")
        return v


class Cap(StrictModel):
    company: str
    match: list[str] = Field(default_factory=list)  # other name forms, matched case-insensitively
    max_applications: int = Field(ge=1)
    window_days: Optional[int] = Field(None, ge=1)
    window_months: Optional[int] = Field(None, ge=1)
    # True: the company's own site refuses applications past the limit. False: a recommendation.
    # Either way helper only flags it, then builds and applies (Vatsal, 2026-11-14).
    enforced: bool = False
    note: Optional[str] = None

    @model_validator(mode="after")
    def _one_window(self) -> "Cap":
        if (self.window_days is None) == (self.window_months is None):
            raise ValueError(f"{self.company}: set exactly one of window_days or window_months")
        return self


class CapNote(StrictModel):
    company: str
    note: str


class CapsFile(StrictModel):
    """data/caps.yaml: application caps per company (spec 8.3)."""

    caps: list[Cap]
    notes: list[CapNote] = Field(default_factory=list)


class RoleTypeSet(StrictModel):
    label: str
    projects: list[str]
    plus_one_of: list[str] = Field(default_factory=list)
    kalini: Optional[Literal["sde", "ml"]] = None  # null = choose per posting


class ProjectSetsFile(StrictModel):
    """data/project_sets.yaml: default project sets by role type (spec 7.2)."""

    role_types: dict[str, RoleTypeSet]


class ExperienceMeta(StrictModel):
    location: Optional[str] = None
    resume_title: Optional[str] = None


class ResumeMetaFile(StrictModel):
    """data/resume_meta.yaml: presentation details the master list does not contain."""

    experience: dict[str, ExperienceMeta]
    project_display_names: dict[str, str]


class ApprovedAnswer(StrictModel):
    id: str
    question_patterns: list[str]
    answer: str
    max_chars: Optional[int] = None
    approved_on: Optional[str] = None


class AnswersFile(StrictModel):
    """data/answers.yaml: free-text answers you approved (spec 5.4)."""

    answers: list[ApprovedAnswer] = Field(default_factory=list)


# --------------------------------------------------------------------------- proposals Vatsal approves (milestone 4)


class SkillSynonym(StrictModel):
    term: str  # how it may appear in a skills line, e.g. "Hypothesis Testing"
    evidence: list[str]  # the master-list facts behind it, as "<item id>: <fact>"
    approved: bool = False
    note: Optional[str] = None


class SkillSynonymsFile(StrictModel):
    """data/skill_synonyms.yaml: skills-line terms the master list supports in other words. Only approved ones count."""

    synonyms: list[SkillSynonym] = Field(default_factory=list)

    @field_validator("synonyms")
    @classmethod
    def _unique(cls, v: list[SkillSynonym]) -> list[SkillSynonym]:
        _unique_nonempty([s.term.lower() for s in v], "synonym terms")
        return v

    def approved_terms(self) -> list[str]:
        return [s.term for s in self.synonyms if s.approved]


class TechFamily(StrictModel):
    id: str
    members: list[str]  # canonical technology names; each must appear somewhere in the master list
    approved: bool = False
    note: Optional[str] = None

    @field_validator("members")
    @classmethod
    def _members(cls, v: list[str]) -> list[str]:
        v = _unique_nonempty(v, "family members")
        if len(v) < 2:
            raise ValueError("a family needs at least 2 members")
        return v


class TechFamiliesFile(StrictModel):
    """data/tech_families.yaml: interchangeable technologies. With a family approved, an entry may name one member
    in place of another member it actually used (a swap replaces, never adds)."""

    families: list[TechFamily] = Field(default_factory=list)

    @field_validator("families")
    @classmethod
    def _unique(cls, v: list[TechFamily]) -> list[TechFamily]:
        _unique_nonempty([f.id for f in v], "family ids")
        return v

    def approved(self) -> list[TechFamily]:
        return [f for f in self.families if f.approved]
