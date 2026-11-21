"""Validate tailor_output.json against the master list and the rules, then check the compiled PDF.

Spec 7.7, plus the plan's extra checks: bullets render on at most 2 lines, no dashes in bullets, precision markers
kept, the spec 7.4 metric rules (as wording rules from the converter), experience titles exactly as in the master
list, and technologies tied to the item that used them (or an approved family swap, which replaces, never adds).
"""

from __future__ import annotations

import functools
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from pydantic import Field

from .config import Paths, load_yaml
from .convert import load_lexicon, vocabulary_for
from .errors import HelperError
from .latex import CompileResult, find_binary
from .master import load_master
from .models import (
    Config,
    CoursesFile,
    MasterData,
    MasterItem,
    ProjectSetsFile,
    ResumeMetaFile,
    SkillSynonymsFile,
    StrictModel,
    TailorOutput,
    TechFamiliesFile,
)
from .numbers import NumberMention, find_identifier_tokens, find_numbers, same_value
from .render import RenderError, ResumeContent
from .render import bullet as render_bullet
from .render import tex as render_text
from .techvocab import TechVocabulary, mask_comparisons
from .wording import violations

KALINI = ("kalini_sde", "kalini_ml")
KALINI_BY_TRACK = {"sde": "kalini_sde", "ml": "kalini_ml"}
OPEN_SOURCE_PROJECTS = ("chef", "spiceai")
MAX_BOLD = 4
MAX_BULLET_LINES = 2
TA_PREFIX = "Graduate Teaching Assistant"

# Name-like tokens that are never claims about what an item used.
GENERIC_TOKENS = {
    "API", "APIs", "CEO", "CTO", "PR", "PRs", "UI", "UX", "US", "ID", "IDs", "OSS", "I", "SDK", "CLI", "JSON",
    "YAML", "CSV", "HTTP", "HTTPS", "URL", "URLs", "CPU", "GPU", "RAM", "QA", "CRUD", "README",
}

_EM_DASH_RE = re.compile(r"---|[\u2014\u2015]")
_EN_DASH_RE = re.compile(r"--|\u2013")
_GPA_RE = re.compile(r"(?i)\bGPA\b|\bgrade point\b|\bCGPA\b|\b[0-4]\.\d{1,2}\s*/\s*4(?:\.0+)?\b")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_QUAL_FLAG_RE = re.compile(r"(?i)^\s*qualitative:\s*(?P<item>[a-z0-9_]+)\s+bullet\s+(?P<n>\d+)\s*:\s*(?P<why>\S.*)$")
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9+#.&'/_-]*")  # not the M of 10M+
_SENTENCE_START_RE = re.compile(r"(?:^|[.!?:]\s+|\(\s*)$")
_NUM_PARTS_RE = re.compile(
    r"^(?P<marker>[~<>≤≥])?\s?(?P<sign>[+\-−])?(?P<dollar>\$)?(?P<digits>\d[\d,]*(?:\.\d+)?)(?P<suffix>[A-Za-z%]*)(?P<plus>\+)?$"
)
_SCALE_SUFFIXES = {"k", "K", "M", "B", "T"}


class RunOptions(StrictModel):
    """What this run requires of the tailor output (stored in state.json)."""

    projects_per_resume: int
    bullets_per_item: int
    grad_date: str
    include_experience: list[str] = Field(default_factory=list)
    require_open_source: bool = False
    open_source_signals: list[str] = Field(default_factory=list)


@dataclass
class Issue:
    check: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.check}] {self.where}: {self.message}"


@dataclass
class Report:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    keyword_match_pct: Optional[float] = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, check: str, where: str, message: str) -> None:
        self.errors.append(Issue(check, where, message))

    def warn(self, check: str, where: str, message: str) -> None:
        self.warnings.append(Issue(check, where, message))

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [vars(i) for i in self.errors],
            "warnings": [vars(i) for i in self.warnings],
            "keyword_match_pct": self.keyword_match_pct,
        }


# --------------------------------------------------------------------------- context


@functools.lru_cache(maxsize=2048)
def phrase_regex(phrase: str) -> re.Pattern[str]:
    """Case-insensitive whole-phrase match; hyphens and spaces count as the same; an added plural s is fine. Postings'
    own hyphen characters (U+2010, and U+2011 in "AI‑assisted", Ralliant's Oracle posting, 2026-11-20) are hyphens."""
    parts = [re.escape(p) for p in re.split(r"[\s\-\u2010\u2011]+", phrase.strip()) if p]
    body = r"[\s\-\u2010\u2011]+".join(parts) if parts else re.escape(phrase)
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?:s|es)?(?![A-Za-z0-9])", re.IGNORECASE)


def in_text(phrase: str, text: str) -> bool:
    phrase = re.sub(r"(?:'s|’s)$", "", phrase.strip())
    if not phrase:
        return True
    if phrase_regex(phrase).search(text):
        return True
    if phrase.endswith("s") and len(phrase) > 3 and phrase_regex(phrase[:-1]).search(text):
        return True
    # "PyTorch/TensorFlow" names two things, so both being there counts. A hyphen joins one name, and phrase_regex
    # already reads hyphens and spaces alike: "scikit-image" isn't Scikit-learn plus an "image" (Syngenta, 2026-11-18)
    parts = [p for p in phrase.split("/") if p.strip()]
    return len(parts) > 1 and all(phrase_regex(p).search(text) for p in parts)


@dataclass(frozen=True)
class NumKey:
    value: float
    unit: str  # "%", "ms", "s", "x", "$", "" (K/M/B fold into the value)
    marker: str  # "~", "<", ">", "≤", "≥", "+", ""
    sign: str  # "+", "-", ""
    raw: str


def number_key(n: NumberMention) -> NumKey:
    if n.is_word:
        return NumKey(n.value, "", "", "", n.raw)
    m = _NUM_PARTS_RE.match(n.raw)
    if not m:
        return NumKey(n.value, "", n.marker, "", n.raw)
    suffix = m.group("suffix")
    unit = "" if suffix in _SCALE_SUFFIXES else ("x" if suffix == "X" else suffix)
    if m.group("dollar"):
        unit = "$" + unit
    sign = (m.group("sign") or "").replace("−", "-")
    return NumKey(n.value, unit, n.marker, sign, n.raw)


@dataclass
class Context:
    master: MasterData
    vocab: TechVocabulary
    meta: ResumeMetaFile
    courses: list[str]
    project_sets: ProjectSetsFile
    synonyms: SkillSynonymsFile
    families: TechFamiliesFile
    run: RunOptions
    _cache: dict[str, Any] = field(default_factory=dict)

    def item_text(self, item: MasterItem) -> str:
        key = f"text:{item.id}"
        if key not in self._cache:
            parts: list[str] = [item.heading, item.name, item.subtitle or "", item.category or "", item.status or "",
                                item.timeline or "", item.organization or "", item.location or ""]
            parts += item.technologies + list(item.fields.values()) + item.bullets() + item.notes + item.variants
            parts += [m for ms in item.metrics.values() for m in ms]
            parts.append(self.meta.project_display_names.get(item.id, ""))
            em = self.meta.experience.get(item.id)
            if em and em.resume_title:
                parts.append(em.resume_title)
            self._cache[key] = "\n".join(p for p in parts if p)
        return self._cache[key]  # type: ignore[no-any-return]

    def number_keys(self, item: MasterItem) -> list[NumKey]:
        key = f"nums:{item.id}"
        if key not in self._cache:
            self._cache[key] = [number_key(n) for raw in item.allowed_numbers for n in find_numbers(raw)]
        return self._cache[key]  # type: ignore[no-any-return]

    @functools.cached_property
    def all_tech(self) -> set[str]:
        return {t for item in self.master.items() for t in item.allowed_tech}

    @functools.cached_property
    def skill_canonicals(self) -> set[str]:
        return {self.vocab.canonical(n) or n for g in self.master.skills for i in g.all_items() for n in [i.name, *i.aliases]}

    @functools.cached_property
    def master_text(self) -> str:
        """The Skills section and every project and experience entry: what a skills line or a gap is checked against."""
        parts: list[str] = []
        for group in self.master.skills:
            for g in [group, *group.subgroups]:
                parts.append(g.name)
                parts += g.notes
                for i in g.items:
                    parts += [i.name, i.note or "", *i.aliases]
        parts += [self.item_text(item) for item in self.master.items()]
        return "\n".join(p for p in parts if p)

    def canonical_of(self, term: str) -> Optional[str]:
        """The technology a whole term names, if it is one."""
        canonical = self.vocab.canonical(term)
        if canonical:
            return canonical
        matches = self.vocab.find(term)
        if len(matches) == 1 and matches[0].text.strip().lower() == term.strip().lower():
            return matches[0].canonical
        return None

    def swap_natives(self, canonical: str, item: MasterItem) -> list[str]:
        """Members of approved families that `canonical` may stand in for in this item. Projects only: experience
        entries are real jobs and keep the tools they used (Vatsal, 2026-11-14)."""
        if item.kind != "project":
            return []
        return sorted({
            m for f in self.families.approved() if canonical in f.members
            for m in f.members if m != canonical and m in item.allowed_tech
        })


def load_context(paths: Paths, cfg: Config, run: RunOptions) -> Context:
    master = load_master(paths, cfg)
    vocab = vocabulary_for(master, load_lexicon(paths))
    synonyms = SkillSynonymsFile.model_validate(load_yaml(paths.skill_synonyms) or {}) if paths.skill_synonyms.is_file() \
        else SkillSynonymsFile()
    families = TechFamiliesFile.model_validate(load_yaml(paths.tech_families) or {}) if paths.tech_families.is_file() \
        else TechFamiliesFile()
    ctx = Context(
        master=master,
        vocab=vocab,
        meta=ResumeMetaFile.model_validate(load_yaml(paths.resume_meta)),
        courses=CoursesFile.model_validate(load_yaml(paths.courses)).courses,
        project_sets=ProjectSetsFile.model_validate(load_yaml(paths.project_sets)),
        synonyms=synonyms,
        families=families,
        run=run,
    )
    known = ctx.all_tech | ctx.skill_canonicals
    for family in families.approved():
        missing = [m for m in family.members if m not in known]
        if missing:
            raise HelperError(f"tech_families.yaml: {family.id} names {', '.join(missing)}, which the master list doesn't have")
    return ctx


# --------------------------------------------------------------------------- tailor_output.json


def validate_tailor(out: TailorOutput, ctx: Context) -> Report:
    report = Report()
    _check_structure(out, ctx, report)
    _check_strings(out, report)
    known = {i.id: i for i in ctx.master.items()}
    for e in out.experience:
        item = known.get(e.entry_id)
        if item is None or item.kind != "experience":
            continue
        _check_experience_title(e.title, item, ctx, report)
        for n, b in enumerate(e.bullets, 1):
            _check_bullet(b, n, item, ctx, out.flags, report)
        _check_entry(item, [e.title, *e.bullets], ctx, report)
    for p in out.projects:
        item = known.get(p.project_id)
        if item is None or item.kind != "project":
            continue
        where = f"{item.id} subtitle"
        _check_numbers(p.subtitle, item, where, ctx, report)
        _check_tech(p.subtitle, item, where, ctx, report)
        _check_tags(p.tech_tags, item, ctx, report)
        for n, b in enumerate(p.bullets, 1):
            _check_bullet(b, n, item, ctx, out.flags, report)
        _check_entry(item, [p.subtitle, ", ".join(p.tech_tags), *p.bullets], ctx, report)
    _check_skills(out, ctx, report)
    _check_audit(out, ctx, report)
    _check_flags(out, ctx, report)
    return report


def _check_structure(out: TailorOutput, ctx: Context, r: Report) -> None:
    run = ctx.run
    role = ctx.project_sets.role_types.get(out.role_type)
    if role is None:
        r.error("structure", "role_type", f"'{out.role_type}' is not in project_sets.yaml "
                f"({', '.join(ctx.project_sets.role_types)})")
    if out.grad_date != run.grad_date:
        r.error("grad_date", "grad_date", f"'{out.grad_date}', but this run uses {run.grad_date} (spec 8 item 4)")

    known = {i.id: i for i in ctx.master.items()}
    exp_ids = [e.entry_id for e in out.experience]
    for eid in exp_ids:
        item = known.get(eid)
        if item is None:
            r.error("structure", f"experience {eid}", "no such entry in master.yaml")
        elif item.kind != "experience":
            r.error("structure", f"experience {eid}", "this is a project, not an experience entry")
    for dup in sorted({x for x in exp_ids if exp_ids.count(x) > 1}):
        r.error("structure", f"experience {dup}", "listed more than once")
    if "ta" not in exp_ids:
        r.error("experience", "experience", "the Graduate Teaching Assistant entry (ta) is always included (spec 7.1)")
    kalini = [x for x in exp_ids if x in KALINI]
    if len(kalini) != 1:
        r.error("experience", "experience", f"exactly one Kalini internship (kalini_sde or kalini_ml); found {len(kalini)}")
    elif role is not None and role.kalini and kalini[0] != KALINI_BY_TRACK[role.kalini]:
        r.error("experience", f"experience {kalini[0]}", f"the {role.label} track uses {KALINI_BY_TRACK[role.kalini]} (spec 7.1)")
    for x in exp_ids:
        if x in known and x != "ta" and x not in KALINI and x not in run.include_experience:
            r.error("experience", f"experience {x}", "off resumes by default; add it with --include-experience")
    for x in run.include_experience:
        if x not in exp_ids:
            r.error("experience", f"experience {x}", "requested with --include-experience but missing")
    for e in out.experience:
        if len(e.bullets) != run.bullets_per_item:
            r.error("structure", f"experience {e.entry_id}", f"{len(e.bullets)} bullets; exactly {run.bullets_per_item}")

    proj_ids = [p.project_id for p in out.projects]
    if len(proj_ids) != run.projects_per_resume:
        r.error("structure", "projects", f"{len(proj_ids)} projects; exactly {run.projects_per_resume}")
    for pid in proj_ids:
        item = known.get(pid)
        if item is None:
            r.error("structure", f"project {pid}", "no such project in master.yaml")
        elif item.kind != "project":
            r.error("structure", f"project {pid}", "this is an experience entry, not a project")
    for dup in sorted({x for x in proj_ids if proj_ids.count(x) > 1}):
        r.error("structure", f"project {dup}", "listed more than once")
    for p in out.projects:
        if len(p.bullets) != run.bullets_per_item:
            r.error("structure", f"project {p.project_id}", f"{len(p.bullets)} bullets; exactly {run.bullets_per_item}")
        if not p.subtitle.strip():
            r.error("structure", f"project {p.project_id}", 'needs a subtitle ("Name: Subtitle")')
    if run.require_open_source and not any(pid in OPEN_SOURCE_PROJECTS for pid in proj_ids):
        r.error("projects", "projects", f"the posting values {', '.join(run.open_source_signals)}, so include chef or "
                "spiceai (spec 7.2)")

    if len(out.coursework) != 5:
        r.error("coursework", "coursework", f"{len(out.coursework)} courses; exactly 5")
    for course in out.coursework:
        if course not in ctx.courses:
            r.error("coursework", "coursework", f"'{course}' is not in courses.yaml")
    for dup in sorted({c for c in out.coursework if out.coursework.count(c) > 1}):
        r.error("coursework", "coursework", f"'{dup}' is listed twice")


def _all_strings(out: TailorOutput) -> Iterator[tuple[str, str, bool, bool]]:
    """(where, text, is_bullet, on_resume) for every string helper will print or show."""
    for e in out.experience:
        yield f"experience {e.entry_id} title", e.title, False, True
        for n, b in enumerate(e.bullets, 1):
            yield f"{e.entry_id} bullet {n}", b, True, True
    for p in out.projects:
        yield f"{p.project_id} subtitle", p.subtitle, False, True
        for t in p.tech_tags:
            yield f"{p.project_id} tag", t, False, True
        for n, b in enumerate(p.bullets, 1):
            yield f"{p.project_id} bullet {n}", b, True, True
        if p.reason:
            yield f"{p.project_id} reason", p.reason, False, False
    for i, line in enumerate(out.skills_lines, 1):
        yield f"skills line {i} label", line.label, False, True
        for item in line.items:
            yield f"skills line {i}", item, False, True
    for c in out.coursework:
        yield "coursework", c, False, True
    for f in out.flags:
        yield "flags", f, False, False
    if out.fit_paragraph:
        yield "fit_paragraph", out.fit_paragraph, False, False


def _check_strings(out: TailorOutput, r: Report) -> None:
    for where, text, is_bullet, on_resume in _all_strings(out):
        if _EM_DASH_RE.search(text):
            r.error("dashes", where, "contains an em dash (rule 4); use a comma or a period")
            continue
        if is_bullet and _EN_DASH_RE.search(text):
            r.error("dashes", where, "a bullet can't contain -- or an en dash; use a comma or a period")
            continue
        if _GPA_RE.search(text):
            r.error("gpa", where, "GPA never appears (rule 3)")
        if on_resume:
            try:
                (render_bullet if is_bullet else render_text)(text)
            except RenderError as exc:
                r.error("render", where, str(exc))


def _check_experience_title(title: str, item: MasterItem, ctx: Context, r: Report) -> None:
    where = f"experience {item.id} title"
    if item.id == "ta":
        expected = (ctx.meta.experience.get("ta").resume_title if ctx.meta.experience.get("ta") else None)  # type: ignore[union-attr]
        if not title.startswith(TA_PREFIX):
            r.error("experience", where, f"must start with '{TA_PREFIX}' (spec 7.1)")
        elif expected and title != expected:
            r.error("experience", where, f"must be exactly '{expected}' (resume_meta.yaml)")
    elif title != item.name:
        r.error("experience", where, f"job titles are facts: use '{item.name}' exactly")
    for rule in item.wording_rules:
        for v in violations(rule, "", title=title) if rule.title_prefix else []:
            r.error("wording", where, v)


def _check_bullet(text: str, n: int, item: MasterItem, ctx: Context, flags: list[str], r: Report) -> None:
    where = f"{item.id} bullet {n}"
    bolds = _BOLD_RE.findall(text)
    if len(bolds) > MAX_BOLD:
        r.error("bold", where, f"{len(bolds)} bold phrases; at most {MAX_BOLD} (spec 7.4)")
    plain = text.replace("**", "")
    _check_numbers(plain, item, where, ctx, r)
    _check_identifiers(plain, item, where, ctx, r)
    spans = _check_tech(plain, item, where, ctx, r)
    _check_names(plain, item, where, ctx, r, spans)
    for rule in item.wording_rules:
        if rule.scope == "bullet":
            for v in violations(rule, plain):
                r.error("wording", where, v)
    if not find_numbers(plain) and not _has_qualitative_flag(flags, item.id, n):
        r.error("qualitative", where, "has no number: describe the result qualitatively and add the flag "
                f"\"qualitative: {item.id} bullet {n}: <why>\" (rule 2)")


def _has_qualitative_flag(flags: list[str], item_id: str, n: int) -> bool:
    for flag in flags:
        m = _QUAL_FLAG_RE.match(flag)
        if m and m.group("item") == item_id and int(m.group("n")) == n:
            return True
    return False


def _check_numbers(text: str, item: MasterItem, where: str, ctx: Context, r: Report) -> None:
    allowed = ctx.number_keys(item)
    for mention in find_numbers(text):
        key = number_key(mention)
        same = [a for a in allowed if same_value(a.value, key.value) and a.unit == key.unit]
        if not same:
            near = sorted({a.raw for a in allowed if same_value(a.value, key.value)})
            hint = f" (the master list has {', '.join(near)})" if near else ""
            r.error("numbers", where, f"'{mention.raw}' is not a number in {item.id}'s master text{hint} (rule 2)")
            continue
        if key.sign and all(a.sign and a.sign != key.sign for a in same):
            r.error("numbers", where, f"'{mention.raw}' has the opposite sign of {', '.join(sorted({a.raw for a in same}))}")
        if not any(a.marker == key.marker for a in same):
            forms = " or ".join(sorted({a.raw for a in same}))
            r.error("numbers", where, f"'{mention.raw}' changes the master list's precision; write {forms} (spec 7.4)")


def _check_identifiers(text: str, item: MasterItem, where: str, ctx: Context, r: Report) -> None:
    allowed = {t.lower() for t in item.allowed_tokens}
    for token in sorted(find_identifier_tokens(text)):
        if token.lower() in allowed or ctx.vocab.canonical(token) or in_text(token, ctx.item_text(item)):
            continue
        r.error("numbers", where, f"'{token}' is not in {item.id}'s master text")


def _check_tech(text: str, item: MasterItem, where: str, ctx: Context, r: Report) -> list[tuple[int, int]]:
    spans = []
    for m in ctx.vocab.find(mask_comparisons(text)):
        spans.append((m.start, m.end))
        if m.canonical in item.allowed_tech or ctx.swap_natives(m.canonical, item):
            continue
        r.error("tech", where, f"'{m.text}' ({m.canonical}) is not a technology {item.id} used (rule 1)")
    return spans


def _check_names(text: str, item: MasterItem, where: str, ctx: Context, r: Report, spans: list[tuple[int, int]]) -> None:
    """Capitalized names the vocabulary doesn't know must still come from this item's master text."""
    itext = ctx.item_text(item)
    for m in _TOKEN_RE.finditer(text):
        token = m.group().rstrip(".-/'&_")
        if not token or not any(c.isupper() for c in token) or any(c.isdigit() for c in token):
            continue
        if _SENTENCE_START_RE.search(text[: m.start()]) or any(s <= m.start() < e for s, e in spans):
            continue
        if token in GENERIC_TOKENS or in_text(token, itext):
            continue
        r.error("names", where, f"'{token}' isn't in {item.id}'s master text; name only what this item involved (rule 1)")


def _check_tags(tags: list[str], item: MasterItem, ctx: Context, r: Report) -> None:
    where = f"{item.id} tags"
    if len(tags) != 3:
        r.error("structure", where, f"{len(tags)} tech tags; exactly 3")
    lowered = [t.strip().lower() for t in tags]
    if len(set(lowered)) != len(lowered):
        r.error("structure", where, "a tag is repeated")
    for tag in tags:
        canonical = ctx.canonical_of(tag)
        if canonical:
            if canonical not in item.allowed_tech and not ctx.swap_natives(canonical, item):
                r.error("tech", where, f"'{tag}' ({canonical}) is not a technology {item.id} used (rule 1)")
        elif not in_text(tag, ctx.item_text(item)):
            r.error("tech", where, f"'{tag}' is not in {item.id}'s master text")
        _check_numbers(tag, item, where, ctx, r)


def _check_entry(item: MasterItem, strings: list[str], ctx: Context, r: Report) -> None:
    """Rules about a whole entry: entry-scope wording rules and family swaps (a swap replaces, never adds)."""
    where = f"{item.id}"
    text = "\n".join(s.replace("**", "") for s in strings)
    named = {m.canonical for m in ctx.vocab.find(mask_comparisons(text))}
    for canonical in sorted(named - set(item.allowed_tech)):
        both = [n for n in ctx.swap_natives(canonical, item) if n in named]
        if both:
            r.error("tech", where, f"{canonical} stands in for {', '.join(both)} here, so name one of them, not both")
    for rule in item.wording_rules:
        if rule.scope == "entry":
            for v in violations(rule, text):
                r.error("wording", where, v)


def _check_skills(out: TailorOutput, ctx: Context, r: Report) -> None:
    if len(out.skills_lines) != 3:
        r.error("skills", "skills_lines", f"{len(out.skills_lines)} lines; exactly 3")
    approved = {t.lower() for t in ctx.synonyms.approved_terms()}
    pending = {s.term.lower() for s in ctx.synonyms.synonyms if not s.approved}
    seen: set[str] = set()
    for i, line in enumerate(out.skills_lines, 1):
        where = f"skills line {i} ({line.label})"
        if not line.label.strip():
            r.error("skills", where, "needs a label")
        if not line.items:
            r.error("skills", where, "has no skills")
        for term in line.items:
            key = term.strip().lower()
            if key in seen:
                r.warn("skills", where, f"'{term}' appears on more than one line")
            seen.add(key)
            if key in approved:
                continue
            canonical = ctx.canonical_of(term)
            if canonical and (canonical in ctx.skill_canonicals or canonical in ctx.all_tech):
                continue
            if in_text(term, ctx.master_text):
                continue
            hint = " (it is proposed in skill_synonyms.yaml but not approved)" if key in pending else ""
            r.error("skills", where, f"'{term}' is not in the master list's Skills section or any entry, and is not an "
                    f"approved synonym{hint}")


def resume_text(out: TailorOutput, ctx: Context) -> str:
    parts: list[str] = []
    for e in out.experience:
        parts += [e.title, *[b.replace("**", "") for b in e.bullets]]
    for p in out.projects:
        name = ctx.meta.project_display_names.get(p.project_id, p.project_id)
        parts += [f"{name}: {p.subtitle}", ", ".join(p.tech_tags), *[b.replace("**", "") for b in p.bullets]]
    for line in out.skills_lines:
        parts += [line.label, ", ".join(line.items)]
    parts += out.coursework
    return "\n".join(parts)


def _check_audit(out: TailorOutput, ctx: Context, r: Report) -> None:
    audit = out.keyword_audit
    text = resume_text(out, ctx)
    skills = "\n".join(", ".join(line.items) for line in out.skills_lines)
    for term in audit.matched:
        if not in_text(term, text):
            r.error("audit", "keyword_audit.matched", f"'{term}' is not on the resume verbatim (spec 7.5)")
    for term in audit.skills_only:
        if not in_text(term, skills):
            r.error("audit", "keyword_audit.skills_only", f"'{term}' is not in a skills line")
    for term in audit.gaps:
        if in_text(term, text):
            r.error("audit", "keyword_audit.gaps", f"'{term}' is listed as a gap but is on the resume")
        canonical = ctx.canonical_of(term)
        if (canonical and (canonical in ctx.all_tech or canonical in ctx.skill_canonicals)) or in_text(term, ctx.master_text):
            r.error("audit", "keyword_audit.gaps", f"'{term}' is in the master list, so it is not a gap: show it, or "
                    "list it under skills_only")
    total = len(audit.matched) + len(audit.skills_only) + len(audit.gaps)
    if total:
        r.keyword_match_pct = round(100 * (len(audit.matched) + len(audit.skills_only)) / total, 1)


def _check_flags(out: TailorOutput, ctx: Context, r: Report) -> None:
    bullets = {(e.entry_id, n): b for e in out.experience for n, b in enumerate(e.bullets, 1)}
    bullets.update({(p.project_id, n): b for p in out.projects for n, b in enumerate(p.bullets, 1)})
    for flag in out.flags:
        if not flag.lower().startswith("qualitative"):
            continue
        m = _QUAL_FLAG_RE.match(flag)
        if not m:
            r.warn("flags", "flags", f"'{flag}' should read 'qualitative: <item_id> bullet <n>: <why>'")
            continue
        bullet = bullets.get((m.group("item"), int(m.group("n"))))
        if bullet is None:
            r.warn("flags", "flags", f"'{flag}' points at a bullet that isn't on the resume")
        elif find_numbers(bullet.replace("**", "")):
            r.warn("flags", "flags", f"'{flag}' is marked qualitative but the bullet has a number")


def keyword_coverage_warnings(out: TailorOutput, keywords_tech: list[dict[str, Any]], r: Report) -> None:
    """Every technology the posting names should be sorted in the audit (spec 7.5 step 2)."""
    listed = "\n".join(out.keyword_audit.matched + out.keyword_audit.skills_only + out.keyword_audit.gaps)
    for term in keywords_tech:
        names = [term["canonical"], *term.get("spellings", [])]
        if not any(in_text(n, listed) for n in names):
            r.warn("audit", "keyword_audit", f"the posting names {term['canonical']}; the audit doesn't sort it")


# --------------------------------------------------------------------------- the compiled PDF


def bullet_line_counts(pdf: Path) -> Optional[list[int]]:
    """Rendered lines per bullet, in reading order, from word positions (pdftotext -tsv). None if unavailable."""
    binary = find_binary("pdftotext")
    if binary is None:
        return None
    proc = subprocess.run([str(binary), "-tsv", str(pdf), "-"], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout.startswith("level"):
        return None
    words: list[tuple[float, float, str]] = []
    for row in proc.stdout.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) >= 12 and cols[0] == "5":
            left, top, height = float(cols[6]), float(cols[7]), float(cols[9])
            words.append((top + height / 2, left, cols[11]))
    words.sort()
    rows: list[tuple[float, list[tuple[float, str]]]] = []
    for center, left, text in words:  # one visual row per vertical center
        if rows and abs(center - rows[-1][0]) <= 2.0:
            rows[-1][1].append((left, text))
        else:
            rows.append((center, [(left, text)]))
    counts = []
    for i, (_, row) in enumerate(rows):
        row = sorted(row)
        mark = next((left for left, text in row if text == "•"), None)
        if mark is None:
            continue
        text_x = min((left for left, _ in row if left > mark + 1), default=mark + 8)
        lines = 1
        for _, nxt in rows[i + 1:]:
            nxt = sorted(nxt)
            if any(t == "•" for _, t in nxt) or abs(nxt[0][0] - text_x) > 1.5:
                break
            lines += 1
        counts.append(lines)
    return counts


def check_compiled(result: CompileResult, tex_source: str, content: ResumeContent, r: Report) -> None:
    if not result.ok or result.pdf is None:
        r.error("latex", "pdf", f"did not compile: {'; '.join(result.errors[:3]) or 'see the .log'}")
        return
    if result.pages != 1:
        r.error("pages", "pdf", f"{result.pages} pages; exactly 1. Shorten bullets.")
    lines = tex_source.splitlines()
    for box in result.overfull:
        line = lines[box.start_line - 1].strip() if 0 < box.start_line <= len(lines) else ""
        if line.startswith((r"\resumeProjectHeading", r"\resumeSubheading")):
            r.error("overfull", _heading_name(line), f"runs {box.too_wide_pt:.1f}pt past the right margin; shorten the "
                    "title, subtitle or tags")
        else:
            r.warn("overfull", f".tex line {box.start_line}", f"{box.too_wide_pt:.1f}pt too wide: {line[:80]}")
    expected = [f"{e.title.split(',')[0]} bullet {n}" for e in content.experience for n in range(1, len(e.bullets) + 1)]
    expected += [f"{p.title.split(':')[0]} bullet {n}" for p in content.projects for n in range(1, len(p.bullets) + 1)]
    counts = bullet_line_counts(result.pdf)
    if counts is None:
        r.warn("lines", "pdf", "pdftotext -tsv is unavailable, so bullet line counts were not checked")
    elif len(counts) != len(expected):
        r.warn("lines", "pdf", f"found {len(counts)} bullets in the PDF, expected {len(expected)}; line counts not checked")
    else:
        for where, n in zip(expected, counts):
            if n > MAX_BULLET_LINES:
                r.error("lines", where, f"renders on {n} lines; at most {MAX_BULLET_LINES}. Shorten it.")


def _heading_name(tex_line: str) -> str:
    m = re.search(r"\\textbf\{([^}]*)\}", tex_line) or re.search(r"\{([^}]*)\}", tex_line)
    return m.group(1) if m else "heading"
