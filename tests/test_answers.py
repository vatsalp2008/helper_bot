"""Form answers (spec 10.3, 10.4): what is filled, what is confirmed, what is drafted, what pauses."""

from __future__ import annotations

import json
from datetime import date
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
    assert (connect.kind, connect.value) == ("auto", "Other")  # "Career site" isn't offered: any source (Vatsal, 2026-11-16)
    assert "Why do you want to join Figma?" in {a.label for a in plan.open_items()}  # the source is picked by the rule


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
    # no term named (Nelnet, 2026-11-17): the first day he's free, January 2027 (free_from)
    assert one(q("Date Available to Start:", "date"), season=None, season_year=None,
               today=date(2026, 11, 17)).value == "January 1, 2027"
    assert one(q("Date Available to Start:", "date"), season=None, season_year=None,
               today=date(2027, 2, 10)).value == "February 10, 2027"


def test_gpa_and_cover_letters_are_never_filled() -> None:
    gpa = one(q("Cumulative GPA"))
    assert gpa.kind == "unknown" and gpa.value is None and "rule 3" in (gpa.note or "")  # no grades file
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


def test_status_questions_that_ask_for_a_date_or_details_go_to_vatsal() -> None:  # Akuna, 2026-11-18
    expiry = one(q("If you have a current work authorization/status, when does it expire? (Please enter N/A if you do "
                   "not require work authorization.)", "text"))
    assert expiry.value is None and "date or details" in (expiry.note or "")  # was "Yes"
    detail = one(q("Please provide additional detail about your sponsorship needs, current work authorization status, "
                   "or eligibility for a work authorization status.", "textarea"))
    assert detail.value is None  # was "No"
    # the yes/no questions keep their answers, in a text box too
    assert one(q("Will you now or in the future require visa sponsorship?", "text")).value == "No"
    assert one(q("Are you legally authorized to work in the US without sponsorship?", "text")).value == "Yes"


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
    assert one(q("What are your annual salary expectations?"), job=job, profile=profile).value == "71000"
    # text boxes get the plain number too: iCIMS rejected "$36 per hour" (Cotiviti, 2026-11-15)
    assert one(q("What are your hourly rate expectations for this position?"), job=job, profile=profile).value == "34"
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
    assert (upload.kind, upload.value, upload.status) == ("file", docs["cover_letter"], "ready")  # no review needed
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


def test_privacy_notices_and_terms_are_accepted() -> None:
    notice = one(q("Candidate Notice: Please read and accept the Global Personal Data Privacy Notice for Candidates.",
                   "select", ["Acknowledge/Confirm"]))
    assert (notice.key, notice.kind, notice.value) == ("terms_consent", "auto", "Acknowledge/Confirm")
    box = one(q("I acknowledge that I have read the privacy policy", "boolean", ["I acknowledge"]))
    assert box.value == "Yes"
    assert one(q("Do you have any employment restrictions such as a non-compete?", "select", ["Yes", "No"])).kind == "unknown"


def test_a_non_compete_question_follows_the_profile() -> None:
    profile = PROFILE.model_copy(deep=True)
    profile.tier2.employment_restrictions.auto_answer, profile.tier2.employment_restrictions.value = True, "No"
    question = q("Do you have any employment restrictions such as a non-compete?", "select", ["Yes", "No"])
    assert (one(question, profile=profile).kind, one(question, profile=profile).value) == ("auto", "No")


def test_a_mailing_address_box_gets_the_whole_address() -> None:
    profile = PROFILE.model_copy(deep=True)
    profile.tier1.address.unit = "Apt 1"
    full = one(q("Current Mailing Address"), profile=profile)
    assert full.value == "1 Example Way, Apt 1, Springfield, ST 00000, United States"
    assert one(q("Address Line 1"), profile=profile).key == "address_street"


def test_school_questions_follow_the_profile_and_the_graduation_date() -> None:
    from helper.answers import _school_status

    enrolled = one(q("Are you currently enrolled in a degree program?", "select", ["Yes", "No"]))
    assert (enrolled.kind, enrolled.value) == ("auto", "Yes")
    highest = one(q("What is the highest level of education you are pursuing or have completed?", "select",
                    ["Bachelor's degree", "Master's degree (non-MBA)", "Master's in Business Administration (MBA)"]))
    assert highest.value == "Master's degree (non-MBA)"
    term = one(q("What term did you (or will you) graduate in?", "select",
                 ["Winter (January - March)", "Spring (April - June)", "Summer (July - September)"]))
    assert term.value == "Spring (April - June)"
    status = one(q("Which best describes your status?", "select",
                   ["I’m in my final year of school", "I’m in my second to last year of school"]))
    assert status.value in ("I’m in my final year of school", "I’m in my second to last year of school")
    assert _school_status("May 2027", date(2026, 11, 15)) == "I'm in my final year of school"
    assert _school_status("May 2027", date(2025, 9, 1)) == "I'm in my second to last year of school"


def test_gpa_goes_on_forms_only_from_the_grades_file() -> None:
    grades = {"Example University": "3.6"}
    gpa = one(q("Cumulative GPA"), grades=grades)
    assert (gpa.kind, gpa.value) == ("auto", "3.6")
    ranged = one(q("What is your GPA?", "select", ["Below 3.0", "3.0 - 3.49", "3.5 - 3.79", "3.8 - 4.0"]), grades=grades)
    assert ranged.value == "3.5 - 3.79"
    assert one(q("What scale is your GPA on?"), grades=grades).kind == "unknown"  # Vatsal answers the scale
    assert one(q("Undergraduate GPA"), grades=grades).kind == "unknown"  # no bachelor's entry in the sample profile


def test_choice_answers_are_remembered_for_later_postings(repo: Path) -> None:
    from helper.answers import PlannedAnswer
    from helper.apply import remember
    from helper.config import Paths, load_yaml

    paths = Paths(repo)
    asked = PlannedAnswer(field="q1", label="Do you have experience with semiconductor equipment?", type="select",
                          required=True, kind="auto", value="No", options=["Yes", "No"], status="needs_review")
    assert remember(paths, [asked]) == 1 and remember(paths, [asked]) == 0  # saved once
    essay = PlannedAnswer(field="q2", label="Why us?", type="textarea", required=True, kind="auto", value="Because.")
    assert remember(paths, [essay]) == 0  # written answers are saved only when Vatsal asks
    approved = AnswersFile.model_validate(load_yaml(paths.answers))
    later = one(q("Do you have experience with semiconductor equipment?", "radio", ["Yes", "No"]), approved=approved)
    assert (later.kind, later.value, later.source) == ("auto", "No", "data/answers.yaml (remembered)")
    unrelated = one(q("Do you have experience with lithography?", "radio", ["Yes", "No"]), approved=approved)
    assert unrelated.kind == "unknown"
    status = PlannedAnswer(field="q3", label="Which best describes your status?", type="select", required=True,
                           kind="auto", value="I’m in an earlier year of school",
                           options=["I’m in my final year of school", "I’m in an earlier year of school"])
    remember(paths, [status])
    approved = AnswersFile.model_validate(load_yaml(paths.answers))
    again = one(q("Which best describes your status?", "select", ["I’m in my final year of school",
                                                                 "I’m in an earlier year of school"]), approved=approved)
    assert again.source != "data/answers.yaml (remembered)"  # helper's own rule (the graduation date) comes first


def test_where_he_would_move_from_is_home() -> None:
    profile = PROFILE.model_copy(deep=True)
    profile.tier1.address.city, profile.tier1.address.state, profile.tier1.address.postal_code = "Seattle", "WA", "98105"
    question = q("Relocation assistance may be available for this role. If you would need to relocate, please tell us "
                 "the city, state, and zip code you’d be moving from")
    answer = one(question, profile=profile)
    assert (answer.key, answer.value) == ("relocate_from", "Seattle, Washington - 98105")
    assert one(q("Are you willing to relocate?", "select", ["Yes", "No"]), profile=profile).key == "relocate"


def test_relatives_at_the_company_is_none_always() -> None:  # Vatsal, 2026-11-15
    profile = PROFILE.model_copy(deep=True)
    profile.tier2.relatives_at_company.auto_answer, profile.tier2.relatives_at_company.value = True, "No"
    written = one(q("Do you have any relatives who are currently or were previously employed by Cotiviti? If yes, "
                    "please provide their names. If NO, please put N/A:", "textarea"), profile=profile)
    assert (written.key, written.kind, written.value) == ("relatives_at_company", "auto", "N/A")
    assert one(q("Are you related to anyone who currently works at Figma?", "select", ["Yes", "No"]),
               profile=profile).value == "No"
    assert one(q("Do you have family members employed by Figma?", "radio", ["Yes", "None"]), profile=profile).value == "None"
    assert one(q("Have you previously been employed by Figma?", "select", ["Yes", "No"]),
               profile=profile).key != "relatives_at_company"
    assert one(q("Are you related to anyone who currently works at Figma?", "select", ["Yes", "No"])).kind == "unknown"


def test_start_questions_with_choices_follow_the_role() -> None:  # Vatsal, 2026-11-15
    options = ["— Make a Selection —", "I'm available to start now", "I'm looking for an internship that starts in "
               "the summer", "I'm looking for an internship that starts in the fall"]
    question = q("We are looking to fill this internship role immediately. When would you be able to start?", "select",
                 options)
    assert one(question).value == "I'm available to start now"  # an internship: free from January
    full_time = one(q("When could you start?", "select", ["Immediately", "Summer 2027 (May)", "Fall 2027"]),
                    employment="full_time")
    assert full_time.value == "Summer 2027 (May)"


def test_icims_address_2_gets_the_unit_and_line_1_the_street() -> None:  # Cotiviti kept a stale "Address 2"
    profile = PROFILE.model_copy(deep=True)
    profile.tier1.address.unit = "Apt 1"
    answers = plan_answers([q("Address", name="a1"), q("Address 2", required=False, name="a2")], ctx(profile=profile)).answers
    assert [(a.key, a.value) for a in answers] == [("address_street", profile.tier1.address.street),
                                                   ("address_line2", "Apt 1")]


def test_sponsorship_wins_over_the_words_work_authorization() -> None:  # Fanatics, 2026-11-15: it said "Yes"
    future = one(q("Will you in future require visa sponsorship to maintain work authorization?", "select", ["Yes", "No"]))
    assert (future.key, future.value) == ("sponsorship", "No")
    without = one(q("Are you legally authorized to work in the United States without the need for sponsorship?",
                    "select", ["Yes", "No"]))
    assert (without.key, without.value) == ("work_authorization", "Yes")


def test_a_closer_option_beats_an_alias_and_years_must_agree() -> None:
    from helper.answers import match_option

    options = ["East Asian", "South Asian", "Southeast Asian", "White"]  # Affirm, 2026-11-15: it chose "East Asian"
    assert match_option("Asian (South Asian)", options, aliases=("Asian",))[0] == "South Asian"
    assert match_option("Asian (South Asian)", ["White", "Asian"], aliases=("Asian",))[0] == "Asian"
    assert match_option("May 2027", ["Spring 2028", "Fall 2027"])[0] in (None, "Fall 2027")  # never another year
    grad = one(q("What is your anticipated graduation year?", "select",
                 ["Fall/Winter 2027", "Spring 2028", "Fall/Winter 2028", "Other"]))
    assert grad.value == "Other"  # Fanatics: May 2027 isn't offered, and "Spring" mustn't land on 2028
    assert one(q("Expected graduation", "select", ["Spring 2027", "Fall 2027", "Spring 2028"])).value == "Spring 2027"


def test_commuting_and_on_site_questions_are_always_yes() -> None:  # Vatsal, 2026-11-15
    for label in ("Are you willing and able to commute to our Cambridge office three times per week?",
                  "This internship is designated as on-site and requires working from the office five days per week. "
                  "Can you meet this requirement?",
                  "Please confirm you are willing to be on-site for the duration of the internship in San Jose, CA."):
        answer = one(q(label, "select", ["Yes", "No"]))
        assert (answer.key, answer.kind, answer.value) == ("onsite_ok", "auto", "Yes"), label
    assert one(q("Do you require remote work?", "select", ["Yes", "No"])).key != "onsite_ok"


def test_a_list_of_immigration_statuses_gets_none() -> None:  # Muon Space, 2026-11-15
    profile = PROFILE.model_copy(deep=True)
    profile.tier2.us_citizen_or_permanent_resident.auto_answer = True
    profile.tier2.us_citizen_or_permanent_resident.value = "No"
    status = one(q("U.S. Export Control laws require that access to certain technical information be limited to U.S. "
                   "Persons. Please indicate your current Immigration and Work Authorization status.", "select",
                   ["US Citizen", "Lawful Permanent Resident", "Protected Person", "None"]), profile=profile)
    assert (status.key, status.value) == ("us_citizen", "None")
    assert one(q("Are you a U.S. person?", "select", ["Yes", "No"]), profile=profile).value == "No"
    visas = one(q("What is your current work authorization status?", "select", ["US Citizen", "F-1 Student Visa"]),
                profile=profile)
    assert visas.value is None  # no "None" to pick: which visa is Vatsal's call


def test_survey_wording_variants_reach_the_right_eeo_rule() -> None:  # SharkNinja, 2026-11-15
    race = one(q("How would you describe your racial/ethnic background? (mark all that apply)", "select",
                 ["East Asian", "Hispanic, Latinx or of Spanish Origin", "South Asian", "White or European"]))
    assert race.key == "eeo_race"
    disability = one(q("Do you have a disability or chronic condition that substantially limits one or more of your "
                       "major life activities?", "select", ["Yes", "No", "I don't wish to answer"]))
    assert disability.key == "eeo_disability"
    upload = one(q("Please attach any additional documents here (if you have multiple files please .zip them)", "file",
                   required=False))
    assert upload.key != "address_zip"  # Riot: an upload is never a zip code


def test_t_mobile_in_a_question_is_not_a_phone_number() -> None:  # T-Mobile, 2026-11-16
    question = q("Have you previously or are you currently working as a contractor for T-Mobile?", "select", ["Yes", "No"])
    assert one(question).key != "phone"
    assert one(q("Mobile phone number")).key == "phone"


def test_questions_about_past_employers_follow_the_master_list() -> None:  # T-Mobile, 2026-11-16
    for label in ("Have you been employed or are you currently employed with an authorized dealer of T-Mobile or "
                  "Metro by T-Mobile?",
                  "Have you previously or are you currently working as a contractor for T-Mobile?",
                  "Have you previously been DIRECTLY employed with Deutsche Telekom AG or Softbank?"):
        answer = one(q(label, "select", ["Yes", "No"]))
        assert (answer.key, answer.kind, answer.value) == ("previous_employee", "auto", "No"), label
    kalini = one(q("Have you ever worked for Kalini Industries?", "select", ["Yes", "No"]))
    assert kalini.value is None  # an employer in the master list: Vatsal answers
    age = one(q("In order to be employed at T-Mobile, you must be at least 18 years of age. Can you meet this "
                "requirement?", "select", ["Yes", "No"]))
    assert age.key == "age_18"


def test_a_plain_upload_box_is_the_resume() -> None:  # ASML's Workday, 2026-11-16
    assert one(q("Upload a file (5MB max)", "file")).key == "resume"
    attachments = one(q("Upload a file (5MB max)", "file", required=False, description="Additional Attachments section"))
    assert attachments.key != "resume" and attachments.kind == "skip"  # Camping World, 2026-11-16
    assert one(q("Please attach any additional documents here", "file", required=False)).key != "resume"


def test_country_of_citizenship_comes_from_the_profile() -> None:  # ASML, 2026-11-16
    profile = PROFILE.model_copy(deep=True)
    profile.tier1.citizenship, profile.tier1.citizenship_since = "Freedonia", "2001"
    text = one(q("Please provide country of citizenship and/or permanent residency and the year granted.", "textarea"),
               profile=profile)
    assert (text.key, text.value) == ("citizenship_country", "Freedonia, since 2001 (by birth)")
    assert one(q("Country of Citizenship", "select", ["Freedonia", "Sylvania"]), profile=profile).value == "Freedonia"
    assert one(q("Country of Citizenship", "select", ["Freedonia", "Sylvania"])).kind == "unknown"  # not set: ask
    # Marvell, 2026-11-18: a country picker, not the U.S.-status question ("... permanent resident status")
    marvell = one(q("Please select all countries in which you currently hold citizenship or have an active permanent "
                    "resident status:", "multi_select", ["Freedonia", "Sylvania"]), profile=profile)
    assert (marvell.key, marvell.value) == ("citizenship_country", "Freedonia")
    status = one(q("What is your current citizenship or immigration status?", "select",
                   ["U.S. Citizen", "Permanent Resident", "None"]), profile=profile)
    assert status.key == "us_citizen"


def test_military_service_questions_are_no() -> None:  # Vatsal, 2026-11-16
    spouse = one(q("Has your spouse or domestic partner ever served in the U.S. Military?", "select",
                   ["Yes", "No", "I do not wish to answer"]))
    assert (spouse.key, spouse.value) == ("military_service", "No")
    rocket = one(q("Have you or your spouse ever been affiliated with any branch of the U.S. or Canadian Armed Forces?",
                   "select", ["Yes", "No", "Prefer not to answer"]))
    assert rocket.value == "No"
    tmobile = one(q("Please take the time to self-identify your Veterans status with the following questions. Do you "
                    "currently or have you ever served in the U.S. Military?", "select",
                    ["Yes, I am either currently serving or have served", "No, I have never served in the U.S. Armed "
                     "Forces, reserves or national guard", "I do not wish to answer"]))
    assert tmobile.value.startswith("No, I have never served")
    assert one(q("Veteran Status", "select", ["I am not a protected veteran", "I identify as a protected veteran"]),
               ).key == "eeo_veteran"


def test_confirm_you_have_read_notices_are_accepted() -> None:  # Shopify, 2026-11-16
    for label in ("Please confirm you've read and agree with our candidate NDA",
                  "Please confirm you've read our applicant privacy notice"):
        answer = one(q(label, "boolean", [label]))
        assert (answer.key, answer.kind) == ("terms_consent", "auto"), label


def test_internship_counts_full_time_starts_and_unlisted_sources() -> None:  # Shopify, 2026-11-16
    count = one(q("How many paid Data (or Data related) internships will you have completed after high school and "
                  "prior to May 31, 2027 *", "radio", ["0", "1", "2", "3", "4+"]), internships_done=2)
    assert (count.key, count.value) == ("internship_count", "2")
    profile = PROFILE.model_copy(deep=True)
    profile.tier2.earliest_start_date.full_time = "May 2027"
    later = one(q("Would you be able to begin full time permanent employment in September 2027?", "radio",
                  ["Yes", "No"]), profile=profile)
    assert (later.key, later.value) == ("full_time_start", "Yes")
    earlier = one(q("Could you start full-time work in January 2027?", "select", ["Yes", "No"]), profile=profile)
    assert earlier.value == "No"
    social = one(q("How did you hear about us?", "select", ["Select...", "Referral", "Social Media", "University event"]))
    assert social.value == "Social Media"
    anything = one(q("How did you hear about us?", "select", ["Employee Referral", "University event"]))
    assert anything.value == "University event"  # never claims a referral


def test_a_date_box_under_a_consent_text_gets_todays_date() -> None:  # Kensho/HiredScore, 2026-11-16
    label = ("Candidate Consent: Use of AI-Assisted Screening Tool (HiredScore) S&P Global requests your consent to use "
             "HiredScore. Enter today’s date below to confirm that you have read and understand the above information.")
    a = one(q(label, "date"))
    assert a.key == "signature_date" and a.value == date.today().strftime("%m/%d/%Y")
    assert key_for(q("Do you consent to the use of AI-assisted screening (HiredScore)?", "select", ["Yes", "No"]))[0] \
        == "ai_consent"
    assert key_for(q("I consent to HiredScore reviewing my application", "date"))[0] != "ai_consent"


def test_a_list_of_countries_is_checked_against_the_profile_citizenship() -> None:  # Analog Devices, 2026-11-16
    profile = PROFILE.model_copy(deep=True)
    profile.tier1.citizenship = "Freedonia"
    group_d = q("Are you a citizen of any of the following countries: Group D (Armenia, Azerbaijan, Belarus, China (PRC), "
                "Russia, Vietnam)", "select", ["Yes", "No"])
    assert key_for(group_d)[0] == "citizen_of_listed" and one(group_d, profile=profile).value == "No"
    listed = q("Are you a citizen of any one of these countries: Cuba, Freedonia, Iran?", "select", ["Yes", "No"])
    assert one(listed, profile=profile).value == "Yes"


def test_a_date_box_only_takes_a_date() -> None:  # Analog Devices, 2026-11-16: "Master of Science" became "/01/"
    done = q("When do you expect to complete your current degree program?", "date")
    assert key_for(done)[0] == "graduation_date"
    assert key_for(q("What degree are you pursuing? Enter the date you started.", "date"))[0] != "degree"
    assert key_for(q("When do you expect to complete your current degree program?", "select", ["Yes", "No"]))[0] \
        == "graduation_date"


def test_counts_pick_their_range_and_projects_are_not_internships() -> None:  # Direct Supply, 2026-11-16
    from helper.answers import count_option
    ranges = ["0", "1-3", "4-6", "7-9", "10+"]
    assert count_option(26, ranges) == "10+" and count_option(2, ranges) == "1-3" and count_option(0, ranges) == "0"
    assert count_option(6, ["Less than 5", "5 or more"]) == "5 or more"
    projects = q("How many software engineering or AI projects have you completed (personal, academic, or internship "
                 "based)?", "select", ranges)
    assert key_for(projects)[0] == "project_count"
    assert one(projects, projects_done=26).value == "10+"
    interns = q("How many paid internships will you have completed?", "select", ranges)
    assert one(interns, internships_done=2).value == "1-3"


def test_two_digit_year_terms() -> None:  # Direct Supply, 2026-11-16: "Winter '27" is December 2027
    terms = ["Winter '26", "Spring '27", "Summer '27", "Winter '27", "Spring '28", "Graduated"]
    grad = q("What is your anticipated graduation date?", "select", terms)
    assert one(grad, grad_date="December 2027").value == "Winter '27"
    assert one(grad, grad_date="May 2027").value == "Spring '27"


def test_returning_to_school_follows_the_graduation_date() -> None:  # T-Mobile, 2026-11-16
    back = q("To be eligible, students must plan to return to school. Do you intend to return to a college or university "
             "for at least one additional semester or quarter after the internship ends?", "select", ["Yes", "No"])
    assert key_for(back)[0] == "returning_to_school"
    summer = dict(employment="internship", season="summer", season_year=2027)
    assert one(back, grad_date="December 2027", **summer).value == "Yes"
    assert one(back, grad_date="May 2027", **summer).value == "No"


def test_unlabelled_upload_boxes_get_the_resume_then_the_letter() -> None:  # Laborie on Jobvite, 2026-11-16
    boxes = [q("File", "file", required=False, name="file-0"), q("File", "file", required=False, name="file-1")]
    plan = plan_answers(boxes, ctx()).answers
    assert plan[0].key == "resume" and plan[0].kind == "file"
    assert plan[1].key == "cover_letter"
    labelled = [q("Resume/CV", "file", name="resume"), q("File", "file", required=False, name="file-1")]
    plan = plan_answers(labelled, ctx()).answers
    assert plan[0].key == "resume" and plan[1].key is None  # a named resume box: the extra box stays empty


def test_company_sites_get_a_us_phone_without_the_plus() -> None:  # Boston Scientific, 2026-11-17
    from helper.models import Job
    phone = q("Phone number", "text")
    site = Job(company="Acme", role_title="Intern", description_text="x", source="test", fetched_at="now", ats="eightfold")
    board = Job(company="Acme", role_title="Intern", description_text="x", source="test", fetched_at="now", ats="greenhouse")
    profile = PROFILE.model_copy(deep=True)
    profile.tier1.phone = "+15550100000"
    assert one(phone, job=site, profile=profile).value == "555-010-0000"
    assert one(phone, job=board, profile=profile).value == "+15550100000"


def test_rules_from_the_first_job_hunt_night() -> None:  # 2026-11-17
    legally = q("Can you legally work in the US now or anytime in the future without sponsorship?", "select",
                ["Yes, I can work WITHOUT sponsorship now and in the FUTURE", "No, I will need sponsorship now or in the future"])
    assert key_for(legally)[0] == "work_authorization" and one(legally).value.startswith("Yes")
    na = q("What is your current visa status? Choose Not Applicable if you do not require sponsorship.", "text")
    assert one(na).value == "Not Applicable"
    put_na = q("If you require sponsorship to work in the United States, please indicate the type of visa you currently "
               "hold. If you do not require a visa please put NA.", "textarea")
    assert one(put_na).value == "NA"
    plain = q("What is your current visa status?", "select", ["F-1", "H-1B", "Other"])
    assert one(plain).value is None  # a visa type: Vatsal answers it
    permanent = q("Do you have legal authority to work permanently in the United States?", "select", ["Yes", "No"])
    assert key_for(permanent)[0] == "permanent_work" and one(permanent).value == "No"
    salary = q("What are your salary expectations? (number only please)", "text")
    assert one(salary).value is None  # "Negotiable" can't go in a numbers-only box: Vatsal decides


def test_remembered_written_answers_never_beat_a_rule() -> None:  # a remembered "Winter '27", 2026-11-17
    remembered = AnswersFile(answers=[ApprovedAnswer(id="grad", question_patterns=[r"anticipated graduation date"],
                                                     answer="Winter '27", approved_on="2026-11-16")])
    grad = q("What is your anticipated graduation date?", "text")
    assert one(grad, approved=remembered, grad_date="December 2027").value == "December 2027"


def test_a_sponsorship_question_that_says_visa_status_stays_a_sponsorship_question() -> None:
    q1 = q("Will you now or in the future require sponsorship for employment visa status?", "select", ["Yes", "No"])
    assert key_for(q1)[0] == "sponsorship" and one(q1).value == "No"


def test_a_hispanic_only_ethnicity_question_and_an_armed_forces_member_question() -> None:  # Soros, 2026-11-17
    profile = PROFILE.model_copy(deep=True)
    profile.tier2.eeo.hispanic_latino = "No"
    ethnicity = one(q("What is your ethnicity? (please check all that apply)", "multi_select",
                      ["Hispanic or Latino", "Not Hispanic or Latino", "I don't wish to answer"]), profile=profile)
    assert ethnicity.key == "eeo_hispanic" and ethnicity.value == "Not Hispanic or Latino"
    race = one(q("What is your race/ethnicity?", "select", ["Asian", "White", "Hispanic or Latino", "Decline"]))
    assert race.key == "eeo_race"
    member = one(q("Are you a veteran or active member of the armed forces?", "select", ["Yes ", "No ", "I don't wish to answer"]))
    assert member.key == "military_service" and member.value.strip() == "No"


def test_the_f1_student_question_follows_the_profile() -> None:  # Boston Scientific, 2026-11-17
    question = q("Are you an F1 student who has or will be applying for CPT, OPT, or STEM OPT?", "radio", ["Yes", "No"])
    assert one(question).kind == "unknown"  # the sample profile doesn't say
    profile = PROFILE.model_copy(deep=True)
    profile.tier2.f1_student.value = "Yes"
    answer = one(question, profile=profile)
    assert (answer.key, answer.value) == ("f1_student", "Yes")
    assert one(q("Are you currently on an F-1 visa?", "select", ["Yes", "No"]), profile=profile).value == "Yes"
    sponsor = one(q("Will you require sponsorship now or in the future (including F-1 OPT)?", "select", ["Yes", "No"]),
                  profile=profile)
    assert sponsor.key == "sponsorship"


def test_permit_ai_processing_questions_opt_in() -> None:  # Labcorp, 2026-11-17: "answer yes always"
    profile = PROFILE.model_copy(deep=True)
    profile.tier2.ai_screening_consent.value = "Yes"
    for label in ("Do you permit Labcorp to process your data using Artificial Intelligence (AI) tools to evaluate you "
                  "against the listed job qualifications of the role? Opting out will not result in any adverse "
                  "employment action.",
                  "Do you permit Labcorp to process your data to consider you for future job openings using Artificial "
                  "Intelligence (AI) tools ? Opting out will not result in any adverse employment action."):
        answer = one(q(label, "select", ["Yes", "No"]), profile=profile)
        assert (answer.key, answer.value) == ("ai_consent", "Yes")
    assert one(q("Are you legally authorized to work in the United States?", "select", ["Yes", "No"]),
               profile=profile).key == "work_authorization"


def test_a_covenant_not_to_compete_is_the_restrictions_question() -> None:  # Little Caesars, 2026-11-17
    profile = PROFILE.model_copy(deep=True)
    profile.tier2.employment_restrictions.value = "No"
    answer = one(q("Are you under any obligation to a current or previous employer, through a covenant not to compete, "
                   "or otherwise restricted in your acceptance of employment with a competitive firm?", "select",
                   ["Yes", "No"]), profile=profile)
    assert (answer.key, answer.value) == ("employment_restrictions", "No")


def test_highest_education_completed_is_the_finished_degree() -> None:  # Scout Motors, 2026-11-18
    from helper.models import EducationEntry

    profile = PROFILE.model_copy(deep=True)
    profile.education.append(EducationEntry(school="Sample Institute", degree="Bachelor of Engineering",
                                            major="Computer Engineering", location="Sample City", start="August 2021",
                                            end="May 2025"))
    options = ["High school or equivalent", "Bachelor's degree or international equivalent",
               "Master's degree or international equivalent", "Doctorate"]
    done = one(q("What is the highest level of education you have completed?", "select", options), profile=profile)
    assert (done.key, done.value) == ("degree_completed", "Bachelor's degree or international equivalent")
    assert one(q("Highest level of education", "select", options), profile=profile).key == "degree"


def test_a_phone_type_list_without_mobile_gets_personal_primary() -> None:  # BMO, 2026-11-18
    answer = one(q("Phone Device Type", "select", ["Business Primary", "Business Secondary", "Personal Primary",
                                                    "Personal Secondary"]))
    assert answer.value == "Personal Primary"
    assert one(q("Phone Device Type", "select", ["Home", "Mobile", "Work"])).value == "Mobile"


def test_the_county_comes_from_the_profile_address() -> None:  # Great American's Workday, 2026-11-18
    county = one(q("County", "text", name="workday:regionSubdivision1"))
    assert (county.key, county.value) == ("address_county", "Example County")
    assert one(q("County", "text")).key == "address_county"
    assert one(q("Country", "select", ["United States", "Canada"])).key == "address_country"


def test_a_yes_no_question_naming_the_resume_is_not_the_upload() -> None:  # Scout, 2026-11-18
    confirm = one(q("I confirm that the information I have provided in my application and resume is true, complete, "
                    "and accurate to the best of my knowledge.", "select", ["Yes", "No"]))
    assert confirm.kind != "file" and confirm.key != "resume"
    assert one(q("Resume/CV", "file")).key == "resume"
