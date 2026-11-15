"""Answers for an application form (spec 10.3 and 10.4).

Each question is matched to a profile value with a confidence:
- at or above match_auto_threshold the value is filled automatically;
- between match_ask_threshold and match_auto_threshold Vatsal confirms it first;
- a free-text question gets an approved answer from data/answers.yaml, or a draft from Claude that Vatsal approves;
- any other required question pauses the run (rule 9). Optional questions nobody can answer stay blank.
A GPA question is never filled (rule 3): required ones pause the run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import Field

from .companies import contains_word, same_company
from .dates import parse_month_year
from .models import AnswersFile, Config, FormQuestion, Job, Profile, StrictModel

Kind = Literal["auto", "confirm", "free_text", "unknown", "file", "skip"]


class PlannedAnswer(StrictModel):
    field: str
    label: str
    type: str
    required: bool
    options: list[str] = Field(default_factory=list)
    section: str = "questions"
    key: Optional[str] = None  # the profile value it maps to
    kind: Kind
    value: Optional[str] = None  # text to type, or the option to choose
    confidence: float = 0.0
    source: str = ""
    note: Optional[str] = None  # why it needs a person
    max_length: Optional[int] = None
    description: Optional[str] = None
    status: Literal["ready", "needs_review", "approved"] = "ready"


class AnswerPlan(StrictModel):
    answers: list[PlannedAnswer]

    def open_items(self) -> list[PlannedAnswer]:
        """Answers Claude or Vatsal still have to provide or confirm."""
        return [a for a in self.answers if a.kind in ("free_text", "confirm") or (a.kind == "unknown" and a.required)]

    def needs_review(self) -> list[PlannedAnswer]:
        return [a for a in self.answers if a.status == "needs_review"]

    def get(self, field_name: str) -> Optional[PlannedAnswer]:
        return next((a for a in self.answers if a.field == field_name), None)


# --------------------------------------------------------------------------- option matching

_STOP = {"a", "an", "the", "i", "am", "to", "of", "or", "and", "my", "as", "be", "in", "for", "with", "is", "are", "you",
         "your", "this", "that", "have", "has", "had", "do", "does"}
DECLINE = ["decline", "prefer not", "don't wish", "do not wish", "not wish", "not to answer", "do not want to answer",
           "don't want to answer", "rather not", "choose not", "not to disclose", "not specified"]


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", text.lower().replace("’", "'").replace("'", "")).split())


def _tokens(text: str) -> set[str]:
    return set(_norm(text).split()) - _STOP


def _negative(text: str) -> bool:
    return bool(re.match(r"\s*(?:no\b|not\b|i am not\b|i'm not\b|i do not\b|i don't\b)", text, re.I))


def match_option(value: str, options: list[str], aliases: tuple[str, ...] | list[str] = ()) -> tuple[Optional[str], float]:
    """The option that says the same as `value`, with a score from 0 to 1."""
    if not value or not options:
        return None, 0.0
    v = _norm(value)
    best, best_score = None, 0.0
    for option in options:
        n = _norm(option)
        if not n:
            continue
        if n == v:
            return option, 1.0
        if v and (n.startswith(v + " ") or v.startswith(n + " ")):
            score = 0.95
        elif aliases and any(_norm(a) and (_norm(a) == n or re.search(rf"\b{re.escape(_norm(a))}\b", n)) for a in aliases):
            score = 0.9
        else:
            tv, to = _tokens(value), _tokens(option)
            score = len(tv & to) / len(tv | to) if tv and to else 0.0
            if tv and tv <= to:
                score = max(score, 0.85)
            if _negative(value) != _negative(option):
                score *= 0.5
        if score > best_score:
            best, best_score = option, score
    return best, round(best_score, 3)


# --------------------------------------------------------------------------- what a question asks

# (key, pattern, confidence that the label means this). First match wins, so order matters.
LABEL_RULES: list[tuple[str, str, float]] = [
    ("gpa", r"\bgpa\b|grade point", 1.0),
    ("cover_letter", r"\bcover letter\b", 0.95),
    ("resume", r"\bresume\b|\bcv\b", 0.95),
    ("preferred_first_name", r"\bpreferred (first )?name\b|\bnickname\b", 0.95),
    ("first_name", r"\bfirst name\b|\bgiven name\b", 0.95),
    ("last_name", r"\blast name\b|\bsurname\b|\bfamily name\b", 0.95),
    ("full_name", r"\bfull (legal )?name\b|^\s*(legal )?name\s*\??\s*$", 0.9),
    ("email", r"\be-?mail\b", 0.95),
    ("phone", r"\bphone\b|\bmobile\b|\bcell\b", 0.9),
    ("linkedin_url", r"\blinked ?in\b", 0.95),
    ("github_url", r"\bgit ?hub\b", 0.95),
    ("portfolio_url", r"\bportfolio\b|\bpersonal (web)?site\b|\bwebsite\b|\bweb ?site\b|\bblog\b", 0.88),
    ("work_authorization", r"\b(legally )?(authori[sz]ed|eligible|permitted) to work\b|\bwork authori[sz]ation\b"
                           r"|\bright to work\b", 0.95),
    ("sponsorship", r"\bsponsor", 0.95),
    ("us_citizen", r"\bcitizen\b|\bpermanent resident\b|\bgreen card\b", 0.9),
    ("graduation_date", r"\bgraduat\w*\b.*\b(date|when|year|term|semester)\b|\bexpected graduation\b|\bgraduation\b"
                        r"|\bclass of\b|\bwhen (do|will) you graduate\b", 0.9),
    ("previous_employee", r"\b(ever|previously|before|formerly)\b.*\b(work(ed)?|employ\w*|contractor|consultant)\b", 0.9),
    ("major", r"\bmajor\b|\bdiscipline\b|\bfield of study\b|\barea of study\b", 0.9),
    ("degree", r"^\s*degree( type| level)?\s*\??\s*$|\bhighest degree\b|\b(type|level) of degree\b", 0.95),
    ("degree", r"\bdegree\b", 0.85),
    ("school", r"\bschool\b|\buniversity\b|\bcollege\b|\binstitution\b", 0.85),
    ("relocate", r"\breloca", 0.9),
    ("start_date", r"\bstart date\b|\bwhen can you (start|begin)\b|\bavailab\w* to (start|begin)\b"
                   r"|\bearliest\b.*\b(start|begin)\b", 0.9),
    ("salary", r"\bsalary\b|\bcompensation\b|\bpay (expectation|range)s?\b|\bdesired pay\b", 0.9),
    ("how_did_you_hear", r"\bhow did you (hear|learn|find|connect|come across)\b|\bwhere did you (hear|find|learn)\b", 0.95),
    ("how_did_you_hear", r"\bsource\b", 0.85),
    ("referral", r"\b(referred|referral)\b", 0.9),
    ("age_18", r"\b18\b|\beighteen\b|\blegal age\b", 0.9),
    ("background_check", r"\bbackground (check|screen)", 0.9),
    ("clearance", r"\bclearance\b", 0.9),
    ("eeo_hispanic", r"\bhispanic\b|\blatin[oax]\b", 0.9),
    ("eeo_race", r"\brace\b|\bethnicit", 0.85),
    ("eeo_gender", r"\bgender\b|^\s*sex\s*\??\s*$", 0.9),
    ("eeo_veteran", r"\bveteran\b", 0.9),
    ("eeo_disability", r"\bdisabilit", 0.9),
    ("eeo_other", r"\bpronoun|\bsexual orientation\b|\blgbt|\btransgender\b", 0.95),
    ("work_location", r"\bwhich (\w+ )?(office|hub|location|city)\b|\b(prefer|preference)\w*\b.*\b(location|office|hub|site"
                      r"|city)\b|\bwork (location|arrangement)\b", 0.95),
    ("address_zip", r"\bzip\b|\bpostal\b", 0.9),
    ("address_street", r"\bstreet\b|\baddress line\b|\bmailing address\b|\bhome address\b|^\s*address\s*$", 0.85),
    ("address_country", r"^\s*country\b", 0.85),
    ("address_state", r"^\s*(state|province)\b", 0.85),
    ("address_city", r"^\s*city\b", 0.85),
    ("location_city", r"\blocation\b|\bwhere (do you|are you) (live|located|based)\b|\bcurrent city\b", 0.85),
]
FIELD_KEYS = {
    "first_name": "first_name", "last_name": "last_name", "email": "email", "phone": "phone", "resume": "resume",
    "cover_letter": "cover_letter", "location": "location_city", "gender": "eeo_gender", "race": "eeo_race",
    "hispanic_ethnicity": "eeo_hispanic", "veteran_status": "eeo_veteran", "disability_status": "eeo_disability",
}
SKIP_FIELDS = {"resume_text", "cover_letter_text"}  # typed-in alternatives to an upload
_US_STATES = {"WA": "Washington", "CA": "California", "NY": "New York", "OR": "Oregon", "TX": "Texas", "MA": "Massachusetts",
              "IL": "Illinois", "NJ": "New Jersey", "CO": "Colorado", "GA": "Georgia", "FL": "Florida", "VA": "Virginia"}


def key_for(q: FormQuestion) -> tuple[Optional[str], float]:
    if q.field_name in FIELD_KEYS:
        return FIELD_KEYS[q.field_name], 1.0
    text = q.label
    for key, pattern, confidence in LABEL_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            if key.startswith("eeo_") and q.section in ("eeoc", "demographic"):
                confidence = max(confidence, 0.95)  # a question in the EEO survey means what it says
            return key, confidence
    return None, 0.0


# --------------------------------------------------------------------------- values


@dataclass
class AnswerContext:
    profile: Profile
    cfg: Config
    job: Job
    grad_date: str
    employment: str = "unknown"
    season: Optional[str] = None
    season_year: Optional[int] = None
    approved: AnswersFile = field(default_factory=AnswersFile)
    past_employers: list[str] = field(default_factory=list)


@dataclass
class Value:
    text: Optional[str]
    source: str
    aliases: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()  # alternative forms to try against options, in order
    note: Optional[str] = None
    confidence: float = 1.0
    fallback_other: bool = False  # no good option: suggest "Other" for Vatsal to confirm


def _grad_candidates(grad: str) -> tuple[str, ...]:
    my = parse_month_year(grad)
    if not my:
        return (grad,)
    season = "Spring" if my.month <= 6 else ("Summer" if my.month <= 8 else "Fall")
    import calendar

    return (grad, f"{calendar.month_abbr[my.month]} {my.year}", f"{my.month:02d}/{my.year}", f"{my.month}/{my.year}",
            f"{my.year}-{my.month:02d}", f"{season} {my.year}", *(("Winter " + str(my.year),) if my.month == 12 else ()))


def _start_date(ctx: AnswerContext) -> Value:
    sd = ctx.profile.tier2.earliest_start_date
    pick = {("internship", "summer"): sd.summer_internship, ("internship", "spring"): sd.spring_internship,
            ("internship", "fall"): sd.fall_internship}.get((ctx.employment, ctx.season or ""))
    if ctx.employment == "co_op":
        pick = sd.co_op
    elif ctx.employment == "full_time":
        pick = sd.full_time
    if not pick or pick.lower() == "ask":
        return Value(None, "profile earliest_start_date", note="start date for this kind of role is 'ask'")
    if ctx.employment == "internship" and ctx.season_year and not re.search(r"\d{4}", pick):
        pick = f"{pick}, {ctx.season_year}"
    return Value(pick, "profile earliest_start_date", candidates=(pick, *_grad_candidates(pick.replace(",", "")
                                                                                         .replace(" 1 ", " "))))


def value_for(key: str, q: FormQuestion, ctx: AnswerContext) -> Value:
    t1, t2 = ctx.profile.tier1, ctx.profile.tier2
    text = f"{q.label} {q.description or ''}"

    def tier2(item: object, name: str, **kw: object) -> Value:
        auto = getattr(item, "auto_answer", True)
        value = getattr(item, "value", None)
        return Value(value, f"profile tier2.{name}", confidence=1.0 if auto else 0.7, **kw)  # type: ignore[arg-type]

    if key == "first_name":
        return Value(t1.first_name, "profile first_name")
    if key == "last_name":
        return Value(t1.last_name, "profile last_name")
    if key == "preferred_first_name":
        name = t1.preferred_name or t1.first_name
        return Value(name.split()[0] if name and "first" in q.label.lower() else name, "profile preferred_name")
    if key == "full_name":
        return Value(t1.full_name, "profile full_name")
    if key == "email":
        return Value(t1.email, "profile email")
    if key == "phone":
        return Value(t1.phone, "profile phone")
    if key == "linkedin_url":
        return Value(t1.linkedin_url, "profile linkedin_url")
    if key == "github_url":
        return Value(t1.github_url, "profile github_url")
    if key == "portfolio_url":
        return Value(t1.portfolio_url, "profile portfolio_url")
    if key == "school":
        return Value(t1.current_school, "profile current_school")
    if key == "degree":
        return Value(t1.degree, "profile degree", aliases=("master", "masters", "ms", "m s", "graduate"))
    if key == "major":
        return Value(t1.major, "profile major")
    if key == "graduation_date":
        return Value(ctx.grad_date, "this run's graduation date", candidates=_grad_candidates(ctx.grad_date))
    if key == "work_authorization":
        return tier2(t2.work_authorization, "work_authorization")
    if key == "sponsorship":
        mentions = re.search(r"\b(CPT|OPT|F-?1)\b", f"{text} {' '.join(q.options)}")
        includes = re.search(r"(includ\w*|such as|e\.g\.|counts?|consider\w*)[^.?]{0,60}\b(CPT|OPT)\b"
                             r"|\b(CPT|OPT)\b[^.?]{0,40}\b(count|includ|consider)", text, re.I)
        if mentions and includes and t2.sponsorship.yes_when_question_includes_cpt_opt:
            return Value("Yes", "rule: sponsorship that includes CPT/OPT")
        if mentions:
            return Value(None, "rule: sponsorship and CPT/OPT",
                         note="names CPT/OPT/F-1 (in the question or its choices) without saying they count")
        return tier2(t2.sponsorship, "sponsorship")
    if key == "us_citizen":
        return tier2(t2.us_citizen_or_permanent_resident, "us_citizen_or_permanent_resident")
    open_to_all = "all" in (t1.work_location_preferences or "").lower()
    if key in ("relocate", "work_location") and q.type == "multi_select" and q.options and open_to_all:
        return Value("; ".join(q.options), "profile work_location_preferences (open to all)", confidence=0.95)
    if key == "relocate":
        return Value(t1.willing_to_relocate, "profile willing_to_relocate")
    if key == "work_location":
        return Value(t1.work_location_preferences, "profile work_location_preferences",
                     aliases=("either", "any", "no preference", "open to", "flexible", "all locations", "both"))
    if key == "start_date":
        return _start_date(ctx)
    if key == "salary":
        return tier2(t2.salary_expectation, "salary_expectation")
    if key == "how_did_you_hear":
        return Value(t1.how_did_you_hear, "profile how_did_you_hear", fallback_other=True,
                     aliases=("career site", "careers site", "company website", "careers page", "career page",
                              "company career", "careers website", "website"))
    if key == "age_18":
        return tier2(t2.age_18_or_older, "age_18_or_older")
    if key == "background_check":
        return tier2(t2.background_check_consent, "background_check_consent")
    if key == "clearance":
        return tier2(t2.security_clearance, "security_clearance", aliases=("none", "no clearance"))
    eeo = t2.eeo
    eeo_conf = 1.0 if eeo.auto_answer else 0.7
    if key == "eeo_gender":
        return Value(eeo.gender, "profile eeo.gender", confidence=eeo_conf,
                     aliases=("man",) if (eeo.gender or "").lower() == "male" else ())
    if key == "eeo_race":
        first = (eeo.race_ethnicity or "").split("(")[0].strip()
        return Value(eeo.race_ethnicity, "profile eeo.race_ethnicity", aliases=(first,) if first else (),
                     confidence=eeo_conf)
    if key == "eeo_veteran":
        return Value(eeo.veteran_status, "profile eeo.veteran_status", confidence=eeo_conf,
                     aliases=("not a protected veteran", "not a veteran", "i am not"))
    if key == "eeo_disability":
        return Value(eeo.disability_status, "profile eeo.disability_status", confidence=eeo_conf)
    if key in ("eeo_hispanic", "eeo_other"):
        return Value(eeo.default_for_other, "profile eeo.default_for_other", aliases=tuple(DECLINE), confidence=eeo_conf)
    if key == "previous_employee":
        mentioned = contains_word(q.label, ctx.job.company) or re.search(r"\b(us|this company|our company)\b", q.label, re.I)
        if any(same_company(e, ctx.job.company) for e in ctx.past_employers):
            return Value(None, "master list", note=f"you worked at {ctx.job.company}: answer it yourself")
        return Value("No", "master list: never worked here", confidence=1.0 if mentioned else 0.8)
    if key == "location_city":
        state = _US_STATES.get((t1.address.state or "").upper(), t1.address.state or "")
        parts = [p for p in (t1.address.city, state, t1.address.country) if p]
        return Value(", ".join(parts) or None, "profile address")
    if key == "address_street":
        street = ", ".join(p for p in (t1.address.street, t1.address.unit) if p)
        return Value(street or None, "profile address")
    if key == "address_city":
        return Value(t1.address.city, "profile address")
    if key == "address_state":
        return Value(t1.address.state, "profile address",
                     aliases=(_US_STATES.get((t1.address.state or "").upper(), ""),))
    if key == "address_zip":
        return Value(t1.address.postal_code, "profile address")
    if key == "address_country":
        return Value(t1.address.country, "profile address", aliases=("united states", "usa", "us"))
    return Value(None, "", note="no profile value for this")


# --------------------------------------------------------------------------- the plan


def _approved_answer(q: FormQuestion, approved: AnswersFile) -> Optional[str]:
    for a in approved.answers:
        if any(re.search(p, q.label, re.IGNORECASE) for p in a.question_patterns):
            if a.max_chars and q.max_length and len(a.answer) > q.max_length:
                continue
            return a.answer
    return None


def plan_answers(questions: list[FormQuestion], ctx: AnswerContext) -> AnswerPlan:
    cfg = ctx.cfg
    answers = []
    for q in questions:
        if not q.field_name or q.field_name in SKIP_FIELDS:
            continue
        key, label_conf = key_for(q)
        base = dict(field=q.field_name, label=q.label, type=q.type, required=q.required, options=q.options,
                    section=q.section, key=key, max_length=q.max_length, description=q.description)
        if key == "resume" or (q.type == "file" and key is None and q.required):
            answers.append(PlannedAnswer(**base, kind="file", confidence=1.0, source="the tailored resume PDF"))
            continue
        if key == "gpa":
            answers.append(PlannedAnswer(**base, kind="unknown" if q.required else "skip",
                                         note="GPA is never filled (rule 3)" + ("; it's required, so Vatsal decides"
                                                                                if q.required else "")))
            continue
        if key == "cover_letter":
            answers.append(PlannedAnswer(**base, kind="unknown" if q.required else "skip",
                                         note="a cover letter needs text Vatsal approved"))
            continue
        approved = _approved_answer(q, ctx.approved) if q.type in ("text", "textarea") else None
        if approved:
            answers.append(PlannedAnswer(**base, kind="auto", value=approved, confidence=1.0, source="data/answers.yaml",
                                         status="approved"))
            continue
        if key is None:
            if q.section in ("eeoc", "demographic") and q.options:
                key, label_conf = "eeo_other", 0.9
            else:
                kind = "free_text" if q.type == "textarea" and q.required else ("unknown" if q.required else "skip")
                answers.append(PlannedAnswer(**base, kind=kind, note="no profile value matches this question"))
                continue
        value = value_for(key, q, ctx)
        planned = _decide(q, value, label_conf, cfg)
        answers.append(PlannedAnswer(**base, **planned))
    return AnswerPlan(answers=answers)


def _decide(q: FormQuestion, value: Value, label_conf: float, cfg: Config) -> dict[str, object]:
    if value.text is None:
        return {"kind": "unknown" if q.required else "skip", "note": value.note or "no value in your profile",
                "source": value.source}
    text, match_conf = value.text, 1.0
    if q.options and q.type == "multi_select":
        picked, scores = [], []
        for part in [p.strip() for p in value.text.split(";") if p.strip()]:
            opt, s = match_option(part, q.options, value.aliases)
            if opt is None or s < 0.8:
                picked = []
                break
            picked.append(opt)
            scores.append(s)
        if picked:
            return _finish(q, value, label_conf, "; ".join(dict.fromkeys(picked)), min(scores), cfg)
    if q.options:
        best, score = None, 0.0
        for candidate in value.candidates or (value.text,):
            opt, s = match_option(candidate, q.options, value.aliases)
            if s > score:
                best, score = opt, s
            if s >= 0.95:
                break
        if (best is None or score < 0.8) and value.fallback_other:
            other = next((o for o in q.options if _norm(o) in ("other", "others", "other please specify")), None)
            if other:
                best, score = other, 0.7
        if best is None or score < cfg.match_ask_threshold:
            return {"kind": "unknown" if q.required else "skip", "source": value.source,
                    "note": f"no option fits '{value.text}'", "confidence": round(score, 3)}
        text, match_conf = best, score
    elif q.type == "textarea" and len(text) < 3:
        return {"kind": "unknown" if q.required else "skip", "source": value.source, "note": "needs a written answer"}
    return _finish(q, value, label_conf, text, match_conf, cfg)


def _finish(q: FormQuestion, value: Value, label_conf: float, text: str, match_conf: float,
            cfg: Config) -> dict[str, object]:
    confidence = round(label_conf * match_conf * value.confidence, 3)
    if confidence >= cfg.match_auto_threshold:
        kind = "auto"
    elif confidence >= cfg.match_ask_threshold:
        kind = "confirm"
    else:
        kind = "unknown" if q.required else "skip"
    note = value.note or (f"best guess for '{value.text}'" if kind == "confirm" else None)
    return {"kind": kind, "value": text, "confidence": confidence, "source": value.source, "note": note}


def format_plan(plan: AnswerPlan) -> str:
    order = {"unknown": 0, "free_text": 1, "confirm": 2, "file": 3, "auto": 4, "skip": 5}
    lines = []
    for a in sorted(plan.answers, key=lambda a: (order[a.kind], a.label.lower())):
        star = "*" if a.required else " "
        value = (a.value or "") if a.kind != "file" else "(the tailored resume PDF)"
        if a.kind == "free_text" and a.value:
            value = f"{a.value[:70]}..." if len(a.value) > 70 else a.value
        conf = f" [{a.confidence:.2f}]" if a.kind in ("auto", "confirm") and a.confidence < 1 else ""
        why = f"  ({a.note})" if a.note and a.kind != "auto" else ""
        lines.append(f"  {a.kind:<9}{star} {a.label[:70]}: {value}{conf}{why}")
    return "\n".join(lines)
