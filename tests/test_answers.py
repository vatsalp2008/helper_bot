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


def test_transcripts_are_uploaded_only_when_a_form_requires_one() -> None:
    from helper.answers import format_plan

    docs = {"transcript": "/docs/Transcript NEU.pdf", "undergrad_transcript": "/docs/Transcript.pdf"}
    required = one(q("Unofficial Transcript", "file", name="transcript_upload"), documents=docs)
    assert (required.kind, required.key, required.value) == ("file", "transcript", "/docs/Transcript NEU.pdf")
    assert "(Transcript NEU.pdf)" in format_plan(plan_answers([q("Unofficial Transcript", "file", name="t")],
                                                               ctx(documents=docs)))
    undergrad = one(q("Undergraduate transcript", "file", name="t2"), documents=docs)
    assert undergrad.value == "/docs/Transcript.pdf"
    assert one(q("Transcript (optional)", "file", required=False), documents=docs).kind == "skip"
    missing = one(q("Transcript", "file"))
    assert missing.kind == "unknown" and "documents.transcript" in (missing.note or "")
    resume = one(q("Resume/CV", "file", name="resume"), documents=docs)
    assert (resume.kind, resume.key, resume.value) == ("file", "resume", None)  # the resume is unchanged


def test_a_numbers_only_salary_box_goes_to_vatsal() -> None:
    salary = one(q("What are your annual salary expectations?", "number"))
    assert salary.kind == "unknown" and "number only" in (salary.note or "")
    assert one(q("What are your annual salary expectations?")).value == "Negotiable"  # a text box takes the word


def test_salary_comes_from_the_posting_when_vatsal_says_so() -> None:
    from helper.answers import posted_pay
    from helper.models import Job

    assert posted_pay("anticipates the hourly range for this position to be between $32-$36.") .yearly == 70720
    assert posted_pay("The base salary range is $120,000 - $150,000 per year.").period == "year"
    assert posted_pay("Interns earn $8,000/month.").yearly == 96000
    assert posted_pay("We cover a $50 application fee.") is None and posted_pay("No pay listed.") is None
    job = Job(company="PTC", role_title="Intern", description_text="the hourly range is between $32-$36.",
              source="test", fetched_at="now")
    profile = PROFILE.model_copy(deep=True)
    profile.tier2.salary_from_posting = True
    assert one(q("What are your annual salary expectations?", "number"), job=job, profile=profile).value == "71000"
    assert one(q("What are your annual salary expectations?"), job=job, profile=profile).value == "$71,000"
    assert one(q("Expected hourly pay?", "number"), job=job, profile=profile).value == "34"
    unposted = one(q("What are your annual salary expectations?", "number"), profile=profile)
    assert unposted.kind == "unknown"  # no pay in the posting and the box takes numbers: Vatsal decides


def test_ai_screening_and_talent_pool_consents_follow_the_profile() -> None:
    profile = PROFILE.model_copy(deep=True)
    for item in (profile.tier2.ai_screening_consent, profile.tier2.keep_for_other_roles):
        item.auto_answer, item.value = True, "Yes"
    ai = q("PTC uses HiredScore, a Workday AI-assisted recruiting tool, to help ... If you opt-in, HiredScore will help",
           "select", ["I understand and I’m ready for to continue with my application involving AI.",
                      "I understand and I’m ready for to continue without my application involving AI."])
    assert one(ai, profile=profile).value == ai.options[0] and one(ai, profile=profile).kind == "auto"
    pool = q("You may opt in to have your candidate information retained for future or alternative opportunities.",
             "select", ["I understand and I’m ready for my application to be retained for other opportunities.",
                        "I understand and I’m not ready for my application to be retained."])
    assert one(pool, profile=profile).value == pool.options[0]
    assert one(ai).kind == "unknown"  # without the profile setting, Vatsal answers
    assert key_for(q("Do you have experience with AI tools?"))[0] != "ai_consent"


def test_hispanic_or_latino_follows_the_profile() -> None:
    profile = PROFILE.model_copy(deep=True)
    profile.tier2.eeo.auto_answer = True
    profile.tier2.eeo.hispanic_latino = "No"
    yes_no = q("Are you Hispanic or Latino?", "select", ["Yes", "No", "Decline To Self Identify"])
    assert one(yes_no, profile=profile).value == "No"
    worded = q("Hispanic or Latino", "select", ["Hispanic or Latino", "Not Hispanic or Latino", "I don't wish to answer"])
    assert one(worded, profile=profile).value == "Not Hispanic or Latino"
    assert one(yes_no).value == "Decline To Self Identify"  # not set in the profile: the decline default


def test_the_runs_cover_letter_goes_wherever_a_form_asks_for_one() -> None:
    docs = {"cover_letter": "/runs/x/AlexExample_CoverLetter.pdf", "cover_letter_text": "Dear Hiring Team,\n\nHello."}
    upload = one(q("Cover Letter", "file", required=False, name="cover_letter"), documents=docs)
    assert (upload.kind, upload.value, upload.status) == ("file", docs["cover_letter"], "needs_review")
    typed = one(q("Cover letter", "textarea", required=False, name="letter_box"), documents=docs)
    assert (typed.kind, typed.value) == ("auto", docs["cover_letter_text"])
    short_box = one(q("Cover letter", "textarea", name="letter_box"), documents={**docs, "cover_letter_text": "x" * 900})
    assert short_box.kind == "free_text" or short_box.kind == "auto"
    assert one(q("Cover Letter", "file", name="cover_letter")).kind == "unknown"  # no letter in this run


def test_a_note_to_the_hiring_manager_is_drafted_short() -> None:
    from helper.apply import _merge

    note = one(q("Note to Hiring Manager", "textarea", required=False, name="note"))
    assert note.kind == "free_text" and "60 to 90 words" in (note.note or "")
    plan = plan_answers([q("Note to Hiring Manager", "textarea", required=False, name="note")], ctx())
    long_note = "Hi,\n\n" + "word " * 150 + "\n\nThank you,\nVatsal Patel"
    assert "hiring-manager note" in _merge(plan, {"answers": [{"field": "note", "answer": long_note}]})[0]
    good = ("Hi,\n\nI enjoy figuring out why a system behaves the way it does, not just whether it works. My favorite "
            "example is a fix I got merged into Chef that had crashed every resource for three and a half years, hidden "
            "behind a second error that pointed elsewhere.\n\nYour platform team is where I want to grow.\n\n"
            "Thank you,\nVatsal Patel")
    assert _merge(plan, {"answers": [{"field": "note", "answer": good}]}) == []
