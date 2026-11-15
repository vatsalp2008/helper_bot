"""Form answers (spec 10.3, 10.4): what is filled, what is confirmed, what is drafted, what pauses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest
import yaml

from helper.adapters.greenhouse import job_from_api
from helper.answers import AnswerContext, key_for, match_option, plan_answers
from helper.models import AnswersFile, ApprovedAnswer, Config, FormQuestion, Profile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIGMA = job_from_api(json.loads((FIXTURES / "greenhouse_figma_job.json").read_text(encoding="utf-8")),
                     "https://job-boards.greenhouse.io/figma/jobs/6178851004", "figma")
PROFILE = Profile.model_validate(yaml.safe_load((FIXTURES / "profile_sample.yaml").read_text(encoding="utf-8")))
PROFILE.tier1.how_did_you_hear = "Career site"
PROFILE.tier1.work_location_preferences = "Open to all locations"
PROFILE.tier1.preferred_name = PROFILE.tier1.preferred_name or PROFILE.tier1.first_name


def ctx(**kw: object) -> AnswerContext:
    base = dict(profile=PROFILE, cfg=Config(), job=FIGMA, grad_date="May 2027", employment="internship",
                season="summer", season_year=2027, past_employers=["Kalini Industries"])
    base.update(kw)
    return AnswerContext(**base)  # type: ignore[arg-type]


def q(label: str, type_: str = "text", options: Optional[list[str]] = None, required: bool = True,
      name: str = "question_1", description: Optional[str] = None) -> FormQuestion:
    return FormQuestion(label=label, field_name=name, type=type_, options=options or [], required=required,
                        description=description)


def one(question: FormQuestion, **kw: object):  # type: ignore[no-untyped-def]
    (answer,) = plan_answers([question], ctx(**kw)).answers
    return answer


def test_the_figma_form() -> None:
    plan = plan_answers(FIGMA.questions, ctx())
    by_label = {a.label: a for a in plan.answers}
    assert by_label["First Name"].kind == "auto" and by_label["First Name"].value == PROFILE.tier1.first_name
    assert by_label["Resume/CV"].kind == "file"
    grad = by_label["If you are currently enrolled in university or a program, what is your expected graduation date?"]
    assert (grad.kind, grad.value) == ("auto", "Spring 2027")
    hub = by_label["From which US hub do you prefer to work?"]
    assert hub.kind in ("auto", "confirm") and hub.value == "Either"
    assert by_label["Why do you want to join Figma?"].kind == "free_text"
    assert by_label["Are you authorized to work in the country for which you applied?"].value == "Yes"
    previous = by_label["Have you ever worked for Figma before, as an employee or a contractor/consultant?"]
    assert (previous.kind, previous.value) == ("auto", "No")
    assert by_label["Are you Hispanic/Latino?"].value == "Decline To Self Identify"
    assert by_label["Pronouns"].kind == "skip"  # optional, and no option says "decline"
    connect = by_label["How did you connect with us?"]
    assert (connect.kind, connect.value) == ("confirm", "Other")  # the default "Career site" isn't offered
    assert {a.label for a in plan.open_items()} >= {"Why do you want to join Figma?", "How did you connect with us?"}


@pytest.mark.parametrize(
    "value,options,expected",
    [
        ("Yes", ["Yes", "No"], "Yes"),
        ("No", ["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"],
         "No, I do not have a disability"),
        ("Not a protected veteran", ["I don't wish to answer", "I identify as one or more of the classifications of a "
                                     "protected veteran", "I am not a protected veteran"], "I am not a protected veteran"),
        ("Male", ["Decline To Self Identify", "Female", "Male"], "Male"),
        ("Decline to self identify", ["Yes", "No", "Decline To Self Identify"], "Decline To Self Identify"),
    ],
)
def test_option_matching(value: str, options: list[str], expected: str) -> None:
    best, score = match_option(value, options)
    assert best == expected and score >= 0.85


def test_option_matching_refuses_a_poor_fit() -> None:
    assert match_option("Career site", ["FigFest", "Virtual event", "Other"])[1] < 0.6


def test_sponsorship_follows_the_cpt_opt_rule() -> None:
    plain = one(q("Will you now or in the future require visa sponsorship?", "select", ["Yes", "No"]))
    assert (plain.kind, plain.value) == ("auto", "No")
    includes = one(q("Will you require sponsorship? Sponsorship includes CPT and OPT.", "select", ["Yes", "No"]))
    assert includes.value == "Yes"
    unclear = one(q("Do you require sponsorship (CPT/OPT)?", "select", ["Yes", "No"]))
    assert unclear.kind == "unknown" and "CPT/OPT" in (unclear.note or "")
    authorized = one(q("Are you legally authorized to work in the US without sponsorship?", "select", ["Yes", "No"]))
    assert authorized.value == "Yes"


def test_graduation_date_options_follow_the_run() -> None:
    options = ["Spring 2027", "Fall 2027", "Spring 2028"]
    assert one(q("Expected graduation date", "select", options)).value == "Spring 2027"
    assert one(q("Expected graduation date", "select", options), grad_date="December 2027").value == "Fall 2027"
    assert one(q("Expected graduation date")).value == "May 2027"


def test_start_date_by_season() -> None:
    assert one(q("Earliest start date")).value == "May 1, 2027"
    fall = one(q("Earliest start date"), season="fall")
    assert fall.kind == "unknown"  # profile says "ask" for fall internships


def test_gpa_and_cover_letters_are_never_filled() -> None:
    gpa = one(q("Cumulative GPA"))
    assert gpa.kind == "unknown" and gpa.value is None and "rule 3" in (gpa.note or "")
    assert one(q("GPA (optional)", required=False)).kind == "skip"
    assert one(q("Cover Letter", "file", name="cover_letter")).kind == "unknown"


def test_a_past_employer_is_left_to_vatsal() -> None:
    from helper.models import Job

    kalini = Job(company="Kalini Industries", role_title="Intern", description_text="x", source="jd_file",
                 fetched_at="now")
    answer = one(q("Have you ever worked for Kalini Industries before?", "select", ["Yes", "No"]), job=kalini)
    assert answer.kind == "unknown"


def test_unknown_questions() -> None:
    assert one(q("What is your favorite color?")).kind == "unknown"
    assert one(q("Anything else?", "textarea", required=False)).kind == "skip"
    assert one(q("Tell us about a project you are proud of", "textarea")).kind == "free_text"


def test_approved_answers_are_reused() -> None:
    approved = AnswersFile(answers=[ApprovedAnswer(id="why", question_patterns=["why .*join"], answer="Because...")])
    answer = one(q("Why do you want to join Figma?", "textarea"), approved=approved)
    assert (answer.kind, answer.value, answer.source) == ("auto", "Because...", "data/answers.yaml")


def test_label_keys() -> None:
    assert key_for(q("Preferred First Name"))[0] == "preferred_first_name"
    assert key_for(q("LinkedIn Profile"))[0] == "linkedin_url"
    assert key_for(q("What is your expected graduation date?"))[0] == "graduation_date"


# --------------------------------------------------------------------------- forms with checkbox groups (Lever, Ashby)


def test_checkbox_groups() -> None:
    offices = ["New York, NY", "San Francisco, CA", "Remote (US)"]
    every = one(q("Which office locations would you consider?", "multi_select", offices))
    assert (every.kind, every.value) == ("auto", "New York, NY; San Francisco, CA; Remote (US)")  # open to all
    heard = one(q("How did you hear about us? (select all that apply)", "multi_select", ["LinkedIn", "Career site", "Referral"]))
    assert (heard.kind, heard.value) == ("auto", "Career site")
    degree = one(q("Degree Type", "multi_select", ["Bachelor's", "Master's", "PhD"]))
    assert (degree.kind, degree.value) == ("auto", "Master's")


def test_sponsorship_choices_that_name_a_student_visa_pause() -> None:
    choices = ["None", "F1 Visa (CPT/OPT/STEM OPT)", "H-1B", "Other"]
    answer = one(q("Will you require any of the below sponsorship for employment?", "radio", choices))
    assert answer.kind == "unknown" and answer.value is None


def test_a_question_in_the_eeo_survey_is_trusted() -> None:
    assert key_for(q("Race"))[1] == 0.85
    survey = FormQuestion(label="Race", field_name="race", type="radio", options=["Asian", "White"], section="eeoc")
    assert key_for(FormQuestion(**{**survey.model_dump(), "field_name": "ashby:race"})) == ("eeo_race", 0.95)
