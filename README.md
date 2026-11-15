# helper

From one job posting URL, helper builds a tailored one-page PDF resume, fills the application form, and logs the application. It logs to SQLite first, then Notion and CSV.

helper never calls an AI API. Judgment steps (tailoring, free-text answers) are done by Claude Code in an interactive session through files in `runs/<run_id>/`. Everything else is plain Python: Playwright, SQLite, the Notion REST API and LaTeX.

> Status: milestone 6 of 11 (tracker and Notion). A full setup guide arrives in milestone 11.

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
helper apply <url> [--projects N] [--mode review|auto] [--include-experience NAME] [--jd-file PATH] [--dry-run]
helper resume [url] --jd-file PATH --company NAME --role TITLE [--projects N] [--include-experience ID] [--grad-date DATE]
helper fill <run_id> [--dry-run]
helper continue <run_id>
helper list | show <id> | stats | export --csv | mark <id> <status> | sync-notion
```

Everything except `helper apply` and `helper fill` works so far. Those two print the milestone that builds them.

### Building a resume (milestone 4)

1. Save the job description as a text file. `helper resume --jd-file jd.txt --company "Acme" --role "Backend SWE Intern"` creates `runs/<run_id>/`. It writes `job.json` and `keywords.json` (the posting's technologies, sorted against the master list).
   - It then runs the eligibility check (spec 8) and writes `eligibility.json`. The check covers work authorization, degree level, timing, the graduation date, location, deadlines, and company caps from `data/caps.yaml` against your applications in SQLite.
   - If something blocks the application (for example a named F-1/OPT exclusion or an office far from Seattle during the semester), it recommends skipping and exits with code 4. `helper continue <run_id> --build-anyway` builds it anyway.
   - Company limits (Google 3 per 30 days, TikTok 2 per window, Salesforce's recommended 3 a year) are flagged first but never stop the run.
   - Otherwise it writes `tailor_input.json` and exits with code 3.
2. Claude, in the Claude Code session, reads `tailor_input.json` and writes `tailor_output.json` in the same folder.
3. `helper continue <run_id>` checks it against every rule in spec 7.7, renders it, and checks the PDF: one page, no title or tag running past the margin, at most 2 lines per bullet.
   - If something fails, `validation.json` lists each problem and the command exits with code 3 again, so Claude fixes the output and reruns it.
   - When everything passes, the PDF lands in `resumes/` and the command prints the pre-build message: fit, keyword audit, track and projects.
4. Two proposal files wait for your approval. Nothing in them is used until you set `approved: true`:
   - `data/skill_synonyms.yaml`: skills-line terms such as Hypothesis Testing.
   - `data/tech_families.yaml`: same-family tech swaps such as GitHub Actions for GitLab CI.

Reading a posting from its URL arrives in milestone 7. Until then, pass `--jd-file`.

### Tracking applications (milestone 6)

- **SQLite is the record.** Every resume `helper continue` builds is tracked in `helper.db` as *drafted*. Before building, helper checks it for duplicates: the same job id or link as an application you already sent stops the run, and the same company and title is a warning.
- **After you apply,** run `helper mark <id> applied` (milestone 7 will do this for you). That adds the application to your Notion tracker: Title, Company, Application link, Location, Resume version (the PDF), Status = Applied, and Applied on. Nothing else in Notion is touched, and helper never adds columns. The mapping is in `data/notion_mapping.yaml`.
- **Syncing:** `helper sync-notion` sends anything pending and brings back status changes you make in Notion. The first run imports your existing tracker. `--check` compares the mapping with your tracker, and `--dry-run` shows what would change without writing.
- **Looking at your data:** `helper list [--status S] [--company C]`, `helper show <id>`, `helper stats [--weeks N]`, `helper export --csv`.
- **Outcomes:** `helper mark <id> oa|interview|offer|rejected|...` records them and updates Notion's Status.

- Run `helper convert` after every edit to the master list. `helper convert --show <id>` prints one parsed item.
- `helper template-check` renders the reference resume's content (`templates/reference_content.yaml`) through `templates/resume.tex.j2`. It checks the result against `reference_resume.tex` (text) and `reference_resume.pdf` (pixels), and writes a side-by-side image to `runs/template-check/compare.png`.
- `--sample <tailor_output.json>` also renders that file with real master data and your profile header, for example `tests/fixtures/tailor_output_backend_sample.json`.
