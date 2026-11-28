# helper: rules for Claude Code sessions

helper is Vatsal's local job-application tool. The full spec is `helper_claude_code_prompt_v2.md`, and section numbers below refer to it.

## How helper and Claude split the work
- No Anthropic API or SDK. Never call `claude -p` or any Claude binary from helper's code, background jobs, or scheduled tasks.
- This interactive session is the only reasoning engine. Each judgment step is a file handoff: helper writes an input file in `runs/<run_id>/`, Claude writes the output file, and helper validates it.
- Exit code 3 means "waiting for Claude". The message names the file to read and the file to write; after writing it, run `helper continue <run_id>`.
- Exit code 4 means paused for Vatsal: an eligibility blocker, CAPTCHA, or unknown required field (caps only flag). Tell him why; don't work around it. After an eligibility pause, run `helper continue <run_id> --build-anyway` only when he says build.
- "helper hit an unexpected error" is a bug in helper: read `~/.helper/last_error.txt` (or rerun with `HELPER_DEBUG=1`), fix the code, add a test. Other failures already come with a plain message that says what to do. Exit 130 is Ctrl+C.
- Everything else is deterministic Python: Playwright, SQLite, the Notion REST API, LaTeX.
- If a Claude feature is blocked on this seat (Claude in Chrome, connectors), say so and use plain Playwright.

| helper writes | Claude writes | Notes |
|---|---|---|
| `tailor_input.json` | `tailor_output.json` | Schema in spec 7.6. Optional extras: `fit_paragraph`, per-project `reason`. Mark a bullet with no number with a flag `"qualitative: <item_id> bullet <n>: <why>"`. |
| `cover_letter_input.json` | `cover_letter_output.json` | `{hook, stories, paragraphs, flags}`: four paragraphs, 400 to 450 words, bold key words (at most 4 per paragraph). Facts only from the resume's 7 items; helper checks each sentence against the item it names. `hook` must be the posting's own words. |
| `questions_needed.json` | `answers_provided.json` | Use only master-list facts and the job description. Every draft stays `needs_review`; Vatsal sees it in the form (or approves it first with `answer_review: before_fill`). Free-text drafts go to `data/answers.yaml` only when he asks. |
| `inbox_input.json` | `inbox_output.json` | `{emails: [{message_id, category, application_id, summary, action, deadline, done}]}`, one per email. Categories: rejection, oa, phone_screen, interview, offer, received, next_step, other. Say what each email is; helper decides status changes. See "Inbox". |

The resume loop:
1. `helper resume --jd-file F --company C --role R` writes `tailor_input.json`. It holds the rules, the posting's keywords, and each item's allowed tech, numbers and wording rules.
2. Write `tailor_output.json`, then run `helper continue <run_id>`.
3. A rejection lists every problem in `validation.json`. Fix the output and continue again.
4. Then the cover letter: `cover_letter_input.json` -> `cover_letter_output.json`, `helper continue` again (rejections in `cover_letter_validation.json`). `helper cover-letter <run_id> [--redo]` writes one for an older run or starts over.
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
3. **No GPA on resumes** or cover letters. There is no GPA field in the template or any data file used for resumes. Application forms that ask for GPA get it from `data/grades.yaml` (git-ignored, forms only; Vatsal, 2026-11-15). Without a value there, pause and ask me.
4. **No em dashes** anywhere in resumes, answers, or generated text. Use commas or periods.
5. **No summary section** on resumes.
6. **Never solve or bypass CAPTCHAs.** Stop and hand control to me in the open browser window.
7. **Never automate LinkedIn Easy Apply or Handshake.** Tell me to find the company's own careers page, and offer to extract the job description only.
8. **Never create accounts** on employer portals (Taleo, SmartRecruiters, etc.). Stop and tell me. The exceptions are Workday (Vatsal, 2026-11-14) and iCIMS (Vatsal, 2026-11-15): helper signs in or creates the account with the application login in `.env` (`WORKDAY_*`, `ICIMS_*`; iCIMS sites that ask for a username get `ICIMS_USERNAME`).
9. **Stop when unsure.** A required field helper cannot fill with high confidence pauses the run.
10. **Never retry a submit.** An ambiguous result is logged as `submitted_unverified`. The one exception is Greenhouse's emailed security code: Greenhouse holds the first click and says to resubmit, so after typing the code helper clicks Submit once more (Vatsal, 2026-11-27, only for Greenhouse's code).
11. **Eligibility is checked before building** (section 8).
12. **Do not re-ask for a job description I already gave you.** If I paste one in the session or pass `--jd-file`, use it.

## Decisions made while planning
- **Master list beats the reference resume.** Examples: the TA entry says "100+ students", not "around 60". "Linear Regression" can't be claimed.
- **Technologies in bullets and tech tags must belong to that specific project or experience entry.** That covers the entry's own text, its confirmed variants, and the Database framing rule. The framing rule applies to every project built with PostgreSQL, including through Supabase (`implies` in `data/tech_lexicon.yaml`). An entry names one database of a framed pair, never both. Skills lines may use anything in the master Skills section, any project or experience text, or a synonym marked `approved: true` in `data/skill_synonyms.yaml`.
- **Small tweaks to fit a posting are fine** (Vatsal, 2026-11-14). Reword a project's details and pick its tech framing to match the job description.
  - **Tech swaps:** same-family swaps from `data/tech_families.yaml` (all approved). They apply in projects only; experience entries keep their own tools. A swap replaces, never adds. Prefer the posting's own tool names in projects. Nothing outside the master list becomes claimable.
  - **Skill names:** when a posting names a skill the master list has under another name, use the posting's name via `data/skill_synonyms.yaml` (all approved). For a new one with clear evidence, add it with `approved: true` and flag it as `new synonym: <term>` in that run.
- **Duplicates are identified by job ID or link** (Vatsal, 2026-11-14). Postings often share a title while having different job IDs, links or locations, so a title never makes two postings the same. Only when there's no link or ID to compare does company + title + location together give a "possible" note.
- **The resume file is always named `VatsalPatel_Resume.pdf`** (Vatsal, 2026-11-14), with no company or role; this replaces spec 6's `_<Company>_<RoleShortName>`. Each run's copy lives in `resumes/<run_id>/`.
- **Workday** (Vatsal, 2026-11-14): helper fills Workday applications.
  - **Account:** it signs in, or creates the account, with `WORKDAY_EMAIL` / `WORKDAY_PASSWORD` from `.env`. Never print, log, commit or repeat them; helper types them only on Workday's own sites and never retries a failed sign-in.
  - **No approval step:** company questions are drafted and filled as the pages open. Vatsal reviews everything on Workday's Review page before he submits.
- **Form answers Vatsal set on 2026-11-14** (all in `data/profile.yaml`):
  - **Salary:** with `tier2.salary_from_posting`, a salary question gets the posting's own pay: the midpoint of its range, annual unless the question asks hourly or monthly, rounded (nearest $1,000 a year). With no pay in the posting, a text box gets `salary_expectation` ("Negotiable") and a numbers-only box goes to Vatsal.
  - **AI screening and keeping the application for other roles** (for example HiredScore): yes, the choice that opts in (`tier2.ai_screening_consent`, `tier2.keep_for_other_roles`). Always yes, however it's worded, such as "Do you permit Labcorp to process your data using AI tools...?" (Vatsal, 2026-11-17).
  - **Hispanic or Latino:** No (`tier2.eeo.hispanic_latino`). Other extra EEO questions, such as pronouns, keep the decline default.
  - **Relatives at the company:** none, always (Vatsal, 2026-11-15; `tier2.relatives_at_company`): "N/A" in a written box, "No" or "None" in a choice question.
  - **Start dates** (Vatsal, 2026-11-15): internships can start now (he is free from January), so a "start now / immediately" choice is right and an immediate-start internship is only a note; full-time roles start in May (the summer option; `immediate_start_target: "May 2027"`). He is open to Spring 2027 work and co-ops anywhere, including school-year hours (Vatsal, 2026-11-16; `free_from: "January 2027"`); only fall terms clash with classes. He can relocate, even during a semester (Vatsal, 2026-11-17): a far-away office or a "must live in the X area" requirement is only a note, never a reason to skip.
  - **Salary boxes** get the plain number ("36", "71000"): iCIMS rejects "$36 per hour".
  - **Commuting, on-site and hybrid attendance:** always Yes (Vatsal, 2026-11-15).
  - **Military service questions** (his, or a spouse's or partner's): "No", or the never-served choice (Vatsal, 2026-11-16). Protected-veteran questions keep the profile answer.
  - **Citizenship:** from the profile (`tier1.citizenship` / `citizenship_since`, git-ignored). **Past employers:** all his experience is in the master list, so any other employer (the company, its affiliates, dealers or contractors) gets "No".
  - **Immigration or citizenship status lists** (US Citizen / Permanent Resident / Protected Person / None): "None"; "Are you a U.S. person?": No (Vatsal, 2026-11-15). A list without a "None" choice (naming visa types) goes to him.
  - **"Indefinite" or "permanent" work authorization in a posting** (Vatsal, 2026-11-17): apply anyway, now and for future postings; the eligibility check only notes it. A form question that asks it directly still gets the current, true answer.
  - **Postings that ask for undergraduates** ("pursuing a Bachelor's degree", rising seniors; Vatsal, 2026-11-25): apply anyway. The eligibility check only notes it, so auto-submit still sends them.
  - **Transcripts:** `documents.transcript` (master's) and `documents.undergrad_transcript` are uploaded only when a form requires a transcript; the undergraduate one when the question asks for it. They're PDFs in the repo's top folder, which git ignores.
- **Cover letters** (Vatsal, 2026-11-14): one with every resume, saved as `<Name>_CoverLetter.pdf` next to the resume, and uploaded wherever a form has a cover letter field (a text box gets the plain text). Once it passes helper's checks it needs no separate review, so it never holds up auto-submit (Vatsal, 2026-11-15).
  - **Format:** his template (`templates/cover_letter.tex.j2`): resume header, thin cyan line, 14pt paragraph spacing. Bold key words. Always "Dear Hiring Team,"; Northeastern campus jobs get "Dear Career Team," and his NUID after the name (`tier1.nuid`, profile only).
  - **Writing:** paragraph 1 why this role (a quoted posting phrase when it truly fits), 2 main proof, 3 rigor (often the open-source merges), 4 close (Master's student in Computer Science at Northeastern University, thanks). His approved examples are in `data/cover_letter_examples.md` (git-ignored).
  - **"Note to Hiring Manager" boxes:** a short note, "Hi," then two short paragraphs (about 60 to 90 words: one story, one honest line about fit), then "Thank you," and "Vatsal Patel".
- **Remembered answers** (Vatsal, 2026-11-15): answers to choice questions (dropdowns, radios, checkboxes) that Claude provided or Vatsal approved are saved to `data/answers.yaml` (git-ignored) and reused when another posting asks the same question. helper's own rules come first, so answers that change over time keep following the profile. Free-text answers are saved only when Vatsal asks.
- **Workday Websites** (Vatsal, 2026-11-15): the Websites section gets LinkedIn, GitHub and Portfolio from the profile.
- **Workday Skills and Certifications** (Vatsal, 2026-11-14; certifications off since 2026-11-15, `fill_certifications: false`):
  - **Skills:** each skill on the tailored resume's skills lines is added when the company's list has the same skill (exactly, or with a qualifier such as "Python (Programming Language)").
  - **Certifications:** `data/certifications.yaml` (git-ignored, read from his certificate images) lists his 13 certificates. Each is added with its ID and issue date only when the company's preset list has it; the rest are reported as not on that company's list.
- **Company limits never block** (Vatsal, 2026-11-14). `data/caps.yaml` limits are flagged first, then helper still builds the resume and applies. `enforced: true` (Google, TikTok) means the company's site may refuse it; `enforced: false` (Salesforce) is a recommendation.
- **`~` prints centered on the line** as `$\sim$` (bold inside bold phrases). Vatsal chose this on 2026-11-14; spec 6's `\textasciitilde{}` prints a raised tilde in this font encoding.
- **Graduation date** (spec 8 item 4): May 2027 by default and for full-time roles. **Summer 2027 internships always use December 2027** (Vatsal, 2026-11-15: he's back at school after the summer), unless the posting's window rules it out. December 2027 also when a "returning to school" requirement needs it. The run's chosen date fills the resume and the form's expected-graduation field.
- **Sponsorship answer** is "No", except "Yes" when the question explicitly says sponsorship includes CPT/OPT. If CPT/OPT is mentioned but it's unclear whether it counts, pause.
- **Profile values:** "ask" means ask Vatsal every time. "per_run" means decided per application. `data/profile.yaml` is git-ignored.
- **Location data:** experience locations and project display names live in `data/resume_meta.yaml`, because the master list doesn't have them.

## Applying
Supported sites: Greenhouse, Lever, Ashby, Workday, iCIMS and Oracle, plus the generic adapter for any other site.
- **The generic adapter:** finds a Greenhouse, Lever, Ashby or Workday posting behind a company's own page and hands it over. Otherwise it reads the page's form by its labels, fills it, and auto-submits when everything is filled (see Auto-submit). A sign-in wall or a blocked page is left to Vatsal.
- **Stopped sites:** Taleo, SmartRecruiters and SuccessFactors are never filled (accounts, rule 8). `helper resume <link>` still builds their resume and cover letter from the posting, and `helper assist <run_id>` (Vatsal, 2026-11-16) opens the posting in his normal browser, shows the files in Finder and prints a paste sheet (also for any posting he'd rather fill himself); after he submits, `helper mark <id> applied`. A company page whose Apply button leads to one of them gets the same stop message; one that leads to iCIMS (Jibe pages do) becomes an iCIMS run.
- **Oracle** (Vatsal, 2026-11-19): helper types the application email (profile) on Oracle's own apply page, never into its hidden honeypot box, and clicks Next. helper reads Oracle's emailed code from Gmail and types it in (see "Emailed codes"). helper then fills each section by its labels, uploads the resume through Oracle's upload button, types the e-signature, and stops at Submit (review-only while new). Each page's markup is saved as `runs/<run_id>/oracle_page_N.html` for tuning.
- **Lever, for now** (Vatsal, 2026-11-17): Lever's bot check rejected every submit from helper's window (TRI, Quest), so Lever postings are applied to by hand. `helper find` lists them as "you apply"; build them with `helper resume <link>`, and they go to the list's Apply manually group with their `helper assist` command. Never disguise helper's browser to get past it.
- **Employment sections** on Greenhouse forms get the jobs on the tailored resume (company, title, months and years, current role), the same entries Workday's My Experience gets (Vatsal, 2026-11-15).
- **Bot filters:** helper identifies itself honestly. Never disguise its browser or requests (user agent, stealth tricks) to get past a site that turns it away. A robot check, a blocked page or a sign-in wall goes to Vatsal, or use `--jd-file`.
- **Job boards that relay postings** (divulgavagas, thebigjobsite and the like) aren't the employer's posting. Before tailoring, check that the company helper read is the real employer; if it isn't, ask Vatsal for the employer's own link.
1. `helper apply <link>` goes through eligibility (exit 4 pauses), then the tailor handoff (exit 3). Then `helper continue <run_id>`.
2. After the resume is built, the answers handoff starts: read `questions_needed.json` and write `answers_provided.json`, as `{"answers": [{"field", "answer"}]}`.
   - Drafts use master-list facts and the job description only. Dropdown answers must be one of the options.
   - Leave out anything only Vatsal can answer.
   - **What Claude answers vs. leaves to Vatsal** (Vatsal, 2026-11-16): answer everything the master list, profile and his rules support. Preference and fit questions (which team or area, what excites you) get the closest match to the master list; counts (internships) come from the master list; full-time start dates on or after May 2027 are Yes. Leave to Vatsal only facts that can't be derived. "How did you hear": the profile's answer, else Social Media, else any option except referrals.
3. **Reviewing drafts in the form** (Vatsal, 2026-11-15; `answer_review: in_form` in config.yaml): `helper continue` puts Claude's drafts straight into the plan and lists them, with the questions only Vatsal can answer (left blank in the window). There is no approval pause; he checks everything in the window before Submit. With `drafts_need_review: false` (Vatsal, 2026-11-15) Claude's drafts count as approved, so auto-submit may send them; helper's own unsure matches (kind `confirm`) still block it. With `answer_review: before_fill` (spec 10.4), `helper continue` pauses (exit 4): show him every draft and run `--approve-answers` only after he approves.
4. Then run `helper fill <run_id>` in the background (run_in_background). It opens a visible Chromium window on Vatsal's screen, fills the form, and never clicks Submit. It waits up to `fill_wait_timeout_minutes` for him to submit, then logs to SQLite, Notion and CSV. Report the result when the task exits.
- **Workday and iCIMS runs** differ: `helper continue` ends right after the resume, because these sites show their questions page by page.
  - **iCIMS** (Vatsal, 2026-11-15): helper enters `ICIMS_EMAIL`, then signs in or creates the account (`ICIMS_USERNAME` where a username is asked). It types the login only inside iCIMS's own frame (`in_iframe=1`), never on the company's page around it (City National's has an online-banking login). It fills each page by its labels and stops at any button that says Submit; Vatsal clicks it, and helper carries on if more pages follow. hCaptcha checks are Vatsal's, and so are iCIMS's emailed codes (helper doesn't read those yet): every iCIMS sign-in has hCaptcha. Some sites (City National) kept re-asking in helper's window; Cotiviti went through. The veteran and disability self-identification forms (IForms) open as a whole page at `.../form?form=...`; helper reads and fills those, but types the login only inside the in_iframe=1 frame.
  1. Run `helper fill <run_id>` in the background, and watch its output for "waiting for Claude" (a background until-loop that greps the output file).
  2. When it asks, read `questions_needed.json` and write `answers_provided.json` (same format and rules as above). helper fills them and moves on by itself; there is no approval step.
  3. Workday's verification email: helper opens its link from Gmail itself and signs in (see "Emailed codes"). If it says no link came, tell Vatsal. When he says he verified, create the empty file `runs/<run_id>/go_on` and helper signs in.
  4. It stops on the Review page; Vatsal submits.
- Never type Vatsal's data into a live form outside a run he started. Live checks use the fictional test profile and never submit.

## Auto-submit
- **Off by default** (`submit_mode: review` in config.yaml). Turn it on only when Vatsal asks: `helper apply <url> --mode auto` for one run, or `submit_mode: auto` in config.yaml for every run.
- **The gate:** in auto mode helper clicks Submit itself, once and never again (rule 10), only if every spec 10.5 condition holds:
  - the site has at least `auto_submit_min_clean_runs` (5) clean review-mode runs;
  - every required answer came from the profile, a fixed rule, an approved answer or (with `drafts_need_review: false`) a Claude draft, and nothing still needs review;
  - there are no eligibility warnings or blockers, and no duplicates;
  - no CAPTCHA or verification step appeared, and the form shows no errors.
  Otherwise the run falls back to review mode and says why.
- **Clean runs:** every fill is logged in SQLite (`fill_runs`). A review run is clean when the submit was confirmed and helper filled every field with nothing for Vatsal to fix. `helper stats` shows each site's count.
- **Workday auto-submits on its Review page** (Vatsal, 2026-11-16, `submit_mode: auto`): whether or not Vatsal stepped in along the way, since Workday shows Review only when every required field is filled. It still stops if the Review page shows errors or there isn't exactly one Submit button. Company-site forms (the generic adapter) auto-submit too (Vatsal, 2026-11-16) once every required field is filled, however it got filled, the form shows no errors, and there is exactly one final Submit button. iCIMS never auto-submits.
- **Workday school field** (Vatsal, 2026-11-16): when Workday rejects My Experience because "School or University" is empty, helper deletes the education entries and goes on (Dexcom and Rocket accepted that). If an entry can't be deleted, it hands over.

## Unattended job hunt (Vatsal, 2026-11-16)
Five times a day (2am, 7am, noon, 5pm and 10pm; Vatsal, 2026-11-18, replacing every 3 hours), this Claude Code session wakes itself and finds and applies on its own.
- **The schedule:** CronCreate `2 2,7,12,17,22 * * *` with the prompt "Run the scheduled job hunt: follow CLAUDE.md 'Unattended job hunt'."
  - It lives only in this session. A recurring job expires after 7 days, so every session renews it.
  - helper never calls Claude itself: the session is the reasoning engine, as everywhere else.
  - `caffeinate -is` runs in the background so the Mac stays awake.
  - "Stop the job hunt" means CronDelete plus stopping caffeinate.
- **The list:** a `postings` table in helper.db, statuses queued, applying, needs_you, ready_for_you and skipped.
  - `helper queue` shows it; `exports/postings_queue.md` is rewritten after every change.
  - The private web page https://claude.ai/artifact/7K1pQVuvwwhWqWhiDQ9keF reads one record, `hunt/list`. Its source is `templates/job_hunt_page.html`.
  - A confirmed submit, or `helper mark <id> applied`, takes a posting off the list. A stuck fill sets needs_you with the reason. A resume-only run sets ready_for_you with its `helper assist` command.
- **Vatsal's decisions:**
  - Fewer than 10 good postings from the last 24 hours: apply to the good ones and say so with `helper queue note`. Never pad with stale or poor fits.
  - Ineligible postings (U.S. citizenship or U.S.-person required, PhD only, a graduation window he doesn't fit) are not built. They're listed as skipped with the reason; `helper find` and `helper apply` do this themselves.
  - A stuck window stays open (`--keep-open`, `stuck_window_hours`); he reviews it later.
  - **Fixed errors get a fresh window** (Vatsal, 2026-11-17): when a fill stalls on a helper bug and the bug is fixed, stop that fill (TaskStop closes its window) and run `helper fill <run_id> --keep-open` again. Add `--replan` when the fix was in an answer rule: a single-page form otherwise reuses its first plan (Claude's answers are kept). Use `--rescan` instead when the fix was in how helper reads a company-site form: it reads the questions again, and new ones the rules can't answer come to Claude (exit 3; `helper continue`, then `helper fill` again). Never when a submit may have gone through (`submitted_unverified`, rule 10), and never in a window he's working in. Stalls only he can clear (CAPTCHA, a code or link helper couldn't get from Gmail, answers only he knows) stay open.
  - Windows are unlimited (`max_browser_windows: 0`), and a new session starts on schedule whatever is still open.
- **Each session:**
  1. **Renew the schedule:** CronList, then CronDelete the hunt's job and CronCreate it again with the same cron and prompt. Start `caffeinate -is` in the background if it isn't running.
  2. **Inbox, once a day:** only in the 10pm session (Vatsal, 2026-11-27); the other sessions skip it. Run `helper inbox` (see "Inbox"), classify, then `helper continue <run_id>`, which applies the changes at once (`inbox_review: auto`). Its to-dos go on the list. Mention the changes in the session note.
  3. **Leftovers first:** finish what earlier sessions started before new tailoring.
     - Agents that a Claude usage limit stopped get resumed with SendMessage ("the limit has reset, continue"); their work is saved, so never start them over.
     - Postings still queued, or runs still waiting on tailoring, come before new picks. Usage limits are the real ceiling on volume.
     - Windows stuck on a helper bug that's now fixed get filled again (see "Fixed errors get a fresh window").
  4. **Find:** `helper find --hours 24`. It reads Greenhouse, Lever, Ashby, Workday and SmartRecruiters feeds and the SimplifyJobs lists, checks eligibility, and lists ineligible postings as skipped.
  5. **Pick:** at least 10 of the eligible postings by fit to the master list (software, data, ML, AI; early career). Include "you apply" ones (SmartRecruiters, Taleo, SuccessFactors). Never research positions (research scientist, researcher, "... Research" roles; Vatsal, 2026-11-17); `helper find` leaves them out.
     - **A wide range of roles** (Vatsal, 2026-11-17): besides engineering, AI-adjacent business roles (an enterprise AI associate, solutions, analyst roles) and supply chain roles are good picks; "not engineering" is never a reason to skip. Quant trader roles count as close to software (Akuna, Optiver and other trading firms hire software people as quant traders). Skip only roles built on another discipline's degree (mechanical, electrical or hardware engineering, law, clinical).
     - `helper queue add <url>...` for the picks.
     - `helper queue add <url> --skip "not a fit: <why>"` for the others, so they don't come back.
  6. **Start the runs:** helper-fillable postings get `helper apply <url>`. "You apply" postings get `helper resume <url>`, and become ready_for_you once the cover letter passes.
  7. **Tailor:** in parallel with general-purpose agents, 2 runs each. The brief is this file's rules plus the tailor and cover letter loop; agents stop at the fill step and never run `helper fill`.
  8. **Fill:** `helper fill <run_id> --keep-open` in the background, a few seconds apart, with one watcher over all of them (`scripts/watch_fills.sh <task output folder> <task ids>` in the background; restart it after each event).
     - Answer `questions_needed.json` by his rules. Auto-submit sends what's complete.
     - Leave CAPTCHAs, codes helper couldn't get from Gmail, and fields only he can answer in the open window. The list shows them as needs_you.
  9. **Close the session:**
     - `helper queue note "<found N, picked N, sent N, N need you, N ready for you, N email updates>"`, including a shortfall if fewer than 10 good postings existed.
     - `helper queue --page-file exports/queue_page.json`, then ArtifactData `get` hunt/list (for its version) and `set` hunt/list from that file with `if_version`. On the first write there's no version yet.
  10. **Don't wait for open windows.** Answers to later questions come when their watcher fires, even during a later session.

## Inbox (Vatsal, 2026-11-27)
`helper inbox [--since YYYY-MM-DD]` reads the application emails, so rejections, assessments, interviews and offers reach the tracker, Notion and the list.
- **Access:** Gmail over IMAP with an app password (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` in `.env`; never print, log or commit them). It opens All Mail read-only and never marks, moves or deletes mail.
- **What reaches Claude:** only mail from a job site or assessment site, from a company's recruiting side, or with an application subject. Job alerts and surveys are counted, not shown.
- **Codes stay out:** sign-in, identity and verification-code emails are left out of Claude's file. Only helper's own code reader uses them (see "Emailed codes").
- **The handoff:** `inbox_input.json` -> `inbox_output.json`, then `helper continue <run_id>`.
  - Each email has its candidate applications. Pick the one whose role the email names, or null.
  - `done: true` marks a step already taken, such as a submitted assessment.
  - Many emails can be classified in parallel by agents writing parts, then merged.
- **helper's rules for status changes:**
  - A status Vatsal set by hand (in helper or Notion) is never changed.
  - "applied", or a status an earlier email set, only moves forward: oa, phone_screen, interview, offer, rejected.
  - A confirmation email turns drafted, filled or submitted_unverified into applied, and the posting leaves the list.
- **Review:** `inbox_review: auto` (Vatsal, 2026-11-27): `helper continue` applies the changes at once, and Vatsal sees them in Notion and on the page. A wrong one gets `helper mark <id> <status>`. With `before_apply`, it pauses with exit 4 and lists every change; then run `helper continue <run_id> --approve-inbox` only after he approves.
- **When:** once a day, in the 10pm job hunt session (Vatsal, 2026-11-27).
- **To-dos:** assessments, interviews and other steps go on the list's "From your inbox" section (no links; the emails have them). `helper inbox done <id>` takes one off.
- **History:** the first check covered Nov 20 onward (Vatsal, 2026-11-27). Going back to September 2025 is his call.

## Emailed codes (Vatsal, 2026-11-27)
helper reads a site's emailed code or account link from Gmail itself (`src/helper/mailcodes.py`), using the same app password as the inbox.
- **What it handles:**
  - Oracle's "Confirm your identity" code: typed into the code boxes, then Verify.
  - Greenhouse's security code after Submit: typed into the code box.
  - Workday's "Verify your candidate account" link: opened in a second tab, then helper signs in.
  - Not iCIMS's codes, yet.
- **Matching:** only an email that arrived after the page asked, from that site, for this company.
  - Oracle: the sender's tenant matches the careers host (egup.fa.sender ↔ egup.fa...oraclecloud.com), or the email names the company.
  - Greenhouse: the company is named in the subject.
  - Workday: the link's host is the window's own host.
- **Once, quietly:** each code is used once, only on the page that asked. It's never printed, logged, saved or shown to Claude.
- **Fallback:** if no email comes within 4 minutes, or a second code page appears, the window waits for Vatsal as before.
- **Greenhouse after the code:** Greenhouse says "resubmit" after the code, so helper clicks Submit once more (Vatsal, 2026-11-27; the one exception to rule 10, only for Greenhouse's code).
  - It clicks once per run, recorded in the run's state as `helper_resubmitted_after_code`.
  - It clicks only after the code is typed; if no code came, nothing is clicked.
  - A submit held for a code was never sent, so it's never logged as submitted_unverified.
- **CAPTCHAs** stay his (rule 6).

## Tracker and Notion
- `helper.db` (SQLite) is the system of record, and it holds Vatsal's imported Notion history.
- Notion receives only sent applications (status applied or later), in the seven columns of `data/notion_mapping.yaml`. Never create Notion columns, never touch Deadline/Referral/Referral name, and never write test rows without asking.
- When Vatsal applies outside helper (the stopped sites, or by hand), run `helper mark <id> applied` once he says he submitted.

## Working conventions
- Python 3.12 venv at `.venv`. Run `.venv/bin/helper ...` and `.venv/bin/pytest`.
- The spec 13 build is complete (milestones 1 to 11, 2026-11-15). Commit only when Vatsal asks; pushing to `origin/main` is approved (2026-11-13). Run the tests before every commit.
- Several `helper fill` runs can go side by side (Vatsal, 2026-11-15): each claims its own browser profile slot (`~/.helper/browser-profile`, `-2`, `-3`... up to 6) and opens its own window. Start them a few seconds apart. Workday and iCIMS logins live in the first slot's cookies, so other slots sign in again. Resumes can be tailored in parallel too.
- The GitHub repo is public by Vatsal's choice. `data/profile.yaml` stays git-ignored.
- Never edit `data/master.yaml` by hand. It is generated by `helper convert`.
