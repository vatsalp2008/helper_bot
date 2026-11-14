# helper: Job Application Automation Bot (Build Spec v2 for Claude Code)

You are Claude Code. Build a local tool called **helper** in this repository. Read this whole file before writing any code. Then follow section 12 ("Before you start"), and only after I answer, build in the order given in section 13.

---

## 1. Goal

I apply to many jobs and internships. My manual process takes 2 to 5 minutes per application:

1. Find the job posting.
2. Paste the job description to Claude and get a tailored resume.
3. Go back to the posting, fill every field, upload the resume, submit.
4. Log the application in my Notion tracker (company, role, date, resume used, etc.).

helper should do steps 2 to 4 from one input: **a job posting URL**.

```
helper apply <job_url>
```

Result: a tailored one-page PDF resume, a filled application form, (optionally) a submitted application, and a logged record in SQLite mirrored to Notion.

---

## 2. Environment and hard constraints

- I have **no Anthropic API key**. I use Claude Code under my Northeastern University Claude Enterprise seat.
- **Do not use the Anthropic API or SDK.** Do not call `claude -p` or any Claude binary from helper's code, background jobs, or scheduled tasks.
- **You (Claude Code, in this interactive session) are the reasoning engine.** Anything that needs judgment (reading a job description, picking projects, writing bullets, drafting an answer to a free text question) is done by you in the session, and you write the result to a JSON file that helper's deterministic code reads and validates. Design every "AI step" as: helper writes an input file, you produce an output file, helper validates and uses it.
- Everything else is **plain deterministic Python** that works with no AI: Playwright (browser), SQLite (storage), Notion REST API with an internal integration token (tracker), LaTeX (PDF).
- Local machine only. Python 3.11+, `playwright`, `pyyaml`, `pydantic`, `requests`, `typer`, `pytest`. Keep dependencies minimal.
- Secrets (Notion token, database ID) go in `.env`, git-ignored. Provide `.env.example`.
- If a Claude feature is blocked on my seat (Claude in Chrome, connectors, etc.), do not work around it. Tell me and use plain Playwright.

---

## 3. Source of truth

**`data/master_list_v23.md` is the single source of truth about me.** I have confirmed everything in it. That means:

- Every project, experience entry, technology, number, link, date, and status in it is a claim I stand behind. Do not flag, question, or "verify" anything in it. Do not second guess it.
- Variants and framings described in it are all legitimate (NutriTrack as React, Angular/TypeScript, or Vue.js + Django + GitLab CI; E-commerce Platform on Docker or Kubernetes; PostgreSQL or MySQL, DynamoDB or MongoDB per the "Database framing" rule; Seattle Transit with C++ plus Rust; PulseTrader with TimescaleDB plus BigQuery; QuickBooks, Stripe, GraphQL, and the other additions). Choose per job posting.
- **Anything not in the master list is not claimable.** No new tools, numbers, employers, courses, or responsibilities.
- If the master list and any other note disagree, **the master list wins.**
- The master list stays in the repo as the original markdown. A converter (section 5) builds structured YAML from it for validation. When I update the markdown, re-run `helper convert` to refresh the YAML.

---

## 4. Non-negotiable rules

Enforce these in code, not only in instructions.

1. **No invented facts.** Every resume bullet, skill, title, date, and number must trace to the master list.
2. **Metrics are real.** Every metric comes from the master list. If a bullet has no real metric, describe the result qualitatively or with a real count, and flag it to me. Never estimate or invent.
3. **No GPA on resumes.** There is no GPA field in the template or any data file used for resumes. If an application form has a required GPA field, pause and ask me.
4. **No em dashes** anywhere in resumes, answers, or generated text. Use commas or periods.
5. **No summary section** on resumes.
6. **Never solve or bypass CAPTCHAs.** Stop and hand control to me in the open browser window.
7. **Never automate LinkedIn Easy Apply or Handshake.** Tell me to find the company's own careers page, and offer to extract the job description only.
8. **Never create accounts** on employer portals (Workday, iCIMS, etc.). Stop and tell me.
9. **Stop when unsure.** A required field helper cannot fill with high confidence pauses the run.
10. **Never retry a submit.** An ambiguous result is logged as `submitted_unverified`.
11. **Eligibility is checked before building** (section 8).
12. **Do not re-ask for a job description I already gave you.** If I paste one in the session or pass `--jd-file`, use it.

---

## 5. Repository layout and data files

```
helper/
  README.md
  CLAUDE.md                  # short rules file for future sessions (copy sections 3 and 4 into it)
  pyproject.toml
  .env.example
  config.yaml
  data/
    master_list_v23.md       # source of truth (I will provide)
    master.yaml              # generated by `helper convert`, do not hand edit
    profile.yaml             # identity and standing answers (5.2)
    courses.yaml             # courses I have actually taken (5.3)
    answers.yaml             # approved free text answers (5.4)
    project_sets.yaml        # default project sets by role type (7.2)
    caps.yaml                # application caps per company (8.3)
  templates/
    resume.tex.j2            # LaTeX template (see section 6)
    reference_resume.tex     # a real resume of mine, used as the layout reference
  src/helper/
    cli.py
    convert.py               # master_list markdown -> master.yaml
    intake.py                # URL -> ATS detection, job description, question list
    eligibility.py
    keywords.py
    tailor.py                # writes tailor_input.json, validates tailor_output.json
    render.py                # tailor_output.json -> .tex -> PDF
    adapters/ (base.py, greenhouse.py, lever.py, ashby.py, generic.py)
    fill.py
    matcher.py
    tracker/ (db.py, notion_sync.py, export.py)
  runs/                      # one folder per application, git-ignored
  resumes/                   # final .tex and PDF files
  tests/ (fixtures/ for HTML of real forms and sample tailor outputs)
```

`config.yaml`:

```yaml
projects_per_resume: 5
bullets_per_item: 2             # per project and per experience entry
submit_mode: review             # review | auto
auto_submit_min_clean_runs: 5
default_grad_date: "May 2027"
alt_grad_date: "December 2027"
browser_profile_dir: ~/.helper/browser-profile
headless: false
```

### 5.1 `helper convert` (master list to master.yaml)

Parse `master_list_v23.md` into `data/master.yaml`. For each project (26) and experience entry (6) capture: `id`, `name`, `timeline`, `status` (as written, e.g. Complete, Merged, In Progress), `github_url`, `technologies`, every description bullet verbatim, every metric string verbatim, any variant or "Can also be described as" note, and any "Always describe as" or "Must always be described" wording note. Also capture the full Skills and Technologies section (grouped), the "Database framing" rule, and the language list.

Rules for the converter:
- Keep original wording verbatim. Do not rewrite or summarize.
- Extract every number and every technology token per item into `allowed_numbers` and `allowed_tech` lists. The validator uses these.
- Items with wording rules must keep them as `wording_rules`. Known ones from the master list: spiceai PR #13507 and chef PR #16260 are always "merged"; spiceai issue #12747 is always a bug report that led to the CEO's overhaul, never my implementation; E-commerce Platform is "Java Spring Boot plus one service in Go," never "9 Java services"; the TA role title is always "Graduate Teaching Assistant."
- Show me a summary of what was parsed (counts, any section it could not parse) and wait for my OK.

### 5.2 `data/profile.yaml`

Create it and ask me to fill in what is missing. Never guess a value. Seed these header values, which come from my reference resume: name Vatsal Patel, location "Seattle, WA 98105", phone (206) 280-0258, email vatsalp2008@gmail.com, LinkedIn https://www.linkedin.com/in/vatsalp20/ (label "vatsalp20"), GitHub https://github.com/vatsalp2008 (label "vatsalp2008"), portfolio https://vatsalp2008.github.io/Portfolio/. Ask me only for the street address and anything else forms need.

**Tier 1, auto-fill without asking:** full name, preferred name, email, phone, address, LinkedIn, GitHub, portfolio, current school, degree, major, expected graduation, "how did you hear about us" default, relocation willingness and work-location preferences (only if I set them).

**Tier 2, auto-fill only if I set `auto_answer: true` for that item:** work authorization, sponsorship required (now and in future), earliest start date, salary expectation (default "Negotiable"), EEO and demographic questions (default "Decline to self identify"), background check consent, age 18+, security clearance.

**Tier 3, never auto-fill:** free text questions, new claims about me, anything unmatched. These use the drafting flow in section 10.4.

### 5.3 `data/courses.yaml`

Only courses I have actually taken may appear in Relevant Coursework. Seed it with the five on my reference resume: Statistics, Machine Learning, Database Systems, Algorithms, Data Structures. Ask me to add the rest of my confirmed courses. The tailor picks 5 relevant ones from this list.

### 5.4 `data/answers.yaml`

Approved free text answers I have reused, each with `question_patterns` and `answer`.

---

## 6. Resume template

Use LaTeX. A ready-made Jinja2 template is provided at `templates/resume.tex.j2`, built from my real resume. My original is at `templates/reference_resume.tex` (and `templates/reference_resume.pdf`). **Do not redesign or "improve" the template.** The preamble, macros, spacing, and layout must stay exactly as they are. Only the variable parts change.

**Rendering setup (`render.py`):**
- Use Jinja2 with `block_start_string='\BLOCK{'`, `block_end_string='}'`, `variable_start_string='\VAR{'`, `variable_end_string='}'`, `comment_start_string='\#{'`, `comment_end_string='}'`, `trim_blocks=True`, `lstrip_blocks=True`, autoescape off.
- Filters: `tex` escapes plain text, `bullet` escapes then converts `**phrase**` into `\textbf{phrase}`, `url` escapes only `%` and `#`.
- Template variables: `header` (name, location, phone, email, linkedin_url, linkedin_label, github_url, github_label, portfolio_url), `grad_date_display`, `skills_lines` (label, items), `experience` (title, dates, org, location, bullets), `projects` (title as "Name: Subtitle", url, tech_tags, dates, bullets), `coursework`.
- **Escaping must handle:** `& % $ # _ { } ^ \`. Render `~` (as in "~800ms") as `\textasciitilde{}`, because a bare `~` is a non-breaking space in LaTeX. Render `<` and `>` as `\textless{}` and `\textgreater{}` and a literal `|` as `\textbar{}`, because the template uses the default font encoding where they print wrongly. Replace unicode arrows and special symbols with plain words (for example "to"). Render "R squared" as `R\textsuperscript{2}`. Use `--` for date ranges.
- **Date format:** abbreviate months exactly as in my reference resume: Jan., Feb., Mar., Apr., May, Jun., Jul., Aug., Sept., Oct., Nov., Dec. Ranges read "Jul. 2026 -- Aug. 2026", and "Present" stays "Present". `grad_date_display` is "May 2027" or "Dec. 2027".
- **Verify the template first:** render my reference resume's content through the template and diff the result against `reference_resume.tex`. They should match apart from whitespace. The compiled PDF must visually match `reference_resume.pdf`. Show me the result before building anything else on top.

Standard layout:
- `article` class, 10.5pt, letterpaper, `fontawesome5`, section headers in cyan (RGB 0,150,200) with a rule under each.
- **Header:** name, then "Seattle, WA 98105", then phone, email, LinkedIn, GitHub, Portfolio, each with a Font Awesome icon.
- **Section order:** Education, Technical Skills, Experience, Projects, Relevant Coursework. No summary section.
- **Education:** Northeastern University (MS Computer Science, Khoury College, Seattle) and University of Mumbai (BE Computer Engineering). No GPA. Northeastern runs "Sept. 2025 -- <graduation date>" where the graduation date comes from `config.yaml` (May 2027 default, December 2027 when the posting requires it). University of Mumbai is "Oct. 2021 -- May 2025".
- **Technical Skills:** exactly **3 labeled lines**, with category names adapted to the role type. Every skill listed must exist in the master list's skills section.
- **Experience:** each entry has title, organization, dates, and exactly 2 bullets.
- **Projects:** each project heading is a hyperlinked **bold title** with an external-link icon, then `|` and exactly 3 italic tech tags, with dates right-aligned. Exactly 2 bullets each. Titles and tags must never run past the right margin.
- **Relevant Coursework:** 5 courses separated by `|`, from `courses.yaml`.
- One page. Consistent vertical spacing everywhere (changing one section must not change spacing in another). Standard bullet markers.
- ATS friendliness: simple text, no tables in the body, keywords in plain text and not inside icons or links.

Output names: `resumes/VatsalPatel_Resume_<Company>_<RoleShortName>.tex` and the matching `.pdf`.

---

## 7. Tailoring

### 7.1 Experience selection
- **Graduate Teaching Assistant is always included.** The title is always "Graduate Teaching Assistant, Data Structures and Algorithms in C" (adjust wording only if the master list says otherwise).
- Include **one** Kalini internship by track: **Software Development Intern** for SWE, infra, full-stack, mobile, and automation roles. **Machine Learning Intern** for ML, data science, data, and AI roles.
- Default order: TA first, Kalini second. Put Kalini first when it is the stronger match for the posting.
- Event Assistant, Student Proctor, and BAPS Charities stay off resumes by default. I can add one with `--include-experience <name>` for operations or leadership roles.

### 7.2 Project selection
- Choose exactly `projects_per_resume` (default **5**) projects by verbatim keyword and domain overlap. Override with `--projects N`.
- Prefer the project that genuinely used a named tool over one that only relates conceptually (for example Wardrobe for gRPC, F1 Analytics for Spark).
- Include at least one merged open source project (chef/chef or spiceai/spiceai) when the posting values open source, code review, debugging, unfamiliar codebases, or reliability.
- Follow each project's `Status` as written. For an "In Progress" project, describe ongoing work in present tense where natural.
- You may reword a project's subtitle to match the posting (for example "ExperimentIQ: Hypothesis Testing Platform"), but never the project's facts.
- Seed `data/project_sets.yaml` with these defaults. You choose the final set from the posting, starting from these:

| Role type | Default projects |
|---|---|
| Backend and distributed | PulseTrader, E-commerce Platform, spiceai, ExperimentIQ, chef |
| Full-stack and web | theManager, PNWER, E-commerce Platform, Afterglow, chef |
| Frontend | PNWER, Afterglow, F1 Analytics, theManager, chef |
| iOS | Wardrobe, TransitLens, StreamLine, PNWER, MoodMeals |
| Android | TransitLens, Wardrobe, Afterglow, PNWER, chef |
| Data science and analytics | ExperimentIQ, Traffic Accident, Recommendation Engine, F1 Analytics, plus one of Housing Price, Investment Prediction, EchoCoach, MoodMeals |
| Data engineering | ExperimentIQ, PNWER, PulseTrader, F1 Analytics, plus E-commerce or chef |
| ML engineering | Traffic Accident, EchoCoach, Investment Prediction, PerceptNet, PulseTrader |
| Applied AI and agents | theManager, SWARM, AI Agent Discovery, EchoCoach, plus Afterglow or Recommendation Engine |
| Inference and ML platform | EchoCoach, PerceptNet, ScenarioForge, SWARM, TransitLens |
| Systems, quant, low latency | PulseTrader, chef, Seattle Transit, StreamLine, spiceai |
| Cloud, DevOps, IT | E-commerce Platform, SWARM, chef, PNWER, spiceai |
| Robotics and autonomous vehicles | PerceptNet, ScenarioForge, plus TransitLens or Afterglow |
| Games and graphics | Afterglow, PerceptNet, EchoCoach, ScenarioForge, Intelligent Chess AI |
| Supply chain and logistics | PNWER, Seattle Transit, ExperimentIQ, theManager, Traffic Accident |
| Consumer product | MoodMeals, ExperimentIQ, Recommendation Engine, Wardrobe, TransitLens |
| Governance, audit, data ops | PNWER, PulseTrader, ExperimentIQ, Housing Price, F1 Analytics |

### 7.3 Tech swaps and framing
- All tech additions in the master list are additive: old and new tech are both true. Swap in or out per the job description.
- Match PostgreSQL vs MySQL and DynamoDB vs MongoDB to whichever the posting names, per the master list's "Database framing" rule.
- Choose the NutriTrack variant (React, Angular/TypeScript, Vue.js + Django + GitLab CI) that matches the posting's stack.
- C# and PowerShell may appear in Technical Skills even without a project tie.

### 7.4 Bullet writing
Every bullet uses the **XYZ formula**:
- **X (accomplishment):** the result, win, or improvement delivered.
- **Y (measurement):** the real number, scale, or percentage that proves it.
- **Z (action):** the specific skills, tools, or steps used.

Style rules:
- Start with a strong past-tense verb (Built, Cut, Merged, Benchmarked, Automated, Designed, Root-caused). For "In Progress" work, present tense is fine.
- One to two rendered lines. Exactly 2 bullets per project and per experience entry. Bullet 1: what was built and the headline metric. Bullet 2: how it was made reliable, fast, or tested, or the second-strongest result.
- Show, do not tell. No filler phrases that look like keyword stuffing. Weave the posting's keywords in naturally.
- TA bullets must read as teaching (office hours, explaining concepts, debugging with students, grading with consistent rubrics, refining assignments with the instructor).
- Bold only metrics and core technologies or posting keywords, **2 to 4 bolded phrases per bullet at most**.
- No em dashes.

Metric precision rules:
- Keep the source's precision markers: "~" approximate, "+" lower bound, "p<0.01" significance.
- Do not join two numbers that were measured separately into one claim. (Example: Kalini accuracy and inference speedup are separate results. PerceptNet's ~77% mAP is not an after-INT8 number.)
- Label simulated data as simulated (ExperimentIQ's 10M+ events). Call PulseTrader's data "trade events."
- Report borderline results as borderline (EchoCoach tone, p=0.06).
- Use the master list's exact numbers.
- Follow `wording_rules` captured by the converter, including: chef/chef is "merged"; spiceai #12747 is a bug report that led to the CEO's overhaul and never my implementation; E-commerce Platform is Java Spring Boot plus one service in Go.

### 7.5 Keyword audit (every build)
1. **Extract** terms from five places in the posting: required qualifications, preferred qualifications, responsibilities (as action phrases), every named tool, language, framework and platform, and domain words.
2. **Sort** each term into: (a) true and shown in a bullet, (b) true but only in Technical Skills, (c) not true, which is a gap.
3. **Match verbatim.** Use the posting's exact wording in bullets, project subtitles, and the 3 skills lines. Include both acronym and expansion where the posting uses both, otherwise match the posting's form.
4. **Never force a gap in.** Anything not in the master list stays off the resume.
5. **Report** two lines: "Matched verbatim:" and "Genuine gaps not forced in:" (or "No meaningful gaps").
6. In the closing note, suggest one honest way to close a notable gap (for example a free trial or small project).

### 7.6 `tailor_output.json` schema

```json
{
  "role_type": "string",
  "grad_date": "May 2027 | December 2027",
  "experience": [
    {"entry_id": "string", "title": "string", "bullets": ["string", "string"]}
  ],
  "projects": [
    {"project_id": "string", "subtitle": "string", "tech_tags": ["a","b","c"], "bullets": ["string","string"]}
  ],
  "skills_lines": [
    {"label": "string", "items": ["strings from master skills"]}
  ],
  "coursework": ["five entries from courses.yaml"],
  "keyword_audit": {"matched": ["..."], "gaps": ["..."], "skills_only": ["..."]},
  "flags": ["anything I should look at"]
}
```

Bold in bullet strings is written as `**phrase**` and converted to `\textbf{}` at render time.

### 7.7 Validation (enforced in code)

Reject `tailor_output.json` if:
- any `entry_id` or `project_id` is not in `master.yaml`
- project count differs from `projects_per_resume`, or any item does not have exactly `bullets_per_item` bullets
- any number in a bullet is not in that item's `allowed_numbers` (allow harmless formatting such as commas, "~", "+", "%")
- any technology in a bullet, tag, or skills line is not in that item's `allowed_tech` or in the master skills section
- a `wording_rules` violation is detected (for example "pending" near chef, "implemented" near spiceai #12747, "9 Java")
- a bullet has no number and is not marked qualitative in `flags`
- a bullet contains an em dash, the resume contains a GPA, or a summary section exists
- a bullet has more than 4 bold phrases
- `skills_lines` does not contain exactly 3 lines, or `tech_tags` does not contain exactly 3 tags per project
- `coursework` has fewer or more than 5 entries, or any entry is not in `courses.yaml`
- the TA entry is missing or its title does not start with "Graduate Teaching Assistant"
- the compiled PDF is not exactly 1 page, or LaTeX reports an overfull horizontal box in a heading or tag line

On rejection, show me the reason, fix the output, and re-validate. Do not continue until it passes.

---

## 8. Eligibility check (before building)

`eligibility.py` extracts these facts from the posting and flags mismatches. Show me flags before any resume is built.

1. **Work authorization wording**, in three levels:
   - *Explicit exclusion:* the posting names F-1 or OPT as ineligible. Recommend skipping and ask before building.
   - *Ambiguous no-sponsorship:* phrases like "authorized to work without sponsorship" or "now and in the future." Flag, recommend confirming with recruiting, and build.
   - *Export control:* may require government authorization. Flag and build.
2. **Degree level.** Undergraduate-only or Bachelor's-only postings are likely screen-outs: flag. A signal that grad students are considered (such as a pay table) is a positive: note it.
3. **Timing.** Spring or winter roles requiring full-time on-site work conflict with my Seattle classes: flag the CPT hours question. Immediate-start full-time roles are checked against my December 2026 target.
4. **Graduation window.** Use May 2027 by default. Use December 2027 when the posting's window or a "returning to school" rule requires it. Use May 2027 when a full-time start date requires it.
5. **Location.** Flag roles requiring living near a far-off office during the semester.
6. **Deadlines and urgency.** Note them.
7. **Company caps** (section 8.3).

Decision: explicit or physical blockers (a named F-1 exclusion, an impossible commute) mean recommend skipping and ask me. Everything else is flagged and built. I usually say "build."

### 8.3 `data/caps.yaml`

Seed with: Salesforce at most 3 roles in 12 months, Samsung 10 per 6 months, Databricks 1 per 30 days. Qualcomm (Eightfold visibility setting) and Google (questionnaire UniqueID matching) are notes only. Check SQLite before each run, warn me when I am near a cap, and when I have several applications at one company note the count and rank the strongest fits.

---

## 9. Per-application workflow

Each step writes into `runs/<run_id>/`.

1. **Duplicate check** by URL, job ID, and company plus title.
2. **Intake.** Detect the ATS, open the posting (headed Playwright, persistent profile), extract company, role, location, job description, and form questions. Prefer public job data for Greenhouse, Lever, and Ashby, then confirm in the browser. Save `job.json`.
3. **Eligibility check** (section 8). Stop and ask if blocked.
4. **Keyword extraction** (deterministic first pass) into `keywords.json`.
5. **Tailoring.** Write `tailor_input.json` (job description, keywords, master.yaml, config, rules from section 7). **You then read it and produce `tailor_output.json`.**
6. **Validate and render.** Run section 7.7. On pass, fill the template, compile the PDF, check page count, save to `resumes/` and the run folder, store the PDF hash.
7. **Pre-build message to me,** in this order: eligibility flags; one paragraph of fit; keyword audit (matched verbatim, genuine gaps); track plus all 5 projects with a short reason each.
8. **Fill the form** (section 10).
9. **Submit.** `review` mode: stop on the final page and wait for me. `auto` mode: submit only if section 10.5 conditions hold.
10. **Confirm** via confirmation page or message, screenshot into the run folder. Ambiguous means `submitted_unverified`.
11. **Log** to SQLite, then Notion, then CSV. Notion failure keeps the SQLite row and marks `notion_pending`.
12. **Closing note to me:** what changed during validation, the one or two gaps worth preparing for in interviews, and a way to close a gap honestly.

---

## 10. Form filling

### 10.1 Browser
Playwright with a persistent profile at `browser_profile_dir`, headed by default, separate from my everyday Chrome. Small human-like pauses. No stealth plugins or detection evasion. Never click outside the application form.

### 10.2 Adapters
Full adapters for **Greenhouse, Lever, and Ashby** using semantic locators (labels, roles, names). Save an HTML fixture of a real form for each and test against it. `generic.py` is best effort, always pauses before submit, and never runs in auto mode. For Workday, iCIMS, SmartRecruiters and similar: detect them, tell me they need manual or assisted handling, and stop. No account creation.

### 10.3 Field matching
Map each field label (plus placeholder and nearby text) to a profile key with a confidence from 0 to 1. Fill automatically only above a high threshold, ask between thresholds, treat as unknown below. For dropdowns, radios, and checkboxes choose the best match to my stored value and log the exact option. Upload the tailored PDF. Add a cover letter only if required and I approved text.

### 10.4 Free text questions
1. Check `answers.yaml` for an approved match.
2. Otherwise write the question to `runs/<id>/questions_needed.json`. **You draft the answer** using only master list facts and the job description.
3. Style: short to medium unless I say otherwise, relevant to the specific role, natural conversational tone with no heavy words, honest, no em dashes. "About yourself" answers include who I am personally, not only my work. When describing a project, give a brief background before the story. Respect every character or word limit and report the character count when there is one.
4. Mark `needs_review`, show me, wait for approval, then offer to save to `answers.yaml`.

### 10.5 Auto-submit conditions (only when `submit_mode: auto`)
Submit only if all are true: this ATS has at least `auto_submit_min_clean_runs` successful review-mode runs; every required field was filled from Tier 1, an enabled Tier 2 item, or an approved answer; no answer is `needs_review`; no eligibility flag, validation failure, or duplicate warning; no CAPTCHA or verification step; the final page shows no validation errors. Otherwise fall back to `review` behavior and tell me why.

---

## 11. Tracking

SQLite is the system of record. Table `applications`: id, company, role_title, location, job_url, job_id, ats, date_applied, status (`drafted`, `filled`, `applied`, `submitted_unverified`, `skipped`, `failed`), resume_path, resume_hash, projects_used (JSON), keyword_match_pct, confirmation_screenshot, notes, flags, notion_page_id, notion_synced.

**Notion:** use the REST API with the internal integration token from `.env`. On first setup ask for the database ID, **read its schema through the API, and map my existing column names** instead of creating new ones. Ask before creating any column. Provide `helper sync-notion` to retry unsynced rows.

Commands: `helper list`, `show <id>`, `stats` (per week, by ATS, by status), `export --csv`, `mark <id> <status>` (interview, rejected, offer, and so on).

---

## 12. CLI and "Before you start"

```
helper setup
helper convert
helper apply <url> [--projects N] [--mode review|auto] [--include-experience NAME] [--jd-file PATH] [--dry-run]
helper resume <url>        # intake + tailor + render only
helper fill <run_id>
helper list | show | stats | export | mark | sync-notion
```

**Ask me these first, then wait:**
1. Confirm these files are in place: `data/master_list_v23.md`, `templates/resume.tex.j2`, `templates/reference_resume.tex`, and `templates/reference_resume.pdf`. If any is missing, tell me which.
2. Is `pdflatex` or `xelatex` installed with `fontawesome5`? Check and tell me.
3. What is my Notion database ID and what are its column names?
4. My street address and any other form fields not already seeded from my reference resume (header details and degree dates are already known).
5. The rest of my `courses.yaml`: courses I have actually taken beyond the five already seeded.
6. Which Tier 2 items should auto-answer?

Do not write code until I answer or tell you to use defaults.

---

## 13. Build order

After each milestone: run tests, show me the result, wait for me to say continue.

1. **Skeleton.** Repo, config, pydantic models, CLI scaffold, SQLite schema, `helper setup`.
2. **Convert.** `helper convert` and `master.yaml`. Show me the parse summary.
3. **Template.** Build `resume.tex.j2` from my reference resume and compile a sample with real data. I approve the look.
4. **Tailor pipeline.** Keywords, `tailor_input.json`, the full validator in 7.7 with a test for every rule, rendering. Test with a pasted job description via `helper resume`.
5. **Eligibility and caps.**
6. **Tracker.** SQLite, CSV, Notion mapping and sync, list/show/stats/mark.
7. **Greenhouse adapter** with fixtures and `--dry-run` first.
8. **Lever and Ashby adapters.**
9. **Auto-submit mode,** off by default.
10. **Generic adapter and unsupported-site messages.**
11. **Polish.** README, `CLAUDE.md`, clear error messages.

---

## 14. Definition of done

- `helper apply <greenhouse url>` runs eligibility, produces a validated one-page PDF, fills the form, pauses on the final page, and after I submit logs everything to SQLite and Notion with the resume hash. Same for Lever and Ashby.
- Every validation rule has a passing test. No resume contains a GPA, an em dash, a summary section, or a claim that is not in the master list.
- Unknown required fields, CAPTCHAs, eligibility blockers, cap warnings, and duplicates stop the run with a clear message.
- The README lets me set up from scratch in under 30 minutes.

Start with section 12 now.
