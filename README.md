# helper

From one job posting link, helper builds a tailored one-page resume and a cover letter, fills the application form,
and logs the application (SQLite first, then Notion and a CSV).

helper never calls an AI API. Every judgment step (choosing projects, writing bullets, the cover letter, free-text
answers) is done by Claude Code, in an interactive session in this folder, through files in `runs/<run_id>/`. helper
writes an input file and stops. Claude writes the matching output file, and helper checks it against the master list
before using it. Everything else is plain Python: Playwright, SQLite, the Notion REST API and LaTeX.

## Set up from scratch (about 30 minutes)

You need a Mac (Linux works too) and the master list, `data/master_list_v23.md`, which is already in the repo.

### 1. Install the tools (about 10 minutes)

```bash
# Homebrew first, if you don't have it: https://brew.sh
brew install uv poppler
brew install --cask basictex          # LaTeX, about 100 MB (full MacTeX also works but is about 5 GB)
eval "$(/usr/libexec/path_helper)"    # or open a new terminal, so pdflatex is on your PATH
sudo tlmgr update --self
sudo tlmgr install enumitem fancyhdr fontawesome5 preprint marvosym parskip titlesec
```

You also need [Claude Code](https://claude.com/claude-code). It is the reasoning half of helper.

### 2. Get the code (about 3 minutes)

```bash
git clone https://github.com/vatsalp2008/helper_bot.git
cd helper_bot
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/playwright install chromium          # the browser helper fills forms in
```

### 3. Run setup (1 minute)

```bash
.venv/bin/helper setup
```

`helper setup` creates the folders, the database (`helper.db`), `data/profile.yaml` and `data/answers.yaml`. It then
checks LaTeX, poppler, Playwright and every data file, and prints one line per check. On a fresh clone, expect one
`FAIL` for `profile.yaml` ("fill in header.name, ...") until you finish step 4.

### 4. Fill in your profile (about 10 minutes)

`data/profile.yaml` holds everything forms ask about you. It is git-ignored, so it never reaches GitHub. Open it next
to `data/profile.example.yaml`, which explains every field, and fill in:

- **header:** name, location, phone, email and links, exactly as the resume should show them.
- **tier1:** name, address, phone for forms, school, degree, links, how you heard about the job.
- **education:** your schools, with degree, major, location and dates, for forms that list them (Workday's My
  Experience page). The resume's Education section is fixed in the template.
- **tier2:** work authorization, sponsorship, salary, start dates and EEO answers. Each item has `auto_answer`: true
  means forms get the answer, false means helper asks you every time.
- **documents:** the transcript PDFs, if you keep them in this folder. They're uploaded only when a form requires one.

A value of `ask` means "ask me every time", `per_run` means "decided per application", and an empty value pauses any
form that asks for it. `helper setup --interactive`, run in your own terminal, prompts for every empty value.

The other private files are optional and git-ignored:

| File | What it holds |
|---|---|
| `data/grades.yaml` | GPA by school, for application forms only. A resume or cover letter never shows a GPA. |
| `data/answers.yaml` | Answers you approved, reused when another posting asks the same question. helper adds to it. |
| `data/certifications.yaml` | Certificates for Workday's Certifications section (off in `config.yaml` for now). |
| `data/cover_letter_examples.md` | Cover letters you liked, as style examples for Claude. |
| `.env` | The Notion token and database ID, and the Workday login. Copy `.env.example` to start. |

### 5. Optional: Notion and Workday (about 5 minutes)

- **Notion:** follow the comments in `.env.example` to create a connection named helper, share your tracker database
  with it, and put `NOTION_TOKEN` and `NOTION_DATABASE_ID` in `.env`. Then run `helper sync-notion --check`, which
  compares your tracker's columns with `data/notion_mapping.yaml` without writing anything. `helper sync-notion` then
  imports your existing rows, so the duplicate check sees them. Without Notion, applications are tracked in
  `helper.db` and `exports/applications.csv` only.
- **Workday:** put the login you use for Workday applications in `.env` as `WORKDAY_EMAIL` and `WORKDAY_PASSWORD`.
  helper types it only on Workday's own sites, never prints or logs it, and never retries a failed sign-in.

### 6. Check that it works (about 3 minutes)

```bash
.venv/bin/helper setup              # should end with "0 problem(s)"
.venv/bin/helper template-check     # renders the reference resume and compares it to templates/reference_resume.pdf
.venv/bin/pytest                    # all tests should pass
```

If LaTeX stops with "File `something.sty' not found", run `sudo tlmgr install something`. The one exception is
`fullpage.sty`, which comes in the `preprint` package.

## Applying to a job

Open Claude Code in this folder and give it the link, for example "apply to https://job-boards.greenhouse.io/acme/jobs/123".
Claude runs helper and does the writing. Here is what happens and when you step in.

1. **Reading the posting.** helper reads the job description and the form's questions from the site.
2. **Eligibility.** It checks work authorization, degree, timing, location, the graduation date, company limits and
   duplicates. A blocker, such as a named F-1/OPT exclusion, pauses the run and says why. You decide whether to skip it
   or build anyway.
3. **Resume.** Claude picks the projects and writes the bullets. helper checks every fact, number and tool against
   the master list, then checks the PDF: one page, nothing past the margin, at most two lines per bullet. Anything
   wrong goes back to Claude until it passes. The file is `resumes/<run_id>/VatsalPatel_Resume.pdf`.
4. **Cover letter.** Claude writes four paragraphs from the same projects. helper checks each sentence against the
   project it names and saves `<Name>_CoverLetter.pdf` next to the resume.
5. **Answers.** Most questions are answered from your profile. Claude drafts the rest from the master list and the
   posting. **You review every draft** before anything is filled.
6. **Filling.** A Chromium window opens on your screen and helper fills the form. **It never clicks Submit**: you
   check the form and submit it yourself. If a CAPTCHA or a verification code appears, that's yours too.
7. **Logging.** When the site confirms the submission, helper saves a screenshot and records the application in
   SQLite, Notion and `exports/applications.csv`.

To get only the resume and cover letter, use `helper resume <link>`. For a posting you saved as text, use
`helper resume --jd-file jd.txt --company "Acme" --role "Backend Intern"`. If you apply some other way, run
`helper mark <id> applied` afterwards to log it.

### Which sites helper fills

| Site | What helper does |
|---|---|
| Greenhouse, Lever, Ashby | Fills the form and stops before Submit. Auto-submit is possible after 5 clean runs (below). |
| Workday | Signs in or creates the account with your `.env` login, fills every page and stops on the Review page. Questions are drafted as the pages open, with no separate approval step; you review them all on the Review page. |
| A company's own careers page | Looks for a Greenhouse, Lever, Ashby or Workday posting behind it and uses that adapter. Otherwise it reads the form by its labels, fills what it can and stops before Submit. |
| iCIMS, Taleo, SmartRecruiters, SuccessFactors | Never filled, because they need an account at each company. `helper resume <link>` still builds the resume and cover letter. |
| LinkedIn Easy Apply, Handshake | Refused. Find the job on the company's own site; helper can still read the description for a resume. |
| Sign-in pages, robot checks | Left to you. helper never solves a CAPTCHA, never gets around a site's bot filter, and never creates accounts outside Workday. |

### Auto-submit (off by default)

`helper apply <link> --mode auto`, or `submit_mode: auto` in `config.yaml`, lets helper click Submit itself, once, when
every condition holds:
- the site has 5 clean review-mode runs;
- every required answer came from your profile, a fixed rule or an approved answer;
- there are no eligibility warnings or duplicates;
- no CAPTCHA or form errors appeared.

Otherwise the run stays in review mode and says why. `helper stats` shows each site's count. Workday and other
company sites are always review-only.

## Commands

```
helper setup [--interactive]
helper convert [--show ID]                      # rebuild data/master.yaml after editing the master list
helper template-check [--sample FILE]
helper apply <url> [--mode review|auto] [--projects N] [--include-experience ID] [--jd-file F] [--grad-date D] [--dry-run]
helper resume [url] [--jd-file F --company C --role R] [--location L] [--projects N] [--include-experience ID] [--grad-date D]
helper continue <run_id> [--approve-answers] [--build-anyway]
helper cover-letter <run_id> [--redo]
helper fill <run_id> [--wait MINUTES] [--headless] [--dry-run]
helper list [--status S] [--company C] [--limit N] [--all]
helper show <id>
helper stats [--weeks N]
helper mark <id> <status> [--date YYYY-MM-DD] [--note TEXT]   # applied, oa, interview, offer, rejected, ...
helper export --csv [--out FILE]
helper sync-notion [--check] [--dry-run]
```

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Done. |
| 1 | An error. The message says what to fix. |
| 3 | Waiting for Claude: helper names the file to read and the file to write, then `helper continue <run_id>`. |
| 4 | Waiting for you: an eligibility blocker, a CAPTCHA, a question only you can answer, or answers to review. |
| 130 | You pressed Ctrl+C. Progress so far is saved. |

## When something goes wrong

- **"helper's browser profile is in use":** another `helper fill` still has its window open. Close it or let it
  finish. helper runs one fill at a time.
- **"Playwright's Chromium isn't installed":** run `.venv/bin/playwright install chromium` (again after upgrading
  Playwright).
- **"couldn't read a job description":** the site blocked helper or the posting is closed. Save the description as
  text and use `--jd-file`.
- **A resume keeps getting rejected:** `runs/<run_id>/validation.json` lists every problem. The checks are never
  loosened to get a pass.
- **After editing the master list:** run `helper convert`, or every run stops with a reminder to.
- **"helper hit an unexpected error":** that is a bug in helper. The full details are saved in
  `~/.helper/last_error.txt`; show them to Claude. `HELPER_DEBUG=1 helper ...` prints them in the terminal instead.

## Where things live

| Path | What it is |
|---|---|
| `data/master_list_v23.md` | The single source of truth about you. Anything not in it can't be claimed. |
| `data/master.yaml` | Built from the master list by `helper convert`. Never edited by hand. |
| `data/*.yaml` | Rules and reference data: approved skill names and tech swaps, company limits, project sets, the Notion mapping. |
| `config.yaml` | Settings: graduation dates, timeouts, submit mode, how many projects per resume. |
| `templates/` | The resume and cover letter LaTeX templates, and the reference resume they're checked against. |
| `runs/<run_id>/` | Every file of one application: the posting, the handoff files, validation results, the fill log. |
| `resumes/<run_id>/` | The resume and cover letter PDFs for that application. |
| `helper.db` | The application tracker (SQLite). `exports/applications.csv` is a copy. |
| `CLAUDE.md` | The rules Claude follows in this repo. `helper_claude_code_prompt_v2.md` is the full spec. |

## The rules helper enforces in code

- No invented facts: every bullet, skill, title, date and number traces to the master list.
- Metrics are real; a bullet without one says so instead of estimating.
- No GPA on resumes or cover letters, no em dashes, no summary section.
- Never solves CAPTCHAs, never automates LinkedIn Easy Apply or Handshake, never creates accounts (Workday excepted).
- Stops when unsure, never retries a submit, and checks eligibility before building anything.
