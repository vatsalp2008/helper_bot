# helper: rules for Claude Code sessions

helper is Vatsal's local job-application tool. The full spec is `helper_claude_code_prompt_v2.md`, and section numbers below refer to it.

## How helper and Claude split the work
- No Anthropic API or SDK. Never call `claude -p` or any Claude binary from helper's code, background jobs, or scheduled tasks.
- This interactive session is the only reasoning engine. Each judgment step is a file handoff: helper writes an input file in `runs/<run_id>/`, Claude writes the output file, and helper validates it.
- Exit code 3 means "waiting for Claude". The message names the file to read and the file to write; after writing it, run `helper continue <run_id>`.
- Exit code 4 means paused for Vatsal: an eligibility blocker, CAPTCHA, or unknown required field (caps only flag). Tell him why; don't work around it. After an eligibility pause, run `helper continue <run_id> --build-anyway` only when he says build.
- Everything else is deterministic Python: Playwright, SQLite, the Notion REST API, LaTeX.
- If a Claude feature is blocked on this seat (Claude in Chrome, connectors), say so and use plain Playwright.

| helper writes | Claude writes | Notes |
|---|---|---|
| `tailor_input.json` | `tailor_output.json` | Schema in spec 7.6. Optional extras: `fit_paragraph`, per-project `reason`. Mark a bullet with no number with a flag `"qualitative: <item_id> bullet <n>: <why>"`. |
| `questions_needed.json` | `answers_provided.json` | Use only master-list facts and the job description. Every draft is `needs_review` until Vatsal approves it, then offer to save it to `data/answers.yaml`. |

The resume loop:
1. `helper resume --jd-file F --company C --role R` writes `tailor_input.json`. It holds the rules, the posting's keywords, and each item's allowed tech, numbers and wording rules.
2. Write `tailor_output.json`, then run `helper continue <run_id>`.
3. A rejection lists every problem in `validation.json`. Fix the output and continue again.
Never edit validation results or loosen a check to get a pass.

## 3. Source of truth

**`data/master_list_v23.md` is the single source of truth about me.** I have confirmed everything in it. That means:

- Every project, experience entry, technology, number, link, date, and status in it is a claim I stand behind. Do not flag, question, or "verify" anything in it. Do not second guess it.
- Variants and framings described in it are all legitimate (NutriTrack as React, Angular/TypeScript, or Vue.js + Django + GitLab CI; E-commerce Platform on Docker or Kubernetes; PostgreSQL or MySQL, DynamoDB or MongoDB per the "Database framing" rule; Seattle Transit with C++ plus Rust; PulseTrader with TimescaleDB plus BigQuery; QuickBooks, Stripe, GraphQL, and the other additions). Choose per job posting.
- **Anything not in the master list is not claimable.** No new tools, numbers, employers, courses, or responsibilities.
- If the master list and any other note disagree, **the master list wins.**
- The master list stays in the repo as the original markdown. A converter (section 5) builds structured YAML from it for validation. When I update the markdown, re-run `helper convert` to refresh the YAML.

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

## Decisions made while planning
- **Master list beats the reference resume.** Examples: the TA entry says "100+ students", not "around 60". "Linear Regression" can't be claimed.
- **Technologies in bullets and tech tags must belong to that specific project or experience entry.** That covers the entry's own text, its confirmed variants, and the Database framing rule. The framing rule applies to every project built with PostgreSQL, including through Supabase (`implies` in `data/tech_lexicon.yaml`). An entry names one database of a framed pair, never both. Skills lines may use anything in the master Skills section, any project or experience text, or a synonym marked `approved: true` in `data/skill_synonyms.yaml`.
- **Small tweaks to fit a posting are fine** (Vatsal, 2026-11-14). Reword a project's details and pick its tech framing to match the job description.
  - **Tech swaps:** same-family swaps from `data/tech_families.yaml` (all approved). They apply in projects only; experience entries keep their own tools. A swap replaces, never adds. Prefer the posting's own tool names in projects. Nothing outside the master list becomes claimable.
  - **Skill names:** when a posting names a skill the master list has under another name, use the posting's name via `data/skill_synonyms.yaml` (all approved). For a new one with clear evidence, add it with `approved: true` and flag it as `new synonym: <term>` in that run.
- **Company limits never block** (Vatsal, 2026-11-14). `data/caps.yaml` limits are flagged first, then helper still builds the resume and applies. `enforced: true` (Google, TikTok) means the company's site may refuse it; `enforced: false` (Salesforce) is a recommendation.
- **`~` prints centered on the line** as `$\sim$` (bold inside bold phrases). Vatsal chose this on 2026-11-14; spec 6's `\textasciitilde{}` prints a raised tilde in this font encoding.
- **Graduation date** (spec 8 item 4): May 2027 by default. Use December 2027 when the posting's graduation window or a "returning to school" requirement needs it. Use May 2027 when a full-time start date needs it. The run's chosen date also fills the form's expected-graduation field.
- **Sponsorship answer** is "No", except "Yes" when the question explicitly says sponsorship includes CPT/OPT. If CPT/OPT is mentioned but it's unclear whether it counts, pause.
- **Profile values:** "ask" means ask Vatsal every time. "per_run" means decided per application. `data/profile.yaml` is git-ignored.
- **Location data:** experience locations and project display names live in `data/resume_meta.yaml`, because the master list doesn't have them.

## Working conventions
- Python 3.12 venv at `.venv`. Run `.venv/bin/helper ...` and `.venv/bin/pytest`.
- The build follows spec section 13. After each milestone, run the tests, commit, push to `origin/main` (Vatsal approved this on 2026-11-13), show Vatsal the result, and wait for "continue". Outside milestone ends, commit only when he asks.
- The GitHub repo is public by Vatsal's choice. `data/profile.yaml` stays git-ignored.
- Never edit `data/master.yaml` by hand. It is generated by `helper convert`.
