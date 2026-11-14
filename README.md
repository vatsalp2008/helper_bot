# helper

From one job posting URL, helper builds a tailored one-page PDF resume, fills the application form, and logs the application. It logs to SQLite first, then Notion and CSV.

helper never calls an AI API. Judgment steps (tailoring, free-text answers) are done by Claude Code in an interactive session through files in `runs/<run_id>/`. Everything else is plain Python: Playwright, SQLite, the Notion REST API and LaTeX.

> Status: milestone 3 of 11 (resume template). A full setup guide arrives in milestone 11.

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
helper resume [url] [--jd-file PATH] [--company NAME --role TITLE]
helper fill <run_id> [--dry-run]
helper continue <run_id>
helper list | show <id> | stats | export --csv | mark <id> <status> | sync-notion
```

`helper setup`, `helper convert` and `helper template-check` work so far. The others print the milestone that builds them.

- Run `helper convert` after every edit to the master list. `helper convert --show <id>` prints one parsed item.
- `helper template-check` renders the reference resume's content (`templates/reference_content.yaml`) through `templates/resume.tex.j2`. It checks the result against `reference_resume.tex` (text) and `reference_resume.pdf` (pixels), and writes a side-by-side image to `runs/template-check/compare.png`.
- `--sample <tailor_output.json>` also renders that file with real master data and your profile header, for example `tests/fixtures/tailor_output_backend_sample.json`.
