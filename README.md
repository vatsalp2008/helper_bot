# helper

From one job posting URL, helper builds a tailored one-page PDF resume, fills the application form, and logs the application. It logs to SQLite first, then Notion and CSV.

helper never calls an AI API. Judgment steps (tailoring, free-text answers) are done by Claude Code in an interactive session through files in `runs/<run_id>/`. Everything else is plain Python: Playwright, SQLite, the Notion REST API and LaTeX.

> Status: milestone 8 of 11 (Greenhouse, Lever, Ashby and Workday applications). A full setup guide arrives in milestone 11.

## Setup

Requirements: macOS or Linux, Python 3.11+, [uv](https://docs.astral.sh/uv/), and TeX Live or MacTeX with `fontawesome5`.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/helper setup        # creates folders and the database, checks tools, reports what is missing
.venv/bin/pytest              # run the tests
```

`data/profile.yaml` holds your form answers and is git-ignored. On a fresh clone, `helper setup` creates it from `data/profile.example.yaml`. Fill it in by hand, or run `helper setup --interactive` in your own terminal.

## Commands

```
helper setup [--interactive]
helper convert [--show ID]
helper template-check [--sample tailor_output.json]
helper apply <url> [--projects N] [--include-experience ID] [--jd-file PATH] [--grad-date DATE] [--dry-run]
helper resume [url] --jd-file PATH --company NAME --role TITLE [--projects N] [--include-experience ID] [--grad-date DATE]
helper fill <run_id> [--dry-run] [--wait MINUTES]
helper continue <run_id>
helper list | show <id> | stats | export --csv | mark <id> <status> | sync-notion
```

Every command works. `helper apply` fills Greenhouse, Lever, Ashby and Workday forms; other sites arrive in milestone 10.

### Building a resume (milestone 4)

1. Save the job description as a text file. `helper resume --jd-file jd.txt --company "Acme" --role "Backend SWE Intern"` creates `runs/<run_id>/`. It writes `job.json` and `keywords.json` (the posting's technologies, sorted against the master list).
   - It then runs the eligibility check (spec 8) and writes `eligibility.json`. The check covers work authorization, degree level, timing, the graduation date, location, deadlines, and company caps from `data/caps.yaml` against your applications in SQLite.
   - If something blocks the application (for example a named F-1/OPT exclusion or an office far from Seattle during the semester), it recommends skipping and exits with code 4. `helper continue <run_id> --build-anyway` builds it anyway.
   - Company limits (Google 3 per 30 days, TikTok 2 per window, Salesforce's recommended 3 a year) are flagged first but never stop the run.
   - Otherwise it writes `tailor_input.json` and exits with code 3.
2. Claude, in the Claude Code session, reads `tailor_input.json` and writes `tailor_output.json` in the same folder.
3. `helper continue <run_id>` checks it against every rule in spec 7.7, renders it, and checks the PDF: one page, no title or tag running past the margin, at most 2 lines per bullet.
   - If something fails, `validation.json` lists each problem and the command exits with code 3 again, so Claude fixes the output and reruns it.
   - When everything passes, the PDF is saved as `resumes/<run_id>/VatsalPatel_Resume.pdf`. The file name never includes the company or role. The command then prints the pre-build message: fit, keyword audit, track and projects.
4. Two files let a resume use the posting's own names. Only entries with `approved: true` are used, and all current ones are:
   - `data/skill_synonyms.yaml`: skills-line terms such as Hypothesis Testing.
   - `data/tech_families.yaml`: same-family tech swaps such as GitHub Actions for GitLab CI, in projects only.

`helper resume <link>` reads Greenhouse, Lever, Ashby and Workday postings directly; other sites need `--jd-file` for now.

### Applying (milestones 7 and 8: Greenhouse, Lever, Ashby, Workday)

1. `helper apply <link>` reads the posting from the site's public API.
   - **Form questions:** Greenhouse lists them in its API. Lever and Ashby don't, so helper opens the application page in a hidden browser, types nothing, and reads the questions from the form.
   - It then runs the same duplicate check, eligibility check and resume steps as `helper resume`.
2. After the resume is built, `helper continue` plans every form answer from your profile, each with a confidence:
   - **Sure answers** are filled automatically. Free-text questions use an answer you approved in `data/answers.yaml`, or Claude drafts one from the master list and the job description (`questions_needed.json` -> `answers_provided.json`).
   - **Everything else waits for you:** drafts and uncertain answers are shown first, and `helper continue <run_id> --approve-answers` approves them. A GPA question is never filled.
3. `helper fill <run_id>` opens a Chromium window with helper's own browser profile and fills the form.
   - **It never clicks Submit** (review mode). You review the form and submit it yourself. If a CAPTCHA appears, you solve it; helper never does.
   - **After you submit,** helper sees the site's confirmation, takes a screenshot, and logs the application as *applied*: SQLite, then Notion, then `exports/applications.csv`. If Submit was clicked but no confirmation showed, it's logged as *submitted_unverified* and never retried.
4. `helper apply <link> --dry-run` shows the eligibility check and the planned answers without starting a run. `helper fill <run_id> --dry-run` shows what would be filled.

**Workday** works the same way, except that its questions appear page by page:
- `helper continue` finishes right after the resume. `helper fill` then goes through every page:
  - the account step (sign in, or create the account, with `WORKDAY_EMAIL` / `WORKDAY_PASSWORD` from `.env`);
  - My Information;
  - My Experience: your jobs from the resume, your schools from the profile, and the resume PDF (never a GPA);
  - the company's questions and the voluntary disclosures.
- Written answers are drafted by Claude while the window stays open, with no separate approval step.
- It stops on Workday's Review page. You check everything there and click Submit.
- A failed sign-in is never retried. If Workday wants an email verification, a CAPTCHA, or an answer only you can give, helper says so and waits for you in the window.

LinkedIn Easy Apply and Handshake are refused (rule 7). iCIMS, Taleo and similar sites are left to you, because they need accounts (rule 8). helper can still build the resume for any of them.

### Tracking applications (milestone 6)

- **SQLite is the record.** Every resume `helper continue` builds is tracked in `helper.db` as *drafted*. Before building, helper checks it for duplicates: the same job id or link as an application you already sent stops the run, and the same company and title is a warning.
- **After you apply,** `helper fill` logs it for you. If you applied some other way, run `helper mark <id> applied`. That adds the application to your Notion tracker: Title, Company, Application link, Location, Resume version (the PDF), Status = Applied, and Applied on. Nothing else in Notion is touched, and helper never adds columns. The mapping is in `data/notion_mapping.yaml`.
- **Syncing:** `helper sync-notion` sends anything pending and brings back status changes you make in Notion. The first run imports your existing tracker. `--check` compares the mapping with your tracker, and `--dry-run` shows what would change without writing.
- **Looking at your data:** `helper list [--status S] [--company C]`, `helper show <id>`, `helper stats [--weeks N]`, `helper export --csv`.
- **Outcomes:** `helper mark <id> oa|interview|offer|rejected|...` records them and updates Notion's Status.

- Run `helper convert` after every edit to the master list. `helper convert --show <id>` prints one parsed item.
- `helper template-check` renders the reference resume's content (`templates/reference_content.yaml`) through `templates/resume.tex.j2`. It checks the result against `reference_resume.tex` (text) and `reference_resume.pdf` (pixels), and writes a side-by-side image to `runs/template-check/compare.png`.
- `--sample <tailor_output.json>` also renders that file with real master data and your profile header, for example `tests/fixtures/tailor_output_backend_sample.json`.
