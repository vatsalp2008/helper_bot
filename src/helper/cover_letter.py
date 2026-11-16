"""Cover letters (Vatsal's handoff, 2026-11-14): one for every resume.

helper writes cover_letter_input.json from the resume run (the same five projects and two experience entries, the
keyword audit and the posting). Claude writes cover_letter_output.json: four paragraphs. helper checks every fact the
way it checks resume bullets, renders templates/cover_letter.tex.j2 and saves resumes/<run_id>/<Name>_CoverLetter.pdf
next to the resume.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from .companies import same_company
from .config import Paths, load_yaml
from .errors import HelperError, WaitingForClaude
from .latex import compile_tex
from .models import Config, CoverLetterOutput, Job, MasterItem, Profile, TailorOutput
from .numbers import find_identifier_tokens, find_numbers, same_value
from .render import RenderError, display_header, make_env, resume_basename, write_tex
from .render import bullet as render_bullet
from .runs import load_state, read_json, rel, run_dir, save_state, set_step, write_json
from .techvocab import mask_comparisons
from .validate import (_BOLD_RE, _EM_DASH_RE, _EN_DASH_RE, _GPA_RE, _SENTENCE_START_RE, _TOKEN_RE, GENERIC_TOKENS,
                       Context, Report, RunOptions, in_text, load_context, number_key)
from .wording import violations

COVER_INPUT = "cover_letter_input.json"
COVER_OUTPUT = "cover_letter_output.json"
COVER_VALIDATION = "cover_letter_validation.json"
COVER_TEXT = "cover_letter.txt"
TEMPLATE = "cover_letter.tex.j2"
TARGET_WORDS = (400, 450)
WORD_LIMITS = (360, 480)
MAX_BOLD_PER_PARAGRAPH = 4
LETTER_WORDS = {"I'm", "I've", "I'd", "I'll", "Master's", "Masters", "Master", "Dear", "Hiring", "Team", "Sincerely",
                "Thank", "Thanks", "AI", "Computer", "Science", "Northeastern", "University", "Graduate", "Teaching",
                "Assistant", "Intern", "Internship"}

RULES = [
    "A letter, not sections: four paragraphs. helper adds the header, the salutation and the sign-off.",
    "Only facts from this resume's items below (its five projects and two experience entries, from master list v23). "
    "The letter tells the same story as the resume: no new tools, numbers, employers or claims, and nothing from items "
    "that aren't on this resume.",
    "Numbers keep the master list's precision and unit. Spelled-out numbers in prose are fine (\"three and a half "
    "years\"); metrics stay numerals (\"p99 under 50ms\").",
    "No em dashes, en dashes or --. Use commas or periods.",
    "Graduate Teaching Assistant: \"100+ students\"; never mention creating materials.",
    "chef/chef PR #16260 is always \"merged\". spiceai #12747 is a bug report that led to the CEO's own fix, never "
    "Vatsal's implementation.",
    "Use the resume's keyword audit: matched terms may appear; gap terms never do.",
    "Nonprofit work means BAPS Charities (Mumbai and Seattle); Northeastern is never a nonprofit Vatsal worked for. "
    "For customer-facing or forward-deployed roles, the Kalini internships can be told as work with external clients.",
    "Bold (**term**) the posting's key words: core technologies and posting terms the letter backs with a real example, "
    "and key metrics; at most 4 per paragraph (Vatsal, 2026-11-14).",
    "About 400 to 450 words across the four paragraphs (helper rejects fewer than 360 or more than 480). It must fit "
    "on one page.",
    "First person, warm, specific, professional; it reads like a person, not a template. Show, don't tell: every claim "
    "about how Vatsal works has a concrete example.",
]
STRUCTURE = [
    "Paragraph 1, why this role: name the exact role, then the one specific thing from the posting that draws Vatsal "
    "(quote a phrase from the posting when it truly fits how he works, and put that phrase in `hook`), then the working "
    "principle that connects him to it (for example measuring before trusting, or owning outcomes).",
    "Paragraph 2, main proof: the most relevant project or projects, with concrete numbers and what made the work "
    "hard. Depth over breadth: the problem, what he did, one or two real metrics, and the honest tradeoff or surprise "
    "when there is one.",
    "Paragraph 3, second proof: rigor and ownership, usually open-source merges (chef/chef, spiceai) when they are on "
    "this resume, evaluation work, or how he uses AI tools while reviewing everything they produce.",
    "Paragraph 4, close in one or two sentences: he's a Master's student in Computer Science at Northeastern "
    "University, he'd welcome the chance to bring that quality to the team, and thank you.",
]
TAILORING = [
    "Start from the keyword audit: work three to six of the posting's key terms into paragraphs 2 and 3, inside real "
    "sentences. No keyword stuffing.",
    "Mirror the company's own values only with a real example behind them.",
    "Leave gaps out of the letter.",
    "Go deep on two or three of this resume's items (list them in `stories`) rather than listing all five projects.",
]
STORY_GUIDE = [
    {"role_type": "Agentic AI, AI engineering, forward deployed", "main": "theManager and SWARM",
     "supporting": "EchoCoach evaluation, plus one open-source merge"},
    {"role_type": "ML, AI evaluation, emerging tech", "main": "EchoCoach (eval harness, LoRA, quantization)",
     "supporting": "theManager and SWARM"},
    {"role_type": "Backend, distributed, platform", "main": "E-commerce Platform and StreamLine",
     "supporting": "chef/chef and spiceai merges, plus the Claude Code workflow"},
    {"role_type": "Data science and analytics", "main": "ExperimentIQ", "supporting": "Traffic Accident or PNWER"},
    {"role_type": "Customer-facing, consulting, forward deployed", "main": "the Kalini internships with external "
     "clients, plus PNWER client delivery", "supporting": "one technical story with metrics"},
    {"role_type": "Campus, nonprofit, community", "main": "Graduate Teaching Assistant (100+ students), Event "
     "Assistant, BAPS Charities (Mumbai and Seattle)", "supporting": "one data or technical story"},
]
OUTPUT_SCHEMA = {
    "hook": "the exact phrase from the posting that paragraph 1 quotes, or null",
    "stories": ["two or three item ids from `items` that the letter goes deep on"],
    "paragraphs": ["paragraph 1", "paragraph 2", "paragraph 3", "paragraph 4"],
    "flags": ["anything Vatsal should look at"],
}
_MATERIALS_RE = re.compile(r"(?i)\b(supplement\w*|extra|additional|new)\s+(teaching\s+|course\s+|study\s+)?(materials?|"
                           r"resources?|worksheets?)\b|\b(created|made|developed|wrote|prepared|designed)\b[^.]{0,40}"
                           r"\b(materials?|worksheets?|study guides?)\b")
_QUOTED_RE = re.compile(r"[\"“]([^\"”]{3,200})[\"”]")


def is_northeastern_campus(job: Job) -> bool:
    return same_company(job.company, "Northeastern University") or bool(re.search(r"(?i)\bnortheastern\b", job.company))


def salutation(job: Job) -> str:
    """Always the team, never one person (Vatsal, 2026-11-14); Northeastern campus jobs address the career team."""
    return "Dear Career Team," if is_northeastern_campus(job) else "Dear Hiring Team,"


def letter_basename(name: str) -> str:
    return resume_basename(name).removesuffix("_Resume") + "_CoverLetter"


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’+#./%-]*", text.replace("**", "")))


# --------------------------------------------------------------------------- the handoff


def write_input(paths: Paths, state: Any) -> Path:
    folder = run_dir(paths, state.run_id)
    job = Job.model_validate(read_json(folder / "job.json"))
    tailor = TailorOutput.model_validate(read_json(folder / "tailor_output.json"))
    tailor_input = read_json(folder / "tailor_input.json")
    pool = {i["id"]: i for i in tailor_input.get("experience", []) + tailor_input.get("projects", [])}
    keep = ("id", "kind", "display_name", "title", "organization", "heading", "status", "dates", "allowed_tech",
            "framing_additions", "variants", "allowed_numbers", "allowed_tokens", "wording_rules", "text")
    wanted = [e.entry_id for e in tailor.experience] + [p.project_id for p in tailor.projects]
    campus = is_northeastern_campus(job)
    data: dict[str, Any] = {
        "run_id": state.run_id,
        "write_to": rel(paths, folder / COVER_OUTPUT),
        "then_run": f"helper continue {state.run_id}",
        "rules": RULES,
        "structure": STRUCTURE,
        "tailoring": TAILORING,
        "story_guide": {"note": "Suggestions by role type; use a story only when its item is on this resume.",
                        "rows": STORY_GUIDE},
        "salutation": salutation(job),
        "sign_off": "Sincerely, then the name" + (" and his NUID (helper adds them)" if campus else " (helper adds them)"),
        "northeastern_campus_job": campus,
        "job": {"company": job.company, "role_title": job.role_title, "location": job.location,
                "description_text": job.description_text},
        "resume": {
            "role_type": tailor.role_type, "grad_date": tailor.grad_date,
            "experience": [e.model_dump() for e in tailor.experience],
            "projects": [p.model_dump() for p in tailor.projects],
            "skills_lines": [s.model_dump() for s in tailor.skills_lines],
            "keyword_audit": tailor.keyword_audit.model_dump(), "fit_paragraph": tailor.fit_paragraph,
        },
        "items": [{k: pool[i][k] for k in keep if k in pool[i]} for i in wanted if i in pool],
        "output_schema": OUTPUT_SCHEMA,
    }
    examples = paths.data / "cover_letter_examples.md"  # Vatsal's approved letters; git-ignored
    if examples.is_file():
        data["approved_examples"] = examples.read_text(encoding="utf-8")
    path = folder / COVER_INPUT
    write_json(path, data)
    return path


def _waiting(paths: Paths, run_id: str, note: str) -> WaitingForClaude:
    folder = run_dir(paths, run_id)
    return WaitingForClaude(run_id, rel(paths, folder / COVER_INPUT), rel(paths, folder / COVER_OUTPUT), note)


def cover_letter_step(paths: Paths, cfg: Config, run_id: str, *, redo: bool = False) -> Optional[str]:
    """Advance the run's cover letter. Returns a message when the letter is built, None when there's nothing to do
    (no resume yet, or the letter was built earlier). Raises WaitingForClaude while Claude has work to do."""
    state = load_state(paths, run_id)
    folder = run_dir(paths, run_id)
    if not (state.steps.get("render") and state.steps["render"].status == "done"):
        return None
    step = state.steps.get("cover_letter")
    if step and step.status == "done" and not redo:
        return None
    if redo or not (folder / COVER_INPUT).is_file():
        (folder / COVER_OUTPUT).unlink(missing_ok=True)
        write_input(paths, state)
        set_step(state, "cover_letter", "waiting_for_claude", f"write {COVER_OUTPUT}")
        save_state(paths, state)
        raise _waiting(paths, run_id, "Next, the cover letter for this resume.")
    out_path = folder / COVER_OUTPUT
    if not out_path.is_file():
        raise _waiting(paths, run_id, f"{COVER_OUTPUT} isn't there yet.")
    report = Report()
    try:
        out = CoverLetterOutput.model_validate_json(out_path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        for err in exc.errors()[:10]:
            report.error("schema", ".".join(str(p) for p in err["loc"]) or COVER_OUTPUT, err["msg"])
        _reject(paths, state, report)
        raise AssertionError("unreachable")  # pragma: no cover
    job = Job.model_validate(read_json(folder / "job.json"))
    tailor = TailorOutput.model_validate(read_json(folder / "tailor_output.json"))
    ctx = load_context(paths, cfg, RunOptions.model_validate(state.options["run"]))
    profile = Profile.model_validate(load_yaml(paths.profile))
    items = [ctx.master.item(i) for i in [e.entry_id for e in tailor.experience] + [p.project_id for p in tailor.projects]]
    report = validate_letter(out, ctx, items, job, tailor, profile)
    if not report.ok:
        _reject(paths, state, report)
    pdf, words = _build(paths, cfg, state, job, profile, out, report)
    lines = [f"Cover letter: {rel(paths, pdf)} ({words} words, 1 page)",
             f"  Stories: {', '.join(out.stories)}" + (f"; hook: \"{out.hook}\"" if out.hook else "")]
    lines += [f"  [warning] {w.where}: {w.message}" for w in report.warnings]
    lines += [f"  Flag: {f}" for f in out.flags]
    return "\n".join(lines)


def _reject(paths: Paths, state: Any, report: Report) -> None:
    folder = run_dir(paths, state.run_id)
    write_json(folder / COVER_VALIDATION, report.to_json())
    set_step(state, "cover_letter", "waiting_for_claude", f"{len(report.errors)} problem(s) to fix")
    save_state(paths, state)
    note = (f"{COVER_OUTPUT} failed its checks: {len(report.errors)} problem(s). Details: "
            f"{rel(paths, folder / COVER_VALIDATION)}\n" + "\n".join(f"  {issue}" for issue in report.errors))
    raise _waiting(paths, state.run_id, note)


def _build(paths: Paths, cfg: Config, state: Any, job: Job, profile: Profile, out: CoverLetterOutput,
           report: Report) -> tuple[Path, int]:
    folder = run_dir(paths, state.run_id)
    nuid = profile.tier1.nuid if is_northeastern_campus(job) else None
    env = make_env(paths.templates)
    source = env.get_template(TEMPLATE).render(header=display_header(profile.header), salutation=salutation(job),
                                               paragraphs=out.paragraphs, nuid=nuid)
    base = letter_basename(profile.header.name)
    tex_path = write_tex(source, folder / f"{base}.tex")
    compiled = compile_tex(tex_path, engine=cfg.latex_engine)
    if not compiled.ok:
        raise HelperError(f"LaTeX failed on {rel(paths, tex_path)}: {'; '.join(compiled.errors[:3])}")
    if compiled.pages != 1:
        report.error("length", "letter", f"runs to {compiled.pages} pages; shorten it to fit one page")
        _reject(paths, state, report)
    out_dir = paths.resumes / state.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / f"{base}.pdf"
    shutil.copyfile(compiled.pdf, pdf)  # type: ignore[arg-type]
    shutil.copyfile(tex_path, out_dir / f"{base}.tex")
    text = plain_letter(salutation(job), out.paragraphs, profile.header.name, nuid)
    (folder / COVER_TEXT).write_text(text, encoding="utf-8")
    words = sum(word_count(p) for p in out.paragraphs)
    write_json(folder / COVER_VALIDATION, report.to_json())
    state.options["cover_letter"] = {"pdf": rel(paths, pdf), "text": rel(paths, folder / COVER_TEXT), "words": words,
                                     "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "stories": out.stories}
    set_step(state, "cover_letter", "done", rel(paths, pdf))
    save_state(paths, state)
    return pdf, words


def plain_letter(greeting: str, paragraphs: list[str], name: str, nuid: Optional[str]) -> str:
    """The letter as plain text, for forms that ask for it in a text box."""
    body = "\n\n".join(" ".join(p.replace("**", "").split()) for p in paragraphs)
    return f"{greeting}\n\n{body}\n\nSincerely,\n{name}" + (f"\nNUID: {nuid}" if nuid else "") + "\n"


# --------------------------------------------------------------------------- the checks


def validate_letter(out: CoverLetterOutput, ctx: Context, items: list[MasterItem], job: Job, tailor: TailorOutput,
                    profile: Profile) -> Report:
    r = Report()
    ids = [i.id for i in items]
    if len(out.paragraphs) != 4:
        r.error("structure", "paragraphs", f"{len(out.paragraphs)} paragraphs; exactly 4 (why this role, main proof, "
                                           "second proof, close)")
    unknown = [s for s in out.stories if s not in ids]
    if unknown:
        r.error("structure", "stories", f"{', '.join(unknown)} isn't on this resume; stories come from {', '.join(ids)}")
    if not 2 <= len(out.stories) <= 3:
        r.error("structure", "stories", f"{len(out.stories)} stories; go deep on two or three")
    posting = " ".join(job.description_text.split()).lower()
    paragraphs = out.paragraphs
    if out.hook:
        hook = " ".join(out.hook.split()).lower().strip("\"“”. ")
        if hook not in posting:
            r.error("hook", "hook", f"'{out.hook}' isn't in the posting; quote the posting's own words")
        elif paragraphs and hook not in " ".join(paragraphs[0].replace("**", "").split()).lower():
            r.error("hook", "paragraph 1", "paragraph 1 must quote the hook")
    words = sum(word_count(p) for p in paragraphs)
    if not WORD_LIMITS[0] <= words <= WORD_LIMITS[1]:
        r.error("length", "letter", f"{words} words; write about {TARGET_WORDS[0]} to {TARGET_WORDS[1]}")
    elif not TARGET_WORDS[0] <= words <= TARGET_WORDS[1]:
        r.warn("length", "letter", f"{words} words; the target is {TARGET_WORDS[0]} to {TARGET_WORDS[1]}")
    # Names the posting itself uses (its team, products, places) aren't claims; tools are still checked as tools.
    extra = " ".join([job.company, job.role_title, job.description_text, profile.header.location,
                      *(f"{e.school} {e.degree} {e.major} {e.location}" for e in profile.education)])
    others = _other_item_names(ctx, ids)
    on_resume = _resume_tech(tailor, ctx)
    # The role title's own numbers ("Summer 2027") and the graduation date may be named; they aren't claims.
    given = [number_key(m) for m in find_numbers(f"{job.role_title} {tailor.grad_date}")]
    for n, paragraph in enumerate(paragraphs, 1):
        _check_paragraph(paragraph, n, items, ctx, job, tailor, extra, posting, others, on_resume, r, given)
    if len(paragraphs) == 4:
        def words(text: str) -> str:  # dashes and commas as word breaks: a title's en dash can't be in a letter (rule 4)
            return " ".join(re.sub(r"[\u2010-\u2015,\-]", " ", text).lower().split())

        core = words(re.sub(r"\s*[\(\[].*?[\)\]]", "", job.role_title))
        if core and core not in words(paragraphs[0].replace("**", "")):
            r.warn("structure", "paragraph 1", f"name the role ('{job.role_title}')")
        close = paragraphs[3].replace("**", "")
        if not (re.search(r"\bMaster", close) and "Northeastern" in close):
            r.error("structure", "paragraph 4", "close with: a Master's student in Computer Science at Northeastern "
                                                "University, welcome the chance, and thanks")
    return r


def _other_item_names(ctx: Context, on_resume: list[str]) -> dict[str, str]:
    """Names of projects that aren't on this resume, so the letter doesn't bring them in."""
    names = {}
    for item in ctx.master.items():
        if item.id in on_resume or item.kind != "project":
            continue
        display = ctx.meta.project_display_names.get(item.id)
        for name in [display, *({"chef": ["chef/chef"], "spiceai": ["spiceai", "Spice.ai"]}.get(item.id, []))]:
            if name and len(name) > 2:
                names[name] = item.id
    return names


def _resume_tech(tailor: TailorOutput, ctx: Context) -> set[str]:
    """Technologies the resume itself names: in bullets, tags and skills lines."""
    strings = [b for e in tailor.experience for b in e.bullets]
    strings += [b for p in tailor.projects for b in [*p.bullets, *p.tech_tags]]
    strings += [i for line in tailor.skills_lines for i in line.items]
    return {m.canonical for text in strings for m in ctx.vocab.find(mask_comparisons(text.replace("**", "")))}


def _check_paragraph(paragraph: str, n: int, items: list[MasterItem], ctx: Context, job: Job, tailor: TailorOutput,
                     extra: str, posting: str, others: dict[str, str], on_resume: set[str], r: Report,
                     given: Optional[list[Any]] = None) -> None:
    where = f"paragraph {n}"
    if _EM_DASH_RE.search(paragraph) or _EN_DASH_RE.search(paragraph):
        r.error("dashes", where, "no em dashes, en dashes or --; use a comma or a period")
        return
    if _GPA_RE.search(paragraph):
        r.error("gpa", where, "GPA never appears (rule 3)")
    bolds = _BOLD_RE.findall(paragraph)
    if len(bolds) > MAX_BOLD_PER_PARAGRAPH:
        r.error("bold", where, f"{len(bolds)} bold phrases; at most {MAX_BOLD_PER_PARAGRAPH}")
    for phrase in bolds:
        if not (re.search(r"\d", phrase) or ctx.vocab.find(phrase) or in_text(phrase, job.description_text)):
            r.warn("bold", where, f"'{phrase}' is bold but isn't a posting term, a technology or a metric")
    try:
        render_bullet(paragraph)
    except RenderError as exc:
        r.error("render", where, str(exc))
    plain = paragraph.replace("**", "")
    checked = _QUOTED_RE.sub(lambda m: " " * len(m.group(0)) if " ".join(m.group(1).split()).lower().strip(". ")
                             in posting else m.group(0), plain)  # quoting the posting isn't a claim
    # Facts belong to the item a sentence is about, as on the resume: a sentence naming no item continues the last
    # one named in the paragraph; before any is named, any of the resume's items may back it.
    current: list[MasterItem] = []
    for sentence in re.split(r"(?<=[.!?])\s+", checked):
        named = [i for i in items if _mentions(i, sentence, ctx)]
        current = named or current
        about = current or items
        label = ", ".join(i.id for i in about) if len(about) < len(items) else "this resume's items"
        _check_numbers(sentence, about, ctx, where, r, label, given or [])
        for token in sorted(find_identifier_tokens(sentence)):
            if ctx.vocab.canonical(token) or any(token.lower() in {t.lower() for t in i.allowed_tokens}
                                                 or in_text(token, ctx.item_text(i)) for i in about):
                continue
            r.error("numbers", where, f"'{token}' isn't in {label}")
        for m in ctx.vocab.find(mask_comparisons(sentence)):
            if not any(m.canonical in i.allowed_tech for i in about) and not (
                    about is items and m.canonical in on_resume):
                r.error("tech", where, f"'{m.text}' ({m.canonical}) isn't a technology {label} used")
    spans = [(m.start, m.end) for m in ctx.vocab.find(mask_comparisons(checked))]
    texts = [ctx.item_text(i) for i in items] + [extra]
    for m in _TOKEN_RE.finditer(checked):
        token = m.group().rstrip(".-/'&_")
        if not token or not any(c.isupper() for c in token) or any(c.isdigit() for c in token):
            continue
        if _SENTENCE_START_RE.search(checked[: m.start()]) or any(s <= m.start() < e for s, e in spans):
            continue
        if token in GENERIC_TOKENS or token in LETTER_WORDS or any(in_text(token, t) for t in texts):
            continue
        r.error("names", where, f"'{token}' isn't in this resume's items; name only what they involved")
    for item in items:
        always = item.id in ("chef", "spiceai", "ta")  # their rules key on PR numbers and the TA title
        if always or _mentions(item, plain, ctx):
            for rule in item.wording_rules:
                if rule.scope == "bullet":
                    for v in violations(rule, plain):
                        r.error("wording", where, v)
    for trigger, name in ((r"(?i)16260|chef/chef", "the chef/chef fix"), (r"13507", "spiceai PR #13507")):
        if re.search(trigger, plain) and not re.search(r"(?i)\bmerged\b", plain):
            r.error("wording", where, f"{name} is always described as merged")
    if re.search(r"(?i)northeastern[^.]{0,80}non-?profit|non-?profit[^.]{0,80}northeastern", plain):
        r.error("wording", where, "Northeastern is never described as a nonprofit (BAPS Charities is the nonprofit)")
    if _MATERIALS_RE.search(plain):
        r.error("wording", where, "the TA work never mentions creating materials")
    for term in tailor.keyword_audit.gaps:
        if term.strip().lower() == job.company.strip().lower():
            continue  # the company's own name is never a gap (ServiceNow, 2026-11-16)
        if in_text(term, plain):
            r.error("gaps", where, f"'{term}' is a gap in the keyword audit; leave it out")
    for name, item_id in others.items():
        if re.search(rf"(?<![\w/]){re.escape(name)}(?![\w/])", plain):
            r.error("items", where, f"'{name}' ({item_id}) isn't on this resume")


_ALIASES = {"chef": ["chef/chef", "Chef Infra"], "spiceai": ["spiceai", "Spice.ai"], "ta": ["Teaching Assistant"],
            "kalini_sde": ["Kalini"], "kalini_ml": ["Kalini"]}


def _mentions(item: MasterItem, text: str, ctx: Context) -> bool:
    display = ctx.meta.project_display_names.get(item.id) or ""
    names = [display, item.name, item.organization, *_ALIASES.get(item.id, [])]
    first = display.split()[0] if display else ""
    if len(first) >= 5 and any(c.isupper() for c in first[1:]):  # a distinctive short name: PNWER, EchoCoach, theManager
        names.append(first)
    return any(name and len(name) > 3 and name.lower() in text.lower() for name in names)


_WORD_NUMBERS = {w: n for n, w in enumerate("one two three four five six seven eight nine ten".split(), 1)}


def prose_numbers(text: str) -> str:
    """Letters say numbers the way people do; read them back as the master list writes them: "three and a half" is
    3.5, "under 50ms" is <50ms, "more than 100" is 100+, "about 200" is ~200."""
    text = re.sub(r"(?i)\b(one|two|three|four|five|six|seven|eight|nine|ten) and a half\b",
                  lambda m: f"{_WORD_NUMBERS[m.group(1).lower()]}.5", text)
    text = re.sub(r"(?i)\b(?:under|below|less than|fewer than)\s+(?=\$?\d)", "<", text)
    text = re.sub(r"(?i)\bmore than\s+(\$?\d[\d,]*(?:\.\d+)?)", r"\1+", text)  # not "over": "a pipeline over 5 sources"
    return re.sub(r"(?i)\b(?:about|around|roughly|approximately|nearly)\s+(?=\$?\d)", "~", text)


def _check_numbers(text: str, items: list[MasterItem], ctx: Context, where: str, r: Report,
                   label: str = "this resume's items", given: Optional[list[Any]] = None) -> None:
    allowed = [a for item in items for a in ctx.number_keys(item)] + list(given or [])
    for mention in find_numbers(prose_numbers(text)):
        key = number_key(mention)
        same = [a for a in allowed if same_value(a.value, key.value) and a.unit == key.unit]
        if not same:
            r.error("numbers", where, f"'{mention.raw}' isn't a number in {label} (rule 2)")
            continue
        if not any(a.marker == key.marker for a in same):
            forms = " or ".join(sorted({a.raw for a in same}))
            r.error("numbers", where, f"'{mention.raw}' changes the master list's precision; write {forms}")
