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
from datetime import date
from typing import Any, Literal, Optional

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
    searchable: bool = False  # FormQuestion.searchable: the options are what the box showed before typing


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


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _other_year(value: str, option: str) -> bool:
    """Both name a year, and not the same one."""
    years = set(_YEAR_RE.findall(value))
    return bool(years) and bool(set(_YEAR_RE.findall(option))) and not years & set(_YEAR_RE.findall(option))


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
        if _other_year(value, option):  # "May 2027" is never "Spring 2028"
            continue
        if v and (n.startswith(v + " ") or v.startswith(n + " ")):
            score = 0.95
        else:
            tv, to = _tokens(value), _tokens(option)
            score = len(tv & to) / len(tv | to) if tv and to else 0.0
            if tv and tv <= to:
                score = max(score, 0.85)
            if _negative(value) != _negative(option):
                score *= 0.5
            # an alias ("Asian") counts, but a closer match ("South Asian" for "Asian (South Asian)") still wins
            if aliases and any(_norm(a) and (_norm(a) == n or re.search(rf"\b{re.escape(_norm(a))}\b", n))
                               for a in aliases):
                # an option that starts with an alias counts like one that starts with the value itself ("Never
                # been a contractor or an employee" for the past-employer rule's "never", Lucid, 2026-11-19)
                # "I have never been employed by Rubrik ..." starts with "never been employed" once its "I have" is
                # set aside (Rubrik, 2026-11-24)
                bare = re.sub(r"^i (?:have |am |was |ve )?", "", n)
                prefix = any(_norm(a) and (n.startswith(_norm(a) + " ") or bare.startswith(_norm(a) + " "))
                             for a in aliases)
                score = max(score, (0.95 if prefix else 0.9) + (0.02 if tv and tv <= to else 0.0))
        if score > best_score or (score == best_score and best is not None and len(option) < len(best)):
            best, best_score = option, score  # on a tie the closer (shorter) option wins
    return best, round(best_score, 3)


# --------------------------------------------------------------------------- what a question asks

# (key, pattern, confidence that the label means this). First match wins, so order matters.
LABEL_RULES: list[tuple[str, str, float]] = [
    ("gpa", r"\bgpa\b|grade point", 1.0),
    # "How many software engineering or AI projects ... (personal, academic, or internship based)?" counts projects
    # (Direct Supply, 2026-11-16), so it comes before the internship count
    ("project_count", r"\bhow many\b.{0,80}\bprojects?\b|\bnumber of\b.{0,60}\bprojects?\b", 0.9),
    ("internship_count", r"\bhow many\b.{0,100}\binternships?\b|\bnumber of\b.{0,60}\binternships?\b", 0.9),
    ("ai_consent", r"\bhiredscore\b|\bAI[- ]assisted\b.{0,25}?\b(recruit|tool|screen)|\binvolving AI\b|"
                   # "Use of AI for Future AI Matching (Optional)" (Merck, 2026-11-24)
                   r"\buse of (AI|artificial intelligence)\b.{0,40}\bmatch\w*\b|\bAI (matching|screening)\b|"
                   r"\b(consent|opt[- ]?in|agree)\b.{0,80}\b(AI|artificial intelligence)\b|"
                   r"\b(AI|artificial intelligence)\b.{0,80}\b(consent|opt[- ]?in)\b|"
                   # "Do you permit Labcorp to process your data using Artificial Intelligence (AI) tools to evaluate
                   # you...?" (Labcorp, 2026-11-17: always Yes)
                   r"\b(permit|allow)\b.{0,100}\b(AI|artificial intelligence)\b|"
                   r"\bprocess\w* (your|my) (personal )?(data|information)\b.{0,80}\b(AI|artificial intelligence)\b",
     0.9),
    ("terms_consent", r"\b(read and accept|accept the|acknowledge (the|that)|agree to the)\b.{0,100}\b(privacy|terms|notice|"
                      r"policy|conditions)\b|\b(privacy|data protection) (notice|policy)\b.{0,60}\b(accept|acknowledge|"
                      r"agree|consent)\b|\bconfirm\b.{0,40}\b(read|agree)\b.{0,60}\b(privacy|notice|nda|"
                      r"non-?disclosure|terms|policy)\b"  # Shopify: "confirm you've read our applicant privacy notice"
                      # Honeywell Aerospace's Oracle box, 2026-11-20: "I have read the Important Privacy Information"
                      r"|\bI have read\b.{0,60}\b(privacy|terms|notice|policy|disclaimer)\b", 0.9),
    ("relatives_at_company", r"\b(relatives?|family members?|immediate family|related to|close personal relationship)\b"
                             r".{0,120}\b(employ\w*|work\w*|board|trustee)\b", 0.95),
    ("employment_restrictions", r"\bnon-?compete\b|\bemployment restrictions?\b|\brestrictive covenants?\b|"
                                r"\bnon-?solicit|\bcovenant not to compete\b", 0.9),  # Little Caesars, 2026-11-17
    # GE Vernova, 2026-11-24: "do you consent to GE Vernova retaining your Candidate profile* to assess your
    # suitability for roles for which you may not have applied, and to contact you by email" went to the email rule
    ("keep_for_other_roles", r"\bretain\w*\b.{0,80}\b(future|other|alternative)\b.{0,40}\b(roles?|opportunit\w*|"
                             r"positions?|openings?)\b|\btalent (pool|community)\b"
                             r"|\b(roles?|positions?|jobs?|opportunit\w*) (for which|that) you (may |might )?(have )?"
                             r"(not|never)( yet)?( have)? applied\b", 0.9),
    ("transcript", r"\btranscripts?\b|\bacademic record\b", 0.95),
    ("hiring_manager_note", r"\b(note|message)\b.{0,25}\b(hiring manager|recruiter|hiring team)\b", 0.9),
    ("cover_letter", r"\bcover letter\b", 0.95),
    ("resume", r"\bresume\b|\bcv\b|^\W*upload (a |your )?files?\b(?!.*\b(additional|other|supporting)\b)", 0.95),
    # ^ Workday's resume box can say only "Upload a file (5MB max)" (ASML, 2026-11-16)
    # "Chosen Name" (MongoDB on Greenhouse, 2026-11-19) asks the same thing
    ("preferred_first_name", r"\b(preferred|chosen) (first )?name\b|\bnickname\b", 0.95),
    # "First & Last Name" (Strava on Ashby, 2026-11-25) is the whole name, not the last name
    ("full_name", r"\bfirst\s*(&|and|\+|/)\s*last\s+names?\b", 0.95),
    ("first_name", r"\bfirst name\b|\bgiven name\b", 0.95),
    ("last_name", r"\blast name\b|\bsurname\b|\bfamily name\b", 0.95),
    # "What is your Legal Name?" (Rubrik, 2026-11-24)
    ("full_name", r"\bfull (legal )?name\b|^\s*(legal )?name\s*\??\s*$"
                  r"|^\s*(what is |please (enter|provide) )?your (full |legal |full legal )?name\s*\??\s*$", 0.9),
    # "Would you like to receive communications via SMS and/or WhatsApp ... we will only communicate with you via email"
    # (Lucid, 2026-11-19) is a consent choice, not the email box: it was matched against the email address
    ("sms_consent", r"\b(receive|send|opt[- ]?in|consent|agree)\b.{0,80}\b(sms|whatsapp|text messag\w*)\b"
                    r"|\b(sms|whatsapp|text messag\w*)\b.{0,80}\b(receive|opt[- ]?in|consent|agree|communications?|updates)\b",
     0.9),
    ("email", r"\be-?mail\b", 0.95),
    # a phone's extension, not "F-1 STEM OPT extension" in a sponsorship question (Bank of America, 2026-11-25)
    ("phone_extension", r"\b(phone|telephone|tel)\.?\s+extension\b|^\W*extension\W*$|^\s*ext\.?\s*$", 0.9),
    ("phone_type", r"\bphone (device )?type\b|\bdevice type\b|\btype of phone\b", 0.9),
    ("phone_country_code", r"\bcountry (phone )?code\b|\bdialing code\b", 0.9),
    ("phone", r"\bphone\b|(?<!t-)\bmobile\b|\bcell\b", 0.9),  # not "T-Mobile" (2026-11-16)
    ("linkedin_url", r"\blinked ?in\b", 0.95),
    ("github_url", r"\bgit ?hub\b", 0.95),
    ("portfolio_url", r"\bportfolio\b|\bpersonal (web)?site\b|\bwebsite\b|\bweb ?site\b|\bblog\b", 0.88),
    # "authorized to work without sponsorship?" asks about authorization (Yes); any other mention of sponsorship asks
    # whether you need it (No), even when it also says "work authorization" (Fanatics, 2026-11-15)
    ("work_authorization", r"\b((authori[sz]ed|eligible|permitted) to work|legally work|work legally|legal(ly)? "
                           r"(able|allowed|authori[sz]ed) to work)\b.{0,80}\bwithout\b.{0,40}\bsponsor", 0.95),
    # "What is your current visa status? Choose Not Applicable if you do not require sponsorship." (Boston Scientific)
    # and "...indicate the type of visa you currently hold. If you do not require a visa please put NA." (BorgWarner)
    ("visa_status", r"\bwhat is your (current )?visa (status|type)\b|\b(indicate|state|list) (the |your )?(type of visa|"
                    r"visa type)\b|\btype of visa (you|do you)\b|^\W*(current )?visa (status|type)\W*$", 0.95),
    # "Do you have legal authority to work permanently in the United States?" (Great American): a citizen or permanent
    # resident's right, which his profile says he doesn't have
    ("permanent_work", r"\bwork permanently\b|\bpermanent(ly)? (authori[sz]ation|right) to work\b|\bpermanent work "
                       r"authori[sz]ation\b", 0.95),
    ("sponsorship", r"\bsponsor", 0.95),
    # "Will you now or in the future require Vertiv to help you obtain an employment visa?" (Vertiv on Oracle, 2026-11-19)
    ("sponsorship", r"\brequire\b.{0,40}\bhelp you (obtain|get|secure)\b.{0,30}\b(employment|work) visa\b", 0.95),
    # "Are you an F1 student who has or will be applying for CPT, OPT, or STEM OPT?" (Boston Scientific, 2026-11-17):
    # the profile's f1_student answer. After the sponsorship rule, so a sponsorship question naming F-1 stays there.
    ("f1_student", r"\bF-?1\b.{0,12}\b(student|visa|status)\b|\bon an? F-?1\b", 0.95),
    # "Are you a citizen of any of the following countries: Group D (Armenia, ...)" (Analog Devices, 2026-11-16):
    # checked against the profile's citizenship, not the U.S.-citizen answer
    ("citizen_of_listed", r"\bcitizen (of|in) (any|one) (one )?of (these|the following|the listed)\b.{0,20}\bcountr", 0.95),
    # "Is the most recent country from which you hold or have been granted citizenship in Cuba, Iran, North Korea, or
    # Syria?" (Nasdaq, 2026-11-24): the same check, not the country-name answer
    ("citizen_of_listed", r"\bcitizenships?\b[^?]{0,60}\b(in|of|from)\s+(cuba|iran|north korea|syria)\b", 0.95),
    # "Do you currently hold permanent resident alien status (or similar designation) inside of Cuba, Iran, North
    # Korea, or Syria?" (Nasdaq): not the U.S.-status answer; the profile doesn't record residency elsewhere
    ("listed_residency", r"\bpermanent resident\w*\b[^?]{0,60}\b(in|inside( of)?|of)\s+(cuba|iran|north korea|syria)\b",
     0.95),
    # "Please select all countries in which you currently hold citizenship or have an active permanent resident
    # status" (Marvell, 2026-11-18): a country list, not the U.S.-status question below
    # "Please specify dual citizenship if applicable" (Vertiv on Oracle, 2026-11-19): N/A with one citizenship
    # "Do you hold citizenship(s) with country(ies) other than the US?" (Draper, 2026-11-20): from the profile
    ("other_citizenship", r"\bcitizenships?\b.{0,60}\b(other than|besides|apart from|outside( of)?)\s+(the\s+)?"
                          r"(U\.?\s?S\.?A?\b\.?|United States)", 0.95),
    # "Check all of the following statements that apply to you:" with government, military, relatives-in-office and
    # advisor statements and "No statements apply to me" (Honeywell, 2026-11-20): none of them is his
    # a list of statements, never "How did you hear about us? (select all that apply)"
    ("none_apply", r"\b(check|select|mark) all (of )?the following\b[^?]{0,40}\bapply\b|\bstatements? (that|which) appl(y|ies)\b", 0.9),
    ("dual_citizenship", r"\b(dual|second|other|additional) (citizenship|nationality)\b", 0.9),
    # "List those country(ies):" under "Do you hold citizenship(s) with country(ies) other than the US?" (Draper,
    # 2026-11-25): the profile's citizenship
    ("citizenship_country", r"^\W*(please )?(list|name|specify) (those|these|the|all|each|your)? ?countr(y|ies)\b", 0.9),
    ("citizenship_country", r"^\W*nationality\W*$|\bcountry of citizenship\b|\bcitizenship country\b|\bcountry (of|where you hold) "
                            r"citizenship\b|\bwhat is your citizenship\b|^\W*citizenship\W*$|"
                            r"\bcountr(y|ies)\b[^.?]{0,40}\b(hold|have|held)\b[^.?]{0,15}\bcitizenships?\b", 0.9),
    # a list of statuses ("US Citizen / Lawful Permanent Resident / Protected Person / None"; Muon Space, 2026-11-15)
    ("us_citizen", r"\b(immigration|citizenship)\b.{0,60}\bstatus\b|\bU\.?\s?S\.? persons?\b|"
                   r"^\W*please indicate your current status\W*$", 0.9),
    ("work_authorization", r"\b(legally )?(authori[sz]ed|eligible|permitted) to work\b|\bwork authori[sz]ation\b"
                           r"|\bright to work\b", 0.95),
    ("us_citizen", r"\bcitizen\b|\bpermanent resident\b|\bgreen card\b|\bU\.?\s?S\.? persons?\b", 0.9),
    # "Please confirm the number of hours per week you can work and be present in the office..." (Snowflake,
    # 2026-11-19) asks for a number: the on-site rule's "Yes" went into a numbers-only box, which refused it
    ("hours_per_week", r"\b(number of|how many) hours\b.{0,60}\b(per|a|each) week\b|\bhours (per|a|each) week\b"
                       r".{0,80}\b(can you|you can|able|available|commit)\b", 0.9),
    # commuting to the office, on-site or hybrid attendance: always yes (Vatsal, 2026-11-15)
    ("onsite_ok",r"\b(willing|able|can you|comfortable|confirm|meet this requirement)\b.{0,120}\b(commut\w*|on-?site|"
                  r"in[- ]office|in the office|in person|in-person|hybrid)\b|\b(commut\w*|on-?site|in[- ]office|"
                  r"in person|in-person|hybrid)\b.{0,160}\b(willing|able to|can you|meet this requirement|comfortable)\b|"
                  r"\backnowledge\b.{0,80}\b(days? (a week |per week )?in the office|in the office \d|on-?site)\b|"
                  # "Do you acknowledge the work location expectations listed on this job posting?" (Oscar, 2026-11-23)
                  r"\backnowledge\b.{0,40}\bwork location (expectations?|requirements?)\b",
     0.9),
    ("graduation_date", r"\bgraduat\w*\b.*\b(date|when|year|term|semester)\b|\bexpected graduation\b|\bgraduation\b"
                        r"|\bclass of\b|\bwhen (do|will) you graduate\b|\b(term|semester|season)\b.*\bgraduat"
                        # "If you are enrolled in a university or college, what year do you graduate?" (Strava,
                        # 2026-11-25) was read as the school
                        r"|\b(what|which) (year|term|semester)\b.{0,30}\bgraduat"
                        # "When do you expect to complete your current degree program?" (Analog Devices, 2026-11-16)
                        r"|\bwhen (do|will) you (expect to )?(complete|finish)\b.{0,40}\b(degree|program|studies)\b"
                        r"|\b(expected|anticipated) (degree )?completion\b", 0.9),
    # "Do you intend to return to a college or university for at least one additional semester or quarter after the
    # internship ends?" (T-Mobile, 2026-11-16): yes when the run's graduation date comes after the internship
    ("returning_to_school", r"\b(return|returning|go back|going back) to (a |an |your )?(college|university|school|"
                            r"studies|degree program)\b|\b(additional|remaining) (semester|quarter|term)s?\b.{0,60}\bafter\b",
     0.9),
    ("enrolled", r"\bcurrently enrolled\b.*\?\s*$|\bare you (currently )?(a )?(student|enrolled)\b", 0.9),
    ("school_status", r"\bbest describes your (current )?(status|situation)\b|\byear of (school|study)\b|"
                      r"\bclass standing\b|\bacademic (status|standing)\b", 0.85),
    ("previous_employee", r"\b(ever|previously|before|formerly|former)\b.*\b(work(ed)?|employ\w*|contractor|consultant)\b"
                          r"|\b(work(ed)?|employ\w*)\b.*\bin the past\b"
                          r"|\b(have you been|are you currently|are you now)\b.{0,40}\b(employed|working|a contractor)\b"
                          r".{0,20}\b(with|for|by|at)\b"
                          # "Are you a current contractor for Lucid, or any of its entities?" (Lucid, 2026-11-19)
                          r"|\bare you a current (employee|contractor|consultant)\b"
                          # "Are you currently an employee of this company?" (Toro, 2026-11-23)
                          r"|\bare you (currently|now) an? (employee|contractor|consultant)\b"
                          # "Have you worked at AppLovin or any of its affiliates before?" (Wurl, 2026-11-20); never
                          # "worked with" (a skill: "Have you worked with Python before?")
                          r"|\bhave you (ever )?(worked|been employed)\s+(at|for|by)\b.{0,80}\bbefore\b", 0.9),
    # "Have you ever applied for a position at a Ryan entity?", "Have you previously applied for a position at Cboe
    # and/or Bats Global Markets?" (2026-11-24): the tracker knows
    ("applied_before", r"\b(ever|previously|before|already)\b.{0,20}\b(applied|submitted an application)\b"
                       r"|\b(applied|submitted an application)\b.{0,80}\b(before|previously|in the past)\b", 0.9),
    ("major", r"\bmajor\b(?! life)|\bdiscipline\b|\bfield of study\b|\barea of study\b", 0.9),  # not "major life activities"
    # "What is the highest level of education you have completed?" (Scout Motors, 2026-11-18): the finished degree
    # (Bachelor's), not the Master's still in progress. Before the general degree rule.
    # ("...you are pursuing or have completed" still means the degree in progress)
    ("degree_completed", r"^(?!.*\b(?:pursu\w*|currently|in progress|working toward\w*|enrolled)\b).*?(?:"
                         r"\b(?:highest|last)\b.{0,50}\b(?:completed|attained|obtained|earned|finished|achieved)\b|"
                         r"\b(?:completed|attained|obtained|earned)\b.{0,30}\b(?:highest|degree|education)\b)", 0.95),
    ("degree", r"^\s*degree( type| level)?\s*\??\s*$|\bhighest degree\b|\b(type|level) of degree\b|"
               r"\bhighest (level of )?education\b|\beducation level\b", 0.95),
    ("degree", r"\bdegree\b", 0.85),
    # never a question about what he did there: "Technical Accomplishments - School/Internships" took his school's
    # name (Shield AI, 2026-11-23)
    # "Is the university you are currently attending located in the United States?" (Rubrik, 2026-11-24) asks where
    # it is, not its name
    ("school_in_us", r"\b(school|university|college|institution)\b.{0,60}\b(located|based)\b.{0,15}"
                     r"\b(united states|u\.?s\.?a?\b)", 0.9),
    ("school", r"^(?!.*\b(accomplishments?|achievements?|projects?|experiences?|coursework)\b).*?"
               r"(\bschool\b|\buniversity\b|\bcollege\b|\binstitution\b)", 0.85),
    ("relocate_from", r"\b(moving|relocat\w*|move) from\b|\bcity,? state,? (and )?zip\b", 0.9),
    ("relocate", r"\breloca", 0.9),
    # Vatsal, 2026-11-16: counted from the master list; and full-time from May 2027 on (graduating then)
    ("full_time_start", r"\b(able to|available to|can you|could you)\b.{0,30}\b(begin|start)\b.{0,40}\bfull[- ]time\b", 0.9),
    ("start_date", r"\bstart date\b|\bwhen (can|could|would|will) you (be able to |be available to )?(start|begin)\b"
                   r"|\bavailab\w* to (start|begin)\b"
                   r"|\bearliest\b.*\b(start|begin)\b", 0.9),
    # what he earns now isn't the posting's pay: Serko's Workable form got 100000 for "Current Salary" (2026-11-24)
    ("current_salary", r"\b(current|present|previous|prior|last|most recent)\b.{0,20}\b(salary|compensation|pay|wages?"
                       r"|ctc|base)\b", 0.95),
    ("salary", r"\bsalary\b|\bcompensation\b|\bpay (expectation|range)s?\b|\bwages?\b|"
               r"\b(desired|expected|hourly|annual)\b.{0,20}\b(pay|rate|wage)\b", 0.9),
    ("how_did_you_hear", r"\bhow did you (hear|learn|find|connect|come across|come to know)\b|\bwhere did you (hear|find|"
                         r"learn)\b", 0.95),  # "How did you come to know about this opportunity?" (Vertiv, 2026-11-19)
    ("how_did_you_hear", r"\bsource\b", 0.85),
    ("referral", r"\b(referred|referral)\b", 0.9),
    ("age_18", r"\b18\b|\beighteen\b|\blegal age\b", 0.9),
    ("background_check", r"\bbackground (check|screen)", 0.9),
    ("clearance", r"\bclearance\b", 0.9),
    # military service, his or a spouse's or partner's: always No (Vatsal, 2026-11-16). Before the veteran rule.
    ("military_service", r"\b(spouse|domestic partner|partner)\b.{0,80}\b(military|armed forces|served)\b|"
                         r"\b(served|serving|affiliated)\b.{0,60}\b(military|armed forces|national guard|reserves?)\b|"
                         # "Are you a veteran or active member of the armed forces?" (Soros, 2026-11-17)
                         r"\bmembers? of the (u\.?s\.? )?(military|armed forces|national guard|reserves?)\b",
     0.95),
    ("eeo_hispanic", r"\bhispanic\b|\blatin[oax]\b", 0.9),
    # "racial/ethnic"; Oracle's "Select the races you identify with." (2026-11-20)
    ("eeo_race", r"\braces?\b|\bracial\b|\bethnicit|\bethnic (background|identity|origin)\b", 0.85),
    ("eeo_gender", r"\bgender\b|^\s*sex\s*\??\s*$", 0.9),
    ("eeo_veteran", r"\bveteran\b", 0.9),
    ("eeo_disability", r"\bdisabilit", 0.9),
    ("eeo_other", r"\bpronoun|\bsexual orientation\b|\blgbt|\btransgender\b", 0.95),
    ("work_location", r"\bwhich (\w+ )?(office|hub|location|city)\b|\b(prefer|preference)\w*\b.*\b(location|office|hub|site"
                      r"|city)\b|\bwork (location|arrangement)\b", 0.95),
    ("address_zip", r"\bzip\b|\bpostal\b", 0.9),
    ("address_full", r"\b(mailing|home|current|full|permanent|residential) address\b", 0.9),
    # "Address Line 3" took the street again, a copy of line 1 (Honeywell and Fortinet on Oracle, 2026-11-20)
    ("address_line_extra", r"\baddress (line )?[3-9]\b", 0.9),
    ("address_line2", r"\baddress (line )?2\b|^\s*(apartment|apt|suite|unit)\b", 0.85),
    ("address_street", r"\bstreet\b|\baddress line\b|\bmailing address\b|\bhome address\b|^\s*address\s*$", 0.85),
    ("address_country", r"^\s*country\b", 0.85),
    ("address_state", r"^\s*(state|province)\b", 0.85),
    ("address_city", r"^\s*city\b", 0.85),
    ("address_county", r"^\s*county\s*$", 0.85),
    ("signature_date", r"^\s*(today'?s )?date\s*$|^\s*date signed\s*$|\bsignature date\b", 0.9),
    ("language", r"^\s*(preferred )?language\s*$", 0.85),
    ("location_city", r"\blocation\b|\bwhere (do you|are you) (live|located|based)\b|\bcurrent city\b", 0.85),
]
FIELD_KEYS = {
    "first_name": "first_name", "last_name": "last_name", "email": "email", "phone": "phone", "resume": "resume",
    "cover_letter": "cover_letter", "location": "location_city", "gender": "eeo_gender", "race": "eeo_race",
    "hispanic_ethnicity": "eeo_hispanic", "veteran_status": "eeo_veteran", "disability_status": "eeo_disability",
    # Workday's standard fields (the automation id of each question block)
    "workday:legalName--firstName": "first_name", "workday:legalName--lastName": "last_name",
    "workday:emailAddress": "email", "workday:phoneNumber": "phone", "workday:phoneType": "phone_type",
    "workday:countryPhoneCode": "phone_country_code", "workday:extension": "phone_extension",
    "workday:addressLine1": "address_street", "workday:addressLine2": "address_line2", "workday:city": "address_city",
    "workday:countryRegion": "address_state", "workday:postalCode": "address_zip", "workday:country": "address_country",
    "workday:regionSubdivision1": "address_county",
    "workday:source": "how_did_you_hear", "workday:candidateIsPreviousWorker": "previous_employee",
    "workday:veteranStatus": "eeo_veteran", "workday:gender": "eeo_gender", "workday:ethnicity": "eeo_race",
    "workday:acceptTermsAndAgreements": "terms_consent", "workday:linkedInAccount": "linkedin_url",
}
SKIP_FIELDS = {"resume_text", "cover_letter_text"}  # typed-in alternatives to an upload
_US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}


def us_state_name(value: str) -> Optional[str]:
    """'WA' -> 'Washington': for state lists that spell the names out (BambooHR, 2026-11-18)."""
    return _US_STATES.get(value.strip().upper()) if value else None


def us_state_code(value: str) -> Optional[str]:
    """'Washington' or 'wa' -> 'WA'."""
    v = (value or "").strip()
    if v.upper() in _US_STATES:
        return v.upper()
    return next((code for code, name in _US_STATES.items() if name.lower() == v.lower()), None)


# Oracle's geography search boxes (ZIP Code, City, County; Fortinet and Ralliant, 2026-11-20) show the first 500 places
# of a list of every U.S. place until something is typed, each with the larger places it lies in: "02115, Boston,
# Suffolk, MA", "Boston, Suffolk, MA", "Suffolk, MA". The profile's place is put in that shape and searched for.
_PLACE_CHAIN = {"address_zip": ("postal_code", "city", "county", "state"),
                "address_city": ("city", "county", "state"),
                "address_county": ("county", "state")}


def searched_place(key: Optional[str], q: FormQuestion, address: Any) -> Optional[str]:
    """The profile's place as a search box's list writes it, when its options show that shape (every entry ends with a
    state code, and has as many parts as the place's chain). None for any other list."""
    chain = _PLACE_CHAIN.get(key or "")
    if not (chain and q.searchable and q.options):
        return None
    if any(len(o.split(",")) != len(chain) or not re.fullmatch(r"\s*[A-Z]{2}\s*", o.split(",")[-1]) for o in q.options):
        return None
    parts = []
    for name in chain:
        value = (getattr(address, name, None) or "").strip()
        if name == "county":
            value = re.sub(r"(?i)\s+(county|parish|borough)$", "", value)
        if name == "state":
            value = us_state_code(value) or ""
        if not value:
            return None
        parts.append(value)
    return ", ".join(parts)


_FILE_KEYS = ("resume", "cover_letter", "transcript")
_PLUS_PHONE_SITES = ("greenhouse", "lever", "ashby", "workday", "icims")  # take "+1..." as typed
_DATE_KEYS = ("graduation_date", "start_date", "signature_date", "full_time_start")


def _hispanic_choices_only(options: list[str]) -> bool:
    text = " ".join(options).lower()
    return bool(re.search(r"hispanic|latin", text)) and not re.search(
        r"\b(asian|white|black|african|pacific|native|indian|alaska|two or more|caucasian)\b", text)


def key_for(q: FormQuestion) -> tuple[Optional[str], float]:
    if q.field_name in FIELD_KEYS:
        return FIELD_KEYS[q.field_name], 1.0
    # "AI‑assisted" with a non-breaking hyphen and "<b>Just-In-Time Notice</b>" markup (Crowe, 2026-11-25)
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", re.sub(r"<[^>]+>", " ", q.label))
    if q.type == "date" and re.search(r"(?i)\b(today['’]?s|the current) date\b", text):
        # "Enter today's date below to confirm..." under a long AI-consent text (Kensho/HiredScore, 2026-11-16): the
        # consent rule's "Yes" typed into a date box came out as "/01/2001"
        return "signature_date", 0.95
    if re.match(r"(?i)^\W*languages?\b", q.description or ""):  # a Languages section's entry (Workday: "Languages section")
        if re.fullmatch(r"(?i)\W*(spoken\s+)?languages?\W*", text):
            return "spoken_language", 0.95
        if q.type == "boolean" and re.search(r"(?i)\b(fluent|native)\b", text):
            return "language_fluent", 0.95  # "I am fluent in this language."
        if re.search(r"(?i)\b(overall|reading|speaking|writing|comprehension|listening|translation|proficiency|level|fluency)\b", text):
            return "language_level", 0.95
    for key, pattern, confidence in LABEL_RULES:
        if q.type == "file" and key not in _FILE_KEYS:  # an upload takes a document, never a zip code (Riot: ".zip")
            continue
        if key in _FILE_KEYS and q.options:  # a choice naming the resume isn't the upload: "I confirm the information
            continue  # in my application and resume is true... Yes/No" was left empty as a file (Scout, 2026-11-18)
        if q.type == "date" and key not in _DATE_KEYS:  # only a date goes into a date box: "Yes" and "Master of
            continue  # Science" came out as "/01/2001" and "/01/" (Kensho, Analog Devices, 2026-11-16)
        if q.type == "file" and key == "resume" and not re.search(r"(?i)\bresume|\bcv\b", q.label) and \
                re.search(r"(?i)additional|attachment|supporting|other", q.description or "") and \
                not re.search(r"(?i)\bresume|\bcv\b", q.description or ""):
            # "Upload a file" under Additional Attachments isn't the resume box (Camping World, 2026-11-16); under a
            # "Resume/CV/additional documents section" it is (MIPS's Workday, 2026-11-24: left to Vatsal)
            continue
        if re.search(pattern, text, re.IGNORECASE):
            if key == "eeo_race" and q.options and _hispanic_choices_only(q.options):
                key = "eeo_hispanic"  # "What is your ethnicity?" offering only Hispanic or Latino (Soros, 2026-11-17)
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
    internships_done: Optional[int] = None  # internship entries in the master list (Vatsal, 2026-11-16)
    projects_done: Optional[int] = None  # projects in the master list
    documents: dict[str, str] = field(default_factory=dict)  # "transcript" / "undergrad_transcript" -> a file that exists
    grades: dict[str, str] = field(default_factory=dict)  # school -> GPA, from data/grades.yaml (forms only, rule 3)
    today: Optional[date] = None  # tests pin it; otherwise the real date
    applied_at_company: Optional[int] = None  # sent applications at this company in the tracker; None = not looked up


@dataclass
class Value:
    text: Optional[str]
    source: str
    aliases: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()  # alternative forms to try against options, in order
    note: Optional[str] = None
    confidence: float = 1.0
    fallback_other: bool = False  # no good option: suggest "Other" for Vatsal to confirm
    searched: Optional[str] = None  # for a search box whose shown options lack it: the option to search for


@dataclass(frozen=True)
class Pay:
    low: float
    high: float
    period: str  # hour | week | month | year

    @property
    def yearly(self) -> float:
        return (self.low + self.high) / 2 * {"hour": 2080, "week": 52, "month": 12, "year": 1}[self.period]


_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kK])?"
_PAY_RE = re.compile(_MONEY + r"(?:\s*(?:-|\u2013|\u2014|to|and)\s*" + _MONEY + r")?")


def posted_pay(text: str) -> Optional[Pay]:
    """The first pay range a posting states ("$32-$36" hourly, "$120,000 - $150,000" a year), or None."""
    for m in _PAY_RE.finditer(text or ""):
        def amount(number: Optional[str], k: Optional[str]) -> Optional[float]:
            return float(number.replace(",", "")) * (1000 if k else 1) if number else None

        low, high = amount(m.group(1), m.group(2)), amount(m.group(3), m.group(4))
        high = high or low
        if not low or high < low:
            continue
        around = text[max(0, m.start() - 80):m.end() + 40].lower()
        if re.search(r"hour|/\s*hr\b|\bhr\b", around):
            period = "hour"
        elif re.search(r"\bweek", around):
            period = "week"
        elif re.search(r"\bmonth", around):
            period = "month"
        elif re.search(r"year|annual|/\s*yr\b|salary", around) or low >= 20000:
            period = "year"
        else:
            continue  # a fee or a bonus, not a pay range
        if period == "hour" and low >= 500 or period == "year" and low < 1000:
            continue
        return Pay(low, high, period)
    return None


def salary_answer(q: FormQuestion, pay: Pay) -> str:
    """The posting's pay for this question, as a plain number: hourly or monthly when it asks that way, otherwise a
    year; rounded."""
    asked = "hour" if re.search(r"(?i)hour", q.label) else "month" if re.search(r"(?i)month", q.label) else "year"
    if asked == "hour":
        value = round(pay.yearly / 2080)
    elif asked == "month":
        value = round(pay.yearly / 12 / 100) * 100
    else:
        value = round(pay.yearly / 1000) * 1000
    return str(value)  # digits only: number boxes (iCIMS: "only the digits 0 to 9") reject "$36 per hour"


_NEGATIVE_CHOICE = re.compile(r"(?i)\bwithout\b|\bnot\b|n['\u2019]t\b|\bdecline\b|\bopt(?:[- ])?out\b|^\s*no\b|\brefuse\b")


def affirmative_option(options: list[str]) -> Optional[str]:
    """The one choice that agrees ("...with my application involving AI" over "...without..."), if exactly one does."""
    agree = [o for o in options if not _NEGATIVE_CHOICE.search(o)]
    return agree[0] if len(agree) == 1 else None


def _term_end_of(season: Optional[str], year: Optional[int]) -> Optional[tuple[int, int]]:
    """(year, month) an internship term ends."""
    if not season or not year:
        return None
    return {"spring": (year, 5), "summer": (year, 8), "fall": (year, 12), "winter": (year, 3)}.get(season)


def count_option(n: int, options: list[str]) -> Optional[str]:
    """The choice a count falls in: 26 -> "10+", 2 -> "1-3" ("0", "1-3", "4-6", "10+", "More than 5", "5 or more")."""
    for option in options:
        o = option.lower().replace("–", "-")
        if m := re.fullmatch(r"\s*(\d+)\s*(?:-|to)\s*(\d+)\s*(?:\w+\s*)?", o):
            if int(m.group(1)) <= n <= int(m.group(2)):
                return option
        elif m := re.fullmatch(r"\s*(\d+)\s*(?:\+|or more|and (?:above|up))\s*(?:\w+\s*)?", o):
            if n >= int(m.group(1)):
                return option
        elif m := re.fullmatch(r"\s*(?:more than|over)\s*(\d+)\s*(?:\w+\s*)?", o):
            if n > int(m.group(1)):
                return option
        elif m := re.fullmatch(r"\s*(\d+)\s*", o):
            if n == int(m.group(1)):
                return option
    return None


def _grad_candidates(grad: str) -> tuple[str, ...]:
    my = parse_month_year(grad)
    if not my:
        return (grad,)
    season = "Spring" if my.month <= 6 else ("Summer" if my.month <= 8 else "Fall")
    import calendar

    return (grad, f"{calendar.month_abbr[my.month]} {my.year}", f"{my.month:02d}/{my.year}", f"{my.month}/{my.year}",
            f"{my.year}-{my.month:02d}", f"{season} {my.year}", *(("Winter " + str(my.year),) if my.month == 12 else ()),
            # "Winter '27" for December 2027, "Spring '27" for May 2027 (Direct Supply, 2026-11-16)
            *((f"Winter '{my.year % 100:02d}",) if my.month == 12 else ()), f"{season} '{my.year % 100:02d}",
            season)  # a term without the year: "Spring (April - June)"


def _school_status(grad: str, today: Optional[date] = None) -> str:
    """Where Vatsal is in his program, from the run's graduation date."""
    my = parse_month_year(grad)
    today = today or date.today()
    months = (my.year - today.year) * 12 + my.month - today.month if my else 0
    if months <= 12:
        return "I'm in my final year of school"
    if months <= 24:
        return "I'm in my second to last year of school"
    return "I'm in an earlier year of school"


def _start_date(ctx: AnswerContext) -> Value:
    sd = ctx.profile.tier2.earliest_start_date
    pick = {("internship", "summer"): sd.summer_internship, ("internship", "spring"): sd.spring_internship,
            ("internship", "fall"): sd.fall_internship}.get((ctx.employment, ctx.season or ""))
    if ctx.employment == "co_op":
        pick = sd.co_op
    elif ctx.employment == "full_time":
        pick = sd.full_time
    elif ctx.employment == "internship" and not ctx.season and (free := parse_month_year(ctx.cfg.free_from)):
        # no term named (Nelnet, 2026-11-17): the first day he's free (free_from, or today once that has passed)
        start = max(date(free.year, free.month, 1), ctx.today or date.today())
        pick = f"{start:%B} {start.day}, {start.year}"
    if not pick or pick.lower() == "ask":
        return Value(None, "profile earliest_start_date", note="start date for this kind of role is 'ask'")
    if ctx.employment == "internship" and ctx.season_year and not re.search(r"\d{4}", pick):
        pick = f"{pick}, {ctx.season_year}"
    return Value(pick, "profile earliest_start_date", candidates=(pick, *_grad_candidates(pick.replace(",", "")
                                                                                         .replace(" 1 ", " "))))


_YES_NO_STATUS = {"work_authorization", "sponsorship", "permanent_work", "f1_student"}
# One school named in a question: "University of Pennsylvania", "Purdue University", "UPenn", "Penn Engineering".
# Case-sensitive, so "a college or university" names none; generic words before "University" don't count either
_GENERIC_SCHOOL_WORDS = re.compile(r"(?i)^(an?|the|any|your|accredited|recognized|four|four-year|community|public|"
                                   r"private|current|state)$")
_NAMED_SCHOOL_RE = re.compile(r"\bUniversity of(?: the)? [A-Z][\w&.'-]*(?: [A-Z][\w&.'-]*)*"
                              r"|\b[A-Z][\w&.'-]*(?: [A-Z][\w&.'-]*)* (?:University|College|Institute of Technology)\b"
                              r"|\bUPenn\b|\bPenn Engineering\b|\b(?:MIT|CMU|UCLA|USC|NYU|Stanford|Harvard|Princeton|"
                              r"Yale|Cornell|Columbia|Berkeley|Georgia Tech)\b")


def _named_school(label: str) -> Optional[str]:
    for m in _NAMED_SCHOOL_RE.finditer(label or ""):
        words = m.group(0).split()
        while words and _GENERIC_SCHOOL_WORDS.match(words[0]):
            words = words[1:]
        if len(words) >= 2 or (words and words[0] not in ("University", "College")):
            name = " ".join(words)
            if not re.fullmatch(r"(?:University|College|Institute of Technology)", name):
                return name
    return None


def _is_own_school(named: str, education: list[Any]) -> bool:
    """Whether a school a question names is one of his (either name contains the other's distinctive word)."""
    generic = {"university", "college", "of", "the", "institute", "technology", "school", "at", "and"}
    asked = {w for w in re.findall(r"[a-z]+", named.lower()) if w not in generic}
    for e in education:
        own = {w for w in re.findall(r"[a-z]+", (getattr(e, "school", "") or "").lower()) if w not in generic}
        if asked and own and asked & own:
            return True
    return False
_ASKS_DETAIL = re.compile(r"(?i)\bwhen (does|do|will|did)\b[^?]{0,60}\bexpir|\bexpir(y|ation) date\b|\bplease (provide|give|"
                          r"share|list|explain|describe|elaborate)\b|\b(additional|more|further) (detail|information|"
                          r"context)\b|\blist any\b")


_NONE_OPTION_RE = re.compile(r"(?i)^\W*(no(ne)?( of (the|these)( above| statements)?)?|no statements? appl(y|ies)( to me)?"
                             r"|not applicable|n/?a)\W*$|\bno statements? appl(y|ies)\b|\bnone of (the|these)\b")
# statements his rules answer No to: military service (2026-11-16), relatives (none, 2026-11-15), government work
# (none in the master list), conflicts of interest and politically exposed people
_NOT_HIS_RE = re.compile(r"(?i)\b(government|military|armed forces|veteran|politically exposed|public official|official"
                         r"|relative|family member|spouse|advisor|adviser|consultant|conflict of interest|debarred"
                         r"|suspended|elected)\b")


# A spoken-language level as proficiency scales name it, closest first: a scale without "Fluent" tops out at
# "Advanced" or "Full Professional Proficiency" short of native.
_LEVEL_FORMS = {
    "fluent": ("Fluent", "Full Professional Proficiency", "Advanced", "Expert", "Proficient"),
    "native": ("Native", "Native or Bilingual Proficiency", "Bilingual", "Fluent"),
}


def _yes_no(options: list[str]) -> list[str]:
    """[the Yes option, the No option] when a choice is a plain yes or no, else []."""
    yes = [o for o in options if re.fullmatch(r"(?i)\W*yes\W*", o)]
    no = [o for o in options if re.fullmatch(r"(?i)\W*no\W*", o)]
    return [yes[0], no[0]] if len(yes) == 1 and len(no) == 1 and len(options) == 2 else []


def value_for(key: str, q: FormQuestion, ctx: AnswerContext) -> Value:
    t1, t2 = ctx.profile.tier1, ctx.profile.tier2
    text = f"{q.label} {q.description or ''}"

    def tier2(item: object, name: str, **kw: object) -> Value:
        auto = getattr(item, "auto_answer", True)
        value = getattr(item, "value", None)
        return Value(value, f"profile tier2.{name}", confidence=1.0 if auto else 0.7, **kw)  # type: ignore[arg-type]

    if key in ("spoken_language", "language_level", "language_fluent"):  # (Vatsal, 2026-11-21: English, Fluent)
        if not t1.spoken_languages:
            return Value(None, "profile spoken_languages", note="your spoken languages aren't in the profile")
        language, level = t1.spoken_languages[0].language, t1.spoken_languages[0].level
        if key == "spoken_language":
            return Value(language, "profile spoken_languages")
        if key == "language_fluent":
            return Value("Yes" if re.search(r"(?i)fluent|native|bilingual", level) else "No",
                         "profile spoken_languages")
        return Value(level, "profile spoken_languages", candidates=_LEVEL_FORMS.get(level.strip().lower(), (level,)))
    if key == "address_line_extra":
        return Value(None, "nothing for a third address line")
    if key == "none_apply":  # "No statements apply to me" when every statement is one his rules answer No to
        none = [o for o in q.options if _NONE_OPTION_RE.search(o)]
        rest = [o for o in q.options if o not in none]
        if len(none) == 1 and rest and all(_NOT_HIS_RE.search(o) for o in rest):
            return Value(none[0], "his rules: no government service, military, relatives in office or advisory roles")
        return Value(None, "a list of statements that need his answer")
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
        digits = re.sub(r"\D", "", t1.phone or "")
        if ctx.job is not None and ctx.job.ats not in _PLUS_PHONE_SITES and len(digits) == 11 and digits[0] == "1":
            # company sites often reject "+": "Only use digits (0-9), dashes (-) or parentheses ()" (Boston Scientific,
            # 2026-11-17), so a US number goes in as 206-280-0258
            return Value(f"{digits[1:4]}-{digits[4:7]}-{digits[7:]}", "profile phone (US format for this site)")
        return Value(t1.phone, "profile phone")
    if key == "phone_type":
        # BMO's Workday lists only Business/Personal Primary/Secondary (2026-11-18): his mobile is his personal primary
        return Value("Mobile", "your phone is a mobile", aliases=("mobile", "cell", "cell phone", "mobile phone"),
                     candidates=("Mobile", "Cell", "Personal Primary", "Personal", "Home"))
    if key == "phone_country_code":
        us = (t1.phone or "").replace(" ", "").startswith("+1")
        return Value("United States of America (+1)" if us else None, "profile phone", aliases=("+1", "united states"))
    if key == "phone_extension":
        return Value(None, "", note="no extension")
    if key == "linkedin_url":
        return Value(t1.linkedin_url, "profile linkedin_url")
    if key == "github_url":
        return Value(t1.github_url, "profile github_url")
    if key == "portfolio_url":
        return Value(t1.portfolio_url, "profile portfolio_url")
    if key == "school_in_us":  # Northeastern University, in Boston
        return Value("Yes", "profile school (Northeastern University, Boston)", confidence=1.0)
    if key == "school":
        return Value(t1.current_school, "profile current_school")
    if key == "degree_completed":
        done = [e for e in ctx.profile.education if e.end != "per_run"]
        if not done:
            return Value(None, "profile education", note="no finished degree in the profile")
        bachelor = re.search(r"(?i)\bbachelor", done[0].degree)
        return Value(done[0].degree, "profile education (finished degree)",
                     # Merck's Workday list writes it "BA/BS/BSN" (2026-11-24)
                     aliases=("bachelor", "bachelors", "bachelor's", "undergraduate", "4 year", "ba", "bs", "bsc")
                     if bachelor else ())
    if key == "degree":
        return Value(t1.degree, "profile degree", aliases=("master", "masters", "ms", "m s", "graduate"))
    if key == "major":
        return Value(t1.major, "profile major")
    if key == "graduation_date":
        return Value(ctx.grad_date, "this run's graduation date", candidates=_grad_candidates(ctx.grad_date),
                     fallback_other=True)  # no option for it: "Other", then the date in the follow-up box
    current = next((e for e in ctx.profile.education if e.end == "per_run"), None)
    if key == "returning_to_school":
        grad, end = parse_month_year(ctx.grad_date), _term_end_of(ctx.season, ctx.season_year)
        if ctx.employment != "internship" or not grad or not end:
            return Value(None, "this run's graduation date", note="no internship term to compare with")
        back = (grad.year, grad.month) > end
        return Value("Yes" if back else "No", f"this run's graduation date ({ctx.grad_date}) and the internship term")
    if key == "enrolled":
        if (named := _named_school(q.label)) and not _is_own_school(named, ctx.profile.education):
            # "Are you currently enrolled at, or about to graduate from, a UPenn Engineering Program?" (CodeRabbit,
            # 2026-11-25) asks about one school, not whether he's a student
            return Value("No", f"profile education (the question names {named}, not his school)")
        program = [o for o in q.options if re.search(r"(?i)\bmaster", o)]
        if current and q.options and not any(re.fullmatch(r"(?i)\W*(yes|no)\b.*", o) for o in q.options):
            # "What academic program are you currently enrolled in?" lists programs, not Yes/No (Plains, 2026-11-24)
            return Value(program[0] if len(program) == 1 else None, "profile education (a master's in progress)",
                         note=None if len(program) == 1 else "no single master's program among the choices")
        return Value("Yes" if current else None, "profile education (a program in progress)")
    if key == "school_status" and current:
        return Value(_school_status(ctx.grad_date), "this run's graduation date",
                     aliases=("final year", "last year", "senior") if _school_status(ctx.grad_date).endswith("final year")
                     else ())
    if key in _YES_NO_STATUS and not q.options and _ASKS_DETAIL.search(q.label):
        # "If you have a current work authorization/status, when does it expire?" and "Please provide additional detail
        # about your sponsorship needs..." want a date or an explanation, not the yes/no answer (Akuna, 2026-11-18)
        return Value(None, key, note="asks for a date or details about his status, not a yes or no: Vatsal answers it")
    if key == "work_authorization":
        return tier2(t2.work_authorization, "work_authorization")
    if key == "visa_status":
        none = re.search(r"(?i)\b(not applicable|n/?a)\b[^.?]{0,60}\b(do not|don't) (require|need)\b|\b(do not|don't) "
                         r"(require|need)\b[^.?]{0,60}\b(not applicable|n/?a)\b", text)
        needs = (tier2(t2.sponsorship, "sponsorship").text or "").strip().lower()
        if none and needs == "no":
            word = "NA" if re.search(r"\bNA\b", text) else "Not Applicable"
            pick = next((o for o in q.options if re.search(r"(?i)not applicable|^n/?a$", o)), word)
            return Value(pick, "the question's own instruction (no sponsorship needed)")
        return Value(None, "visa type", note="names a visa type: Vatsal answers it (CLAUDE.md)")
    if key == "f1_student":
        return tier2(t2.f1_student, "f1_student")
    if key == "permanent_work":
        given = tier2(t2.us_citizen_or_permanent_resident, "us_citizen_or_permanent_resident")
        yes = (given.text or "").strip().lower() in ("yes", "true")
        return Value("Yes" if yes else "No", "profile us_citizen_or_permanent_resident (permanent right to work)")
    if key == "sponsorship":
        mentions = re.search(r"\b(CPT|OPT|F-?1)\b", f"{text} {' '.join(q.options)}")
        # "(i.e., H-1B, Visa Status, OPT, L2)" defines sponsorship to include OPT (Badger Meter, 2026-11-23)
        # "(ex: F-1 student visa, H-1B visa status)" gives F-1 as a kind of sponsorship (Cboe, 2026-11-24), like the
        # "(e.g. H-1B visa status, current F-1 visa)" question Vatsal answered Yes on 2026-11-15
        # "(example: the application or transfer of F-1 STEM OPT extension, H-1B ...)" (Bank of America, 2026-11-25)
        includes = re.search(r"(includ\w*|such as|e\.g\.|i\.e\.|\bex\b[:.]?|\bexamples?\b|means|counts?|consider\w*)"
                             r"[^?]{0,60}?\b(CPT|OPT|F-?1)\b"
                             r"|\b(CPT|OPT)\b[^.?]{0,40}\b(count|includ|consider)", text, re.I)
        # "not including OPT" or "other than F-1" leaves student status out of it
        excludes = re.search(r"(not includ\w*|exclud\w*|other than|except\w*|aside from)[^?]{0,40}?\b(CPT|OPT|F-?1)\b",
                             text, re.I)
        if mentions and includes and not excludes and t2.sponsorship.yes_when_question_includes_cpt_opt:
            return Value("Yes", "rule: sponsorship that includes CPT/OPT")
        if mentions:
            return Value(None, "rule: sponsorship and CPT/OPT",
                         note="names CPT/OPT/F-1 (in the question or its choices) without saying they count")
        return tier2(t2.sponsorship, "sponsorship")
    if key == "citizen_of_listed":
        if not t1.citizenship:
            return Value(None, "profile citizenship")
        listed = re.search(rf"(?i)\b{re.escape(t1.citizenship)}\b", q.label)
        return Value("Yes" if listed else "No", "profile citizenship (checked against the countries listed)")
    if key == "listed_residency":
        return Value(None, "", note="permanent residency in the listed countries isn't in the profile: Claude answers")
    if key == "other_citizenship":  # a citizenship besides the U.S. one (Draper, 2026-11-20)
        if not t1.citizenship:
            return Value(None, "profile citizenship")
        parts = [p.strip() for p in re.split(r",|/|;|&|\band\b", t1.citizenship) if p.strip()]
        us = r"(?i)(the )?(u\.?\s?s\.?a?\.?|united states( of america)?|american)"
        other = any(not re.fullmatch(us, p) for p in parts)
        return Value("Yes" if other else "No", "profile citizenship (a country other than the U.S.)")
    if key == "dual_citizenship":  # one citizenship in the profile: none to name (Vertiv, 2026-11-19)
        single = bool(t1.citizenship) and not re.search(r",|/|;|&|\band\b", t1.citizenship)
        if not single:
            return Value(None, "profile citizenship", note="more than one citizenship: Vatsal answers")
        return Value("N/A" if not q.options else "No", "profile citizenship (one)",
                     aliases=("no", "none", "n/a", "not applicable"))
    if key == "citizenship_country":  # from the profile (Vatsal, 2026-11-16)
        if not t1.citizenship:
            return Value(None, "profile citizenship")
        year = re.search(r"\byear\b", q.label, re.I) and t1.citizenship_since
        text = f"{t1.citizenship}, since {t1.citizenship_since} (by birth)" if year and not q.options else t1.citizenship
        return Value(text, "profile citizenship")
    if key == "us_citizen":
        given = tier2(t2.us_citizen_or_permanent_resident, "us_citizen_or_permanent_resident")
        yes_no = any(_norm(o) in ("yes", "no") for o in q.options)
        if q.options and not yes_no and (given.text or "").strip().lower() in ("no", "false"):
            # a list of statuses (US Citizen, Permanent Resident, Protected Person, None): none of them (Vatsal,
            # 2026-11-15)
            none = [o for o in q.options if re.search(r"(?i)^\W*none\b|none of (the )?(above|these)|^\W*neither\b|"
                                                       r"not an? u\.?\s?s\.? (citizen|person)|non-u\.?\s?s|"
                                                       r"^\W*all others\b.{0,40}\bvisa holder", o)]  # ASML
            if len(none) == 1:
                return Value(none[0], "profile us_citizen_or_permanent_resident (none of these statuses)",
                             confidence=given.confidence)
        return given
    if key == "sms_consent":
        return Value(None, "", note="a text-message consent choice: Claude answers it")
    if key == "hours_per_week":
        role = f"{ctx.job.role_title if ctx.job is not None else ''} {q.label}"
        if re.search(r"(?i)\bsummer\b|\bfull[- ]time\b", role):
            return Value("40", "rule: a summer internship or full-time role is full time, 40 hours a week")
        return Value(None, "rule: hours per week", note="school-year hours are Vatsal's to give")
    if key == "onsite_ok":
        return Value("Yes", "rule: commuting and on-site work, always yes (Vatsal, 2026-11-15)",
                     aliases=("yes", "i can", "i am willing", "i am able"))
    open_to_all = "all" in (t1.work_location_preferences or "").lower()
    if key in ("relocate", "work_location") and q.type == "multi_select" and q.options and open_to_all:
        return Value("; ".join(q.options), "profile work_location_preferences (open to all)", confidence=0.95)
    if key == "relocate":
        return Value(t1.willing_to_relocate, "profile willing_to_relocate")
    if key == "relocate_from":  # where he'd move from: home (Vatsal, 2026-11-15: "Seattle, Washington - 98105")
        a = t1.address
        state = _US_STATES.get((a.state or "").upper(), a.state or "")
        place = ", ".join(p for p in (a.city, state) if p)
        return Value(f"{place} - {a.postal_code}" if place and a.postal_code else place or None, "profile address")
    if key == "work_location":
        return Value(t1.work_location_preferences, "profile work_location_preferences",
                     aliases=("either", "any", "no preference", "open to", "flexible", "all locations", "both"))
    if key == "start_date":
        if q.options:  # Vatsal, 2026-11-15: internships can start now (he's free from January); full-time in May
            now = [o for o in q.options if re.search(r"(?i)\b(start(ing)? now|immediately|right away|asap|as soon as)\b", o)]
            summer = [o for o in q.options if re.search(r"(?i)\bsummer\b|\bmay\b", o)]
            pick = now if ctx.employment == "internship" else summer if ctx.employment == "full_time" else []
            if len(pick) == 1:
                return Value(pick[0], "rule: internships can start now, full-time roles in the summer (Vatsal, "
                                      "2026-11-15)", confidence=0.95)
        if ctx.employment in ("internship", "co_op") and _yes_no(q.options):
            # "Are you able to start on the start date listed on the job posting?" (Plains, 2026-11-24): he's free
            # from January 2027, so an internship's own start date works
            return Value(_yes_no(q.options)[0], "rule: internships can start now; he's free from January 2027 "
                         "(Vatsal, 2026-11-15)", confidence=0.95)
        return _start_date(ctx)
    if key == "current_salary":
        return Value(None, "", note="what you earn now isn't in the profile or the master list, so you answer it")
    if key == "salary":
        pay = posted_pay(ctx.job.description_text) if t2.salary_from_posting else None
        if pay is not None:
            return Value(salary_answer(q, pay), "the posting's pay, midpoint rounded (Vatsal, 2026-11-14)")
        return tier2(t2.salary_expectation, "salary_expectation")
    if key == "employment_restrictions":
        return tier2(t2.employment_restrictions, "employment_restrictions")
    if key == "military_service":
        if q.options:
            never = [o for o in q.options if re.search(r"(?i)^\W*no\b|never served|have not served", o)]
            if len(never) == 1:
                return Value(never[0], "rule: military service questions are No (Vatsal, 2026-11-16)")
        return Value("No", "rule: military service questions are No (Vatsal, 2026-11-16)")
    if key in ("internship_count", "project_count"):
        n = ctx.internships_done if key == "internship_count" else ctx.projects_done
        what = "internships" if key == "internship_count" else "projects"
        if n is None:
            return Value(None, "master list")
        bucket = count_option(n, q.options)
        if bucket:
            return Value(bucket, f"master list: {n} {what}")
        return Value(str(n), f"master list: {n} {what}", candidates=(str(n), f"{n}+", "4+" if n >= 4 else str(n)))
    if key == "full_time_start":
        asked = re.search(r"(?i)\b(january|february|march|april|may|june|july|august|september|october|november|"
                          r"december|spring|summer|fall|winter)\s+(\d{4})\b", q.label)
        ready = parse_month_year(ctx.profile.tier2.earliest_start_date.full_time or "")
        when = parse_month_year(" ".join(asked.groups())) if asked else None
        if when and ready:
            ok = (when.year, when.month) >= (ready.year, ready.month)
            return Value("Yes" if ok else "No", "profile full-time start (graduating May 2027)")
        return Value(None, "profile full-time start", note="no date in the question")
    if key == "relatives_at_company":  # Vatsal, 2026-11-15: none, always ("N/A" where a written answer is asked)
        given = tier2(t2.relatives_at_company, "relatives_at_company",
                      aliases=("no", "none", "n/a", "not applicable", "i do not", "no relatives"))
        none = (given.text or "").strip().lower() in ("no", "none", "n/a")
        if none and not q.options and q.type in ("text", "textarea"):
            return Value("N/A", given.source, confidence=given.confidence)
        return given
    if key in ("ai_consent", "keep_for_other_roles"):
        item = t2.ai_screening_consent if key == "ai_consent" else t2.keep_for_other_roles
        name = "ai_screening_consent" if key == "ai_consent" else "keep_for_other_roles"
        given = tier2(item, name)
        if given.text and given.text.strip().lower() in ("yes", "true") and q.options:
            agree = affirmative_option(q.options)
            return Value(agree, given.source, confidence=given.confidence) if agree else \
                Value(None, given.source, note="can't tell which choice opts in")
        return given
    if key == "how_did_you_hear":
        aliases = ("career site", "careers site", "company website", "careers page", "career page", "company career",
                   "careers website", "website")
        if q.options and t1.how_did_you_hear:
            best, score = match_option(t1.how_did_you_hear, q.options, aliases)
            if best is None or score < 0.8:  # not offered: social media, else Other, else any (Vatsal, 2026-11-16)
                real = [o for o in q.options if not re.match(r"(?i)^\W*(select|choose|--)", o)
                        and not re.search(r"(?i)referr|employee|recruiter|friend", o)]  # never claims a referral
                pick = next((o for o in real if re.search(r"(?i)social media", o)), None) \
                    or next((o for o in real if re.match(r"(?i)^\W*other\b", o)), None) or (real[0] if real else None)
                return Value(pick, "rule: any source when the usual one isn't listed (Vatsal, 2026-11-16)")
        return Value(t1.how_did_you_hear, "profile how_did_you_hear", fallback_other=True, aliases=aliases)
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
        # Some forms split "not a veteran" from "a veteran, just not a protected one"; "Not a protected veteran"
        # takes the first, never an option that says you are a veteran.
        negative = re.match(r"\s*(i am )?not\b", eeo.veteran_status or "", re.I)
        return Value(eeo.veteran_status, "profile eeo.veteran_status", confidence=eeo_conf,
                     aliases=("not a protected veteran", "not a veteran", "i am not"),
                     candidates=("I am not a veteran", "I am not a protected veteran", eeo.veteran_status or "")
                     if negative else ())
    if key == "eeo_disability":
        return Value(eeo.disability_status, "profile eeo.disability_status", confidence=eeo_conf)
    if key == "eeo_hispanic" and eeo.hispanic_latino:
        no = eeo.hispanic_latino.strip().lower() in ("no", "not hispanic or latino")
        return Value(eeo.hispanic_latino, "profile eeo.hispanic_latino", confidence=eeo_conf,
                     aliases=("not hispanic or latino", "no, not hispanic", "non-hispanic", "i am not") if no else ())
    if key in ("eeo_hispanic", "eeo_other"):
        return Value(eeo.default_for_other, "profile eeo.default_for_other", aliases=tuple(DECLINE), confidence=eeo_conf)
    if key == "previous_employee":
        # all of Vatsal's work history is in the master list (Vatsal, 2026-11-16): an employer that isn't there, this
        # company or its affiliates, dealers and contractors, gets "No"
        if any(same_company(e, ctx.job.company) or contains_word(q.label, e) for e in ctx.past_employers):
            return Value(None, "master list", note="the question names an employer in your master list: answer it yourself")
        return Value("No", "master list: no work there (it holds all your experience)", confidence=1.0,
                     aliases=("i have not", "have not worked", "never worked", "never been employed", "none of the above",
                              "never"))  # "Never been a contractor or an employee" (Lucid, 2026-11-19)
    if key == "applied_before":
        if ctx.applied_at_company is None:
            return Value(None, "tracker")
        if ctx.applied_at_company:
            return Value("Yes", f"tracker: {ctx.applied_at_company} earlier application(s) at {ctx.job.company}",
                         confidence=1.0)
        return Value("No", f"tracker: no earlier application at {ctx.job.company}", confidence=1.0)
    if key == "location_city":
        state = _US_STATES.get((t1.address.state or "").upper(), t1.address.state or "")
        parts = [p for p in (t1.address.city, state, t1.address.country) if p]
        return Value(", ".join(parts) or None, "profile address")
    if key == "address_street":
        street = ", ".join(p for p in (t1.address.street, t1.address.unit) if p)
        return Value(street or None, "profile address")
    if key == "address_line2":
        return Value(t1.address.unit, "profile address")
    if key == "address_full":  # one box for the whole address
        a = t1.address
        place = " ".join(p for p in (a.state, a.postal_code) if p)
        parts = [p for p in (a.street, a.unit, a.city, place, a.country) if p]
        return Value(", ".join(parts) or None, "profile address")
    if key == "terms_consent":  # privacy notices and terms: accepted (Vatsal, 2026-11-14 and 2026-11-15)
        if q.options and q.type not in ("boolean", "multi_select"):
            agree = affirmative_option(q.options)
            return Value(agree, "you accept privacy notices and terms") if agree else \
                Value(None, "", note="can't tell which choice accepts")
        return Value("Yes", "you accept privacy notices and terms")
    if key == "signature_date":
        return Value(date.today().strftime("%m/%d/%Y"), "today's date")
    if key == "language":
        return Value("English", "you apply in English", confidence=0.9)
    if key == "address_city":
        return Value(t1.address.city, "profile address", searched=searched_place(key, q, t1.address))
    if key == "address_state":
        return Value(t1.address.state, "profile address",
                     aliases=(_US_STATES.get((t1.address.state or "").upper(), ""),))
    if key == "address_zip":
        return Value(t1.address.postal_code, "profile address", searched=searched_place(key, q, t1.address))
    if key == "address_country":
        us = _norm(t1.address.country or "") in ("united states", "united states of america", "usa", "us")
        return Value(t1.address.country, "profile address", aliases=("united states", "usa", "us"),
                     candidates=("United States of America", "United States", "USA") if us else ())
    if key == "address_county":
        return Value(t1.address.county, "profile address", searched=searched_place(key, q, t1.address))
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
                    section=q.section, key=key, max_length=q.max_length, description=q.description,
                    searchable=q.searchable)
        if key == "resume" or (q.type == "file" and key is None and q.required):
            answers.append(PlannedAnswer(**base, kind="file", confidence=1.0, source="the tailored resume PDF"))
            continue
        if key == "gpa":
            gpa = gpa_for(q, ctx)
            if gpa is None:
                answers.append(PlannedAnswer(**base, kind="unknown" if q.required else "skip",
                                             note="no GPA in data/grades.yaml for this (rule 3)"
                                                  + ("; it's required, so Vatsal decides" if q.required else "")))
            else:
                answers.append(PlannedAnswer(**base, **_decide(q, Value(gpa, "data/grades.yaml (Vatsal, 2026-11-15)"),
                                                               1.0, cfg)))
            continue
        if key == "cover_letter":
            answers.append(_cover_letter(q, base, ctx))
            continue
        if key == "hiring_manager_note" and q.type in ("textarea", "text"):
            answers.append(PlannedAnswer(**base, kind="free_text", note=NOTE_STYLE))
            continue
        if key == "transcript" and q.type == "file":
            answers.append(_transcript(q, base, ctx))
            continue
        # a remembered written answer only where no rule matched: rules follow the run (graduation date, salary...);
        # a remembered "Winter '27" was about to go into Quest's graduation box (2026-11-17)
        approved = _approved_answer(q, ctx.approved) if q.type in ("text", "textarea") and key is None else None
        if approved:
            answers.append(PlannedAnswer(**base, kind="auto", value=approved, confidence=1.0, source="data/answers.yaml",
                                         status="approved"))
            continue
        if key is None:
            if q.section in ("eeoc", "demographic") and q.options:
                key, label_conf = _eeo_key_from_options(q.options), 0.9
                base["key"] = key
            else:
                kind = "free_text" if q.type == "textarea" and q.required else ("unknown" if q.required else "skip")
                answers.append(PlannedAnswer(**base, kind=kind, note="no profile value matches this question"))
                continue
        value = value_for(key, q, ctx)
        planned = _decide(q, value, label_conf, cfg)
        answers.append(PlannedAnswer(**base, **planned))
    _generic_uploads(answers, questions, ctx)
    _split_street(answers, ctx)
    _apply_remembered(answers, ctx)
    return AnswerPlan(answers=answers)


_GENERIC_UPLOAD = re.compile(r"(?i)^\W*(file|upload( a)?( file)?|attach(ment)?( a)?( file)?|choose (a )?file|browse)\W*$")


def _generic_uploads(answers: list[PlannedAnswer], questions: list[FormQuestion], ctx: AnswerContext) -> None:
    """A form whose upload boxes are only labelled "File" (Laborie on Jobvite, 2026-11-16: both were skipped and the
    resume never went in): with no box named for the resume, the first gets the resume, the second the cover letter."""
    if any(a.key == "resume" or (a.kind == "file" and a.type == "file") for a in answers):
        return
    generic = [a for a in answers if a.type == "file" and a.key is None and _GENERIC_UPLOAD.match(a.label or "")]
    if not generic:
        return
    first = generic[0]
    first.key, first.kind, first.confidence, first.source, first.note = (
        "resume", "file", 0.9, "the tailored resume PDF (first unlabelled upload box)", None)
    if len(generic) > 1:
        second = generic[1]
        q = next((q for q in questions if q.field_name == second.field), None)
        if q is not None:
            letter = _cover_letter(q, dict(field=second.field, label=second.label, type=second.type,
                                           required=second.required, options=second.options, section=second.section,
                                           key="cover_letter", max_length=second.max_length,
                                           description=second.description), ctx)
            answers[answers.index(second)] = letter


CHOICE_TYPES = ("select", "radio", "multi_select", "yesno", "boolean", "prompt")
NEVER_REMEMBERED = ("gpa", "resume", "cover_letter", "transcript", "file")


def _apply_remembered(answers: list[PlannedAnswer], ctx: AnswerContext) -> None:
    """A choice question helper had no sure answer for gets the answer remembered from an earlier posting
    (data/answers.yaml; Vatsal, 2026-11-15). helper's own rules come first, so answers that change over time (like
    "final year of school") keep following the profile."""
    for a in answers:
        if a.type not in CHOICE_TYPES or a.kind not in ("unknown", "confirm", "skip") or (a.key in NEVER_REMEMBERED):
            continue
        stored = _approved_answer(FormQuestion(label=a.label, field_name=a.field, type=a.type), ctx.approved)
        if not stored:
            continue
        if a.options:
            best, score = match_option(stored, a.options)
            if best is None or score < 0.8:
                continue
            stored = best
        a.kind, a.value, a.status, a.confidence, a.note = "auto", stored, "approved", 1.0, None
        a.source = "data/answers.yaml (remembered)"


def label_pattern(label: str) -> str:
    """A pattern that matches this question again, on any posting."""
    words = re.findall(r"[A-Za-z0-9']+", label.replace("\u2019", "'"))
    return r"^\W*" + r"\W+".join(re.escape(w).replace("'", "['\u2019]") for w in words) + r"\W*$"


def gpa_for(q: FormQuestion, ctx: AnswerContext) -> Optional[str]:
    """The GPA a form asks for: the current program's, or the bachelor's when the question says undergraduate.
    Forms only; never on a resume or cover letter (rule 3). A question about the scale is left to Vatsal."""
    text = f"{q.label} {q.description or ''}"
    # a question about the scale, or a GPA on another one, is Vatsal's; "(4.0 Scale)" only says how the GPA reads
    # (Honeywell Aerospace's Oracle form, 2026-11-20)
    four = re.search(r"(?i)\b4(?:\.0+)?[- ]?(?:point )?scale\b|\bscale of 4(?:\.0+)?\b|\bout of 4(?:\.0+)?\b", text)
    if (re.search(r"(?i)\bscale\b", text) and not four) or not ctx.grades:
        return None
    undergrad = re.search(r"(?i)under ?grad|bachelor", text)
    school = next((e.school for e in ctx.profile.education
                   if (e.degree.lower().startswith("bachelor") if undergrad else e.end == "per_run")), None)
    value = ctx.grades.get(school or "")
    if value and q.options:
        try:
            number = float(value)
        except ValueError:
            return value
        for option in q.options:  # "3.5 - 3.79", "3.5-4.0", "3.5-4", "3.5 and above", "Above 3.5", "Lower than 2.74"
            m = re.search(r"(?<![\d.])(\d(?:\.\d+)?)\s*(?:-|to|\u2013)\s*(\d(?:\.\d+)?)(?![\d.])", option)
            if m and float(m[1]) <= number <= float(m[2]):
                return option
            m = re.search(r"(?i)(?:above|over|at least)\s*(\d\.\d+)|(\d\.\d+)\s*(?:\+|and above|or higher)", option)
            if m and number >= float(m[1] or m[2]):
                return option
            m = re.search(r"(?i)(?:below|under|lower than|less than)\s*(\d(?:\.\d+)?)", option)
            if m and number < float(m[1]):
                return option
    return value


NOTE_STYLE = ("a short note in Vatsal's style: 'Hi,' on top, then two short paragraphs, about 60 to 90 words in "
              "all (one specific story from the resume, then one honest line about fit that names a real gap if the "
              "posting asks for something he hasn't done), and 'Thank you,' with 'Vatsal Patel' on the next line")


def _cover_letter(q: FormQuestion, base: dict[str, object], ctx: AnswerContext) -> PlannedAnswer:
    """The run's cover letter (one is written with every resume, Vatsal 2026-11-14): the PDF for an upload, the text
    for a text box. It passed helper's checks, so it needs no separate review and doesn't hold up auto-submit
    (Vatsal, 2026-11-15)."""
    pdf, text = ctx.documents.get("cover_letter"), ctx.documents.get("cover_letter_text")
    if q.type == "file" and pdf:
        return PlannedAnswer(**base, kind="file", value=pdf, confidence=1.0, source="this posting's cover letter")
    if q.type in ("textarea", "text") and text:
        if q.max_length and len(text) > q.max_length:
            return PlannedAnswer(**base, kind="unknown" if q.required else "skip",
                                 note=f"the cover letter has {len(text)} characters; this box takes {q.max_length}")
        return PlannedAnswer(**base, kind="auto", value=text, confidence=1.0, source="this posting's cover letter")
    return PlannedAnswer(**base, kind="unknown" if q.required else "skip", note="this run has no cover letter yet")


def _transcript(q: FormQuestion, base: dict[str, object], ctx: AnswerContext) -> PlannedAnswer:
    """A required transcript upload gets the current program's transcript, or the undergraduate one when the question
    asks for that. Optional transcript uploads stay empty (Vatsal: "if needed")."""
    undergrad = re.search(r"(?i)under ?grad|bachelor", f"{q.label} {q.description or ''}")
    which = "undergrad_transcript" if undergrad else "transcript"
    if not q.required:
        return PlannedAnswer(**base, kind="skip", note="optional transcript upload: left empty")
    path = ctx.documents.get(which)
    if not path:
        return PlannedAnswer(**base, kind="unknown", note=f"asks for a transcript: set documents.{which} in data/profile.yaml")
    return PlannedAnswer(**base, kind="file", value=path, confidence=1.0,
                         source="your undergraduate transcript" if undergrad else "your transcript")


def _eeo_key_from_options(options: list[str]) -> str:
    """An EEO question without a telling label ("Please check one of the boxes below"), named by its choices."""
    text = " ".join(options).lower()
    for key, pattern in (("eeo_disability", r"disabilit"), ("eeo_veteran", r"veteran"), ("eeo_hispanic", r"hispanic|latin"),
                         ("eeo_gender", r"\b(male|female|man|woman)\b")):
        if re.search(pattern, text):
            return key
    return "eeo_other"


def _split_street(answers: list[PlannedAnswer], ctx: AnswerContext) -> None:
    """With a separate address line 2, line 1 holds the street alone."""
    if any(a.key == "address_line2" and a.value for a in answers):
        street = ctx.profile.tier1.address.street
        for a in answers:
            if a.key == "address_street" and a.value and street:
                a.value = street


def _decide(q: FormQuestion, value: Value, label_conf: float, cfg: Config) -> dict[str, object]:
    if value.text is None:
        return {"kind": "unknown" if q.required else "skip", "note": value.note or "no value in your profile",
                "source": value.source}
    text, match_conf = value.text, 1.0
    numbers_only = q.type == "number" or bool(re.search(r"(?i)\bnumbers? only\b|\bdigits only\b|\bnumeric\b", q.label))
    if numbers_only and not re.fullmatch(r"\s*\$?\s*\d[\d,]*(\.\d+)?\s*[kK]?\s*", value.text):
        return {"kind": "unknown" if q.required else "skip", "source": value.source,
                "note": f"the field takes a number only; your profile says '{value.text}'"}
    if q.type == "boolean":  # one checkbox: the value says whether to tick it, its label is not a choice
        return _finish(q, value, label_conf, text, 1.0, cfg)
    if q.options and value.searched:  # a place in a search box: its own entry, or found by searching at fill time
        opt, s = match_option(value.searched, q.options)  # never a looser match (two towns can share a ZIP code)
        return _finish(q, value, label_conf, opt if opt is not None and s >= 1.0 else value.searched, 1.0, cfg)
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
            if opt is not None and _other_year(value.text, opt):  # a year-less form ("Spring") of a dated value
                continue
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
    elif q.type == "textarea" and len(text) < 3 and text.strip().upper() not in ("NA", "N/A"):  # "put NA" (BorgWarner)
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
        value = (a.value or "") if a.kind != "file" else (f"({a.value.rsplit('/', 1)[-1]})" if a.key in (
            "transcript", "cover_letter") and a.value else "(the tailored resume PDF)")
        if a.kind == "free_text" and a.value:
            value = f"{a.value[:70]}..." if len(a.value) > 70 else a.value
        conf = f" [{a.confidence:.2f}]" if a.kind in ("auto", "confirm") and a.confidence < 1 else ""
        why = f"  ({a.note})" if a.note and a.kind != "auto" else ""
        lines.append(f"  {a.kind:<9}{star} {a.label[:70]}: {value}{conf}{why}")
    return "\n".join(lines)
