# helper

From one job posting URL, helper builds a tailored one-page PDF resume, fills the application form, and logs the application. It logs to SQLite first, then Notion and CSV.

helper never calls an AI API. Judgment steps (tailoring, free-text answers) are done by Claude Code in an interactive session through files in `runs/<run_id>/`. Everything else is plain Python: Playwright, SQLite, the Notion REST API and LaTeX.

> Status: milestone 4 of 11 (tailor pipeline). A full setup guide arrives in milestone 11.

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

`helper setup`, `helper convert`, `helper template-check`, `helper resume` and `helper continue` work so far. The others print the milestone that builds them.

### Building a resume (milestone 4)

1. Save the job description as a text file. `helper resume --jd-file jd.txt --company "Acme" --role "Backend SWE Intern"` creates `runs/<run_id>/`. It writes `job.json`, `keywords.json` (the posting's technologies, sorted against the master list) and `tailor_input.json`, then exits with code 3.
2. Claude, in the Claude Code session, reads `tailor_input.json` and writes `tailor_output.json` in the same folder.
3. `helper continue <run_id>` checks it against every rule in spec 7.7, renders it, and checks the PDF: one page, no title or tag running past the margin, at most 2 lines per bullet.
   - If something fails, `validation.json` lists each problem and the command exits with code 3 again, so Claude fixes the output and reruns it.
   - When everything passes, the PDF lands in `resumes/` and the command prints the pre-build message: fit, keyword audit, track and projects.
4. Two proposal files wait for your approval. Nothing in them is used until you set `approved: true`:
   - `data/skill_synonyms.yaml`: skills-line terms such as Hypothesis Testing.
   - `data/tech_families.yaml`: same-family tech swaps such as GitHub Actions for GitLab CI.

Reading a posting from its URL arrives in milestone 7. Until then, pass `--jd-file`.

- Run `helper convert` after every edit to the master list. `helper convert --show <id>` prints one parsed item.
- `helper template-check` renders the reference resume's content (`templates/reference_content.yaml`) through `templates/resume.tex.j2`. It checks the result against `reference_resume.tex` (text) and `reference_resume.pdf` (pixels), and writes a side-by-side image to `runs/template-check/compare.png`.
- `--sample <tailor_output.json>` also renders that file with real master data and your profile header, for example `tests/fixtures/tailor_output_backend_sample.json`.
