"""`helper convert`: data/master_list_v<N>.md -> data/master.yaml (spec 5.1).

All text is kept verbatim. The converter only adds structure: stable ids, parsed dates, and the
allowed_numbers / allowed_tech / allowed_tokens lists the resume validator checks bullets against.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import Paths, load_yaml
from .dates import parse_timeline
from .errors import HelperError
from .models import (
    Config,
    DatabaseFraming,
    DateInfo,
    FramingRule,
    MasterData,
    MasterItem,
    RequireRule,
    ResumeMetaFile,
    SkillGroup,
    SkillItem,
    SourceInfo,
    WordingRule,
)
from .numbers import find_identifier_tokens, find_numbers
from .techvocab import (
    NO_SLASH_SPLIT,
    TechVocabulary,
    build_vocabulary,
    is_tech_like,
    mask_comparisons,
    split_parenthetical,
    split_tech_item,
    split_top_level,
)

# Stable ids. Each pattern is searched, case-insensitively, in a project's heading or overview name.
PROJECT_IDS: list[tuple[str, str]] = [
    ("streamline", r"^StreamLine\b"),
    ("ecommerce_platform", r"^E-commerce Platform\b"),
    ("chess_ai", r"^Intelligent Chess AI\b"),
    ("ai_agent_discovery", r"^AI Agent Discovery\b"),
    ("f1_analytics", r"^F1 Analytics\b"),
    ("investment_prediction", r"^Investment Prediction\b"),
    ("recommendation_engine", r"^E-commerce Recommendation\b"),
    ("nutritrack", r"^NutriTrack\b"),
    ("seattle_transit", r"^Seattle Transit\b"),
    ("housing_price", r"^Housing Price\b"),
    ("moodmeals", r"^MoodMeals\b"),
    ("traffic_accident", r"^Traffic Accident\b"),
    ("themanager", r"^theManager\b"),
    ("forum", r"^FORUM\b"),
    ("swarm", r"^SWARM\b"),
    ("wardrobe", r"^Wardrobe\b"),
    ("pulsetrader", r"^PulseTrader\b"),
    ("scenarioforge", r"^ScenarioForge\b"),
    ("perceptnet", r"^PerceptNet\b"),
    ("transitlens", r"^TransitLens\b"),
    ("experimentiq", r"^ExperimentIQ\b"),
    ("spiceai", r"spiceai/spiceai"),
    ("chef", r"chef/chef"),
    ("echocoach", r"^EchoCoach\b"),
    ("afterglow", r"^Afterglow\b"),
    ("pnwer", r"^PNWER\b"),
]
# Searched in "<heading> | <organization>".
EXPERIENCE_IDS: list[tuple[str, str]] = [
    ("ta", r"Teaching Assistant"),
    ("kalini_ml", r"^Machine Learning Intern\b.*Kalini"),
    ("kalini_sde", r"^Software Development Intern\b.*Kalini"),
    ("event_assistant", r"^Event Assistant\b"),
    ("student_proctor", r"^Student Proctor\b"),
    ("baps", r"BAPS"),
]


@dataclass(frozen=True)
class KnownRule:
    rule: WordingRule
    note_hint: Optional[str]  # regex that finds this rule's source text among the item's wording notes


# Machine checks for the wording rules the spec lists (5.1). Their source text is copied from the
# master list when it is there.
# Patterns are applied as written; (?i) marks the case-insensitive ones.
KNOWN_RULES: dict[str, list[KnownRule]] = {
    "spiceai": [
        KnownRule(
            WordingRule(
                id="spiceai_pr_merged",
                source="spec 5.1",
                description="PR #13507 is always described as merged.",
                forbid=[
                    r"(?i)#?13507\)?\s+(?:is\s+|was\s+|remains\s+|still\s+)*"
                    r"(?:pending|open|unmerged|awaiting|under review|not\s+(?:yet\s+)?merged)\b",
                    r"(?i)\b(?:pending|open|unmerged|awaiting)\s+(?:PR\s+)?#?13507",
                    r"(?i)\bno PRs? (?:merged|have been merged)",
                    r"(?i)\bnot yet merged\b",
                ],
            ),
            note_hint=r"merged",
        ),
        KnownRule(
            WordingRule(
                id="spiceai_12747_bug_report",
                source="spec 5.1",
                description="Issue #12747 is a bug report that led to the CEO's overhaul, never Vatsal's own implementation.",
                forbid=[
                    r"(?i)\b(?:implemented|built|wrote|authored|shipped|developed|patched|coded)\b[^.;]{0,80}"
                    r"(?:12747|overhaul|Decimal128|overflow|2,244|2,200|13224)",
                    r"(?i)\b(?:my|own) (?:fix|implementation|overhaul)\b[^.;]{0,40}(?:overflow|Decimal128|12747)",
                ],
                require=[RequireRule(when=r"(?i)12747|Decimal128|overflow|overhaul|2,244|2,200|13224",
                                     must=r"(?i)\b(?:report|reported|filed|issue)\b")],
            ),
            note_hint=r"bug report",
        ),
    ],
    "chef": [
        KnownRule(
            WordingRule(
                id="chef_pr_merged",
                source="spec 5.1",
                description="PR #16260 is always described as merged.",
                forbid=[r"(?i)\b(?:pending|awaiting|unmerged|not\s+(?:yet\s+)?merged|under review|re-approval)\b"],
            ),
            note_hint=r"merged",
        ),
    ],
    "ecommerce_platform": [
        KnownRule(
            WordingRule(
                id="ecommerce_not_9_java",
                source="spec 5.1",
                description='Java Spring Boot plus one service in Go, never "9 Java services".',
                forbid=[
                    r"(?i)\b(?:9|nine)\s+(?:independent\s+)?(?:Java|Spring)\b",
                    r"(?i)\ball\b[^.;]{0,20}\bJava\b[^.;]{0,20}\bservices\b",
                ],
                require=[RequireRule(when=r"(?i)\b(?:9|nine)\b[^.;]{0,40}\b(?:Java|Spring Boot)\b", must=r"\bGo\b")],
            ),
            note_hint=None,
        ),
    ],
    "ta": [
        KnownRule(
            WordingRule(
                id="ta_title",
                source="spec 5.1",
                description='The title always starts with "Graduate Teaching Assistant".',
                title_prefix="Graduate Teaching Assistant",
            ),
            note_hint=r"Graduate Teaching Assistant",
        ),
    ],
}

_WORDING_NOTE_RE = re.compile(r"Always (?:describe|title)|Must always be described|never implied", re.IGNORECASE)
_VARIANT_RE = re.compile(
    r"Can also be described as|Confirmed variants|described differently|framing has been", re.IGNORECASE
)
_FIELD_RE = re.compile(r"^\*\*(?P<label>[^*]+?):\*\*\s*(?P<value>.*)$")
_STATUS_RE = re.compile(r"^\*\*Status:\s*(?P<head>[^*]+?)\*\*\s*(?P<rest>.*)$")
_SEGMENT_SPLIT_RE = re.compile(r"\s+\|\s+(?=\*\*[^*]+?:\*\*)")
_URL_RE = re.compile(r"https?://[^\s)]+")
_COUNT_NOTE_RE = re.compile(r"\s*\((\d+ [a-z ]+)\)\s*$")

MASTER_HEADER = "# Generated by `helper convert` from {file}. Do not edit by hand: edit the master list and re-run.\n"


# --------------------------------------------------------------------------- low-level parsing


Line = tuple[int, str]


@dataclass
class Block:
    title: str
    lineno: int
    lines: list[Line] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(line for _, line in self.lines).strip()


def split_blocks(lines: list[Line], prefix: str) -> tuple[Block, list[Block]]:
    """Split on headings starting with `prefix` ('## ' or '### '). Returns (lines before the first one, blocks)."""
    preamble = Block("", 0)
    blocks: list[Block] = []
    for lineno, line in lines:
        if line.startswith(prefix):
            blocks.append(Block(line[len(prefix):].strip(), lineno))
        else:
            (blocks[-1] if blocks else preamble).lines.append((lineno, line))
    return preamble, blocks


def parse_field_line(line: str) -> Optional[list[tuple[str, str]]]:
    """'**A:** x | **B:** y' -> [('A', 'x'), ('B', 'y')]; '**Status: MERGED.** rest' -> [('Status', 'MERGED. rest')]."""
    if not line.startswith("**"):
        return None
    status = _STATUS_RE.match(line)
    if status:
        return [("Status", f"{status.group('head')} {status.group('rest')}".strip())]
    out = []
    for segment in _SEGMENT_SPLIT_RE.split(line):
        m = _FIELD_RE.match(segment)
        if not m:
            return None
        out.append((m.group("label").strip(), m.group("value").strip()))
    return out


@dataclass
class ParsedBlock:
    fields: dict[str, str] = field(default_factory=dict)
    sections: dict[str, list[str]] = field(default_factory=dict)
    metrics: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


def parse_item_block(lines: list[Line], default_section: str = "Description") -> ParsedBlock:
    parsed = ParsedBlock()
    current: Optional[str] = None
    for lineno, raw in lines:
        line = raw.strip()
        if not line or line == "---":
            continue
        if line.startswith("- "):
            parsed.sections.setdefault(current or default_section, []).append(line[2:].strip())
            continue
        fields = parse_field_line(line)
        if fields is None:
            parsed.unparsed.append(f"line {lineno}: {line}")
            continue
        for label, value in fields:
            if label.startswith("Key ") and value:
                parsed.metrics[label] = [m.strip() for m in value.split(" | ")]
            elif label == "Note":
                parsed.notes.append(value)
            elif not value:
                current = label
                parsed.sections.setdefault(label, [])
            else:
                parsed.fields[label] = value
    return parsed


def item_text(*parts: Any) -> str:
    """All of an item's text as one plain string (markdown bold and code marks removed)."""
    chunks: list[str] = []
    for part in parts:
        if not part:
            continue
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict):
            for key, value in part.items():
                chunks.append(str(key))
                chunks.extend(value if isinstance(value, list) else [str(value)])
        else:
            chunks.extend(str(p) for p in part)
    return "\n".join(chunks).replace("**", "").replace("`", "")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def match_id(text: str, table: list[tuple[str, str]]) -> Optional[str]:
    for item_id, pattern in table:
        if re.search(pattern, text, re.IGNORECASE):
            return item_id
    return None


def split_heading(heading: str) -> tuple[str, Optional[str]]:
    """'StreamLine - Distributed Messaging Engine' -> ('StreamLine', 'Distributed Messaging Engine')."""
    for sep in (" - ", " — "):
        if sep in heading:
            name, subtitle = heading.split(sep, 1)
            return name.strip(), subtitle.strip()
    base, inner = split_parenthetical(heading)
    if inner and base != heading:
        return base, inner
    return heading.strip(), None


def status_kind(status: Optional[str]) -> Optional[str]:
    if not status:
        return None
    lowered = status.lower()
    for prefix, kind in (("complete", "complete"), ("merged", "merged"), ("in progress", "in_progress")):
        if lowered.startswith(prefix):
            return kind
    return "other"


# --------------------------------------------------------------------------- skills


def parse_skill_items(text: str) -> tuple[list[SkillItem], list[str]]:
    """One comma list of skills -> (items, group notes). Prose lines become notes."""
    text = text.replace("**", "").strip()
    notes: list[str] = []
    dash_parts = split_top_level(text, " — ")
    if len(dash_parts) > 1:
        text, notes = dash_parts[0], [" — ".join(dash_parts[1:])]
    count = _COUNT_NOTE_RE.search(text)
    if count and text.count("(") == text[: count.start()].count("(") + 1:
        notes.insert(0, count.group(1))
        text = text[: count.start()]
    parts = split_top_level(text)
    if any(len(split_parenthetical(p)[0].split()) > 6 for p in parts):  # long entries mean prose, not a list
        return [], [text if not notes else f"{text} — {' '.join(notes)}"]
    items = []
    for part in parts:
        name, note = split_parenthetical(part)
        aliases = [n for n in split_tech_item(name) if n != name]
        items.append(SkillItem(name=name, note=note, aliases=aliases))
    return items, notes


def parse_skills(block: Block, problems: list[str]) -> tuple[list[SkillGroup], Optional[str]]:
    _, group_blocks = split_blocks(block.lines, "### ")
    groups: list[SkillGroup] = []
    framing_text: Optional[str] = None
    for gb in group_blocks:
        group = SkillGroup(name=gb.title)
        for lineno, raw in gb.lines:
            line = raw.strip()
            if not line or line == "---":
                continue
            if line.startswith("- "):
                fields = parse_field_line(line[2:].strip())
                if fields and len(fields) == 1 and fields[0][1]:
                    sub_items, sub_notes = parse_skill_items(fields[0][1])
                    group.subgroups.append(SkillGroup(name=fields[0][0], items=sub_items, notes=sub_notes))
                else:
                    group.notes.append(line[2:].strip())
                continue
            fields = parse_field_line(line)
            if fields:
                for label, value in fields:
                    if label == "Database framing":
                        framing_text = value
                    else:
                        group.notes.append(f"{label}: {value}")
                continue
            items, notes = parse_skill_items(line)
            group.items.extend(items)
            group.notes.extend(notes)
        if not group.items and not group.subgroups and not group.notes:
            problems.append(f"skills group '{gb.title}' (line {gb.lineno}) is empty")
        groups.append(group)
    return groups, framing_text


def parse_database_framing(text: Optional[str], project_ids_by_tech: dict[str, set[str]],
                           problems: list[str]) -> Optional[DatabaseFraming]:
    if not text:
        problems.append("no 'Database framing' rule found in the Skills section")
        return None
    rx = re.compile(
        r"built with (?P<a>[A-Za-z][\w ]*?) \(e\.g\., (?P<examples>[^)]*)\) "
        r"have also genuinely been implemented with (?P<b>[A-Za-z][\w ]*?)[,.]",
        re.IGNORECASE,
    )
    rules = []
    for m in rx.finditer(text):
        a, b, examples = m.group("a").strip(), m.group("b").strip(), m.group("examples").strip()
        named = {pid for ex in split_top_level(examples) if (pid := match_id(ex.strip(), PROJECT_IDS))}
        applies = sorted(named | project_ids_by_tech.get(a.lower(), set()))
        rules.append(FramingRule(built_with=a, also=b, examples=examples, applies_to=applies))
    if len(rules) != 2:
        problems.append(f"Database framing: expected 2 rules (SQL and NoSQL), parsed {len(rules)}")
    return DatabaseFraming(text=text, rules=rules)


# --------------------------------------------------------------------------- items


@dataclass
class ItemDraft:
    item: MasterItem
    text: str  # all of the item's text, plain


def _wording_notes(parsed: ParsedBlock) -> list[str]:
    candidates = [f"{k}: {v}" for k, v in parsed.fields.items()] + parsed.notes + [
        b for bullets in parsed.sections.values() for b in bullets
    ]
    return [c for c in candidates if _WORDING_NOTE_RE.search(c)]


def _variants(parsed: ParsedBlock) -> list[str]:
    candidates = parsed.notes + [b for bullets in parsed.sections.values() for b in bullets]
    return [c for c in candidates if _VARIANT_RE.search(c)]


def _attach_rules(item: MasterItem, problems: list[str]) -> None:
    unclaimed = list(item.wording_notes)
    for known in KNOWN_RULES.get(item.id, []):
        rule = known.rule.model_copy(deep=True)
        if known.note_hint:
            for note in item.wording_notes:
                if re.search(known.note_hint, note, re.IGNORECASE):
                    rule.source = note
                    if note in unclaimed:
                        unclaimed.remove(note)
                    break
        item.wording_rules.append(rule)
    for note in unclaimed:
        problems.append(f"{item.id}: wording note has no automatic check yet: {note[:120]}")


def build_project(number: int, heading: str, parsed: ParsedBlock, overview: Optional[dict[str, str]],
                  link: Optional[str], problems: list[str]) -> ItemDraft:
    name, subtitle = split_heading(heading)
    pid = match_id(heading, PROJECT_IDS) or (overview and match_id(overview["name"], PROJECT_IDS))
    if not pid:
        pid = slugify(name)
        problems.append(f"project {number} '{heading}' is new: id '{pid}' (add a display name to resume_meta.yaml)")
    fields = dict(parsed.fields)
    timeline_text = fields.pop("Timeline", None)
    timeline = parse_timeline(timeline_text) if timeline_text else None
    if timeline_text and timeline is None:
        problems.append(f"{pid}: could not parse timeline {timeline_text!r}")
    if not timeline_text:
        problems.append(f"{pid}: no Timeline line")
    github_field = fields.get("GitHub")
    github_url = None
    if github_field:
        url = _URL_RE.search(github_field)
        github_url = url.group() if url else None
        if url and github_field.strip() == url.group():
            fields.pop("GitHub")
    github_url = github_url or link
    tech_line = fields.pop("Technologies", None)
    status = overview["status"] if overview else None
    if overview is None:
        problems.append(f"{pid}: not in the All Projects Overview table")
    item = MasterItem(
        id=pid, kind="project", number=number, name=name, heading=heading, subtitle=subtitle,
        category=overview["category"] if overview else None,
        status=status, status_kind=status_kind(status),
        timeline=timeline_text,
        timeline_note=timeline.note if timeline else None,
        dates=DateInfo(start=timeline.start_iso(), end=timeline.end_iso(), display=timeline.display()) if timeline else None,
        github_url=github_url,
        technologies=split_top_level(tech_line) if tech_line else [],
        fields=fields, sections=parsed.sections, metrics=parsed.metrics, notes=parsed.notes,
        variants=_variants(parsed), wording_notes=_wording_notes(parsed),
    )
    text = item_text(heading, overview and overview["name"], item.category, list(parsed.fields.values()),
                     parsed.sections, parsed.metrics, parsed.notes)
    problems.extend(f"{pid}: could not parse {u}" for u in parsed.unparsed)
    return ItemDraft(item, text)


def build_experience(heading: str, parsed: ParsedBlock, problems: list[str]) -> ItemDraft:
    fields = dict(parsed.fields)
    org = fields.pop("Organization", None)
    location = fields.pop("Location", None)
    timeline_text = fields.pop("Timeline", None)
    name, subtitle = split_heading(heading)
    eid = match_id(f"{heading} | {org or ''}", EXPERIENCE_IDS)
    if not eid:
        eid = slugify(name)
        problems.append(f"experience '{heading}' is new: id '{eid}' (add its location to resume_meta.yaml)")
    timeline = parse_timeline(timeline_text) if timeline_text else None
    if timeline_text and timeline is None:
        problems.append(f"{eid}: could not parse timeline {timeline_text!r}")
    item = MasterItem(
        id=eid, kind="experience", name=name, heading=heading, subtitle=subtitle,
        timeline=timeline_text, timeline_note=timeline.note if timeline else None,
        dates=DateInfo(start=timeline.start_iso(), end=timeline.end_iso(), display=timeline.display()) if timeline else None,
        organization=org, location=location,
        fields=fields, sections=parsed.sections, metrics=parsed.metrics, notes=parsed.notes,
        variants=_variants(parsed), wording_notes=_wording_notes(parsed),
    )
    text = item_text(heading, list(parsed.fields.values()), org, location, timeline_text,
                     parsed.sections, parsed.metrics, parsed.notes)
    problems.extend(f"{eid}: could not parse {u}" for u in parsed.unparsed)
    return ItemDraft(item, text)


def finish_item(draft: ItemDraft, vocab: TechVocabulary, framing: Optional[DatabaseFraming]) -> None:
    item = draft.item
    text = _URL_RE.sub(" ", draft.text)  # repo links are not technologies or metrics
    tech = vocab.find_canonical(mask_comparisons(text))
    for entry in item.technologies:
        for name in split_tech_item(entry):
            tech.add(vocab.canonical(name) or name)
    if framing:
        for rule in framing.rules:
            if item.id in rule.applies_to:
                for name in (rule.built_with, rule.also):
                    canonical = vocab.canonical(name) or name
                    if canonical not in tech:
                        item.framing_additions.append(canonical)
                        tech.add(canonical)
    item.allowed_tech = sorted(tech, key=str.lower)
    seen: dict[str, None] = {}
    for number in find_numbers(text):
        seen.setdefault(number.raw, None)
    item.allowed_numbers = list(seen)
    item.allowed_tokens = sorted(find_identifier_tokens(text), key=str.lower)


# --------------------------------------------------------------------------- whole document


@dataclass
class ConvertResult:
    master: MasterData
    master_path: Path
    source_path: Path
    notes: list[str]  # informational, not problems
    meta_missing: list[str]  # master ids with no resume_meta.yaml entry
    meta_unknown: list[str]  # resume_meta.yaml ids not in the master list
    language_check: Optional[str]


def _first(rx: str, text: str) -> Optional[str]:
    m = re.search(rx, text, re.MULTILINE)
    return m.group(1).strip() if m else None


def parse_master(markdown: str, lexicon: dict[str, Any], source_name: str) -> tuple[MasterData, list[str], Optional[str]]:
    """Pure parse. Returns (master data, informational notes, language cross-check message)."""
    lines = list(enumerate(markdown.splitlines(), start=1))
    problems: list[str] = []
    notes: list[str] = []
    _, sections = split_blocks(lines, "## ")

    def find(keyword: str) -> Optional[Block]:
        # Prefer a title that starts with the keyword: the document title also mentions "Professional Experience".
        matches = [b for b in sections if keyword in b.title.upper()]
        return min(matches, key=lambda b: (not b.title.upper().startswith(keyword), len(b.title)), default=None)

    overview_block = find("OVERVIEW")
    details_block = find("PROJECT DETAILS")
    experience_block = find("PROFESSIONAL EXPERIENCE")
    skills_block = find("SKILLS")
    stats_block = find("SUMMARY STATISTICS")
    links_block = find("GITHUB LINKS")
    for name, block in (("All Projects Overview", overview_block), ("Project Details", details_block),
                        ("Professional Experience", experience_block), ("Skills & Technologies", skills_block)):
        if block is None:
            problems.append(f"section '{name}' not found")

    overview: dict[int, dict[str, str]] = {}
    for lineno, line in overview_block.lines if overview_block else []:
        m = re.match(r"^\|\s*(\d+)\s*\|(.+)\|\s*$", line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) != 4:
            problems.append(f"overview line {lineno}: expected 4 columns, found {len(cells)}")
            continue
        overview[int(m.group(1))] = dict(zip(("name", "category", "timeline", "status"), cells))

    links: dict[int, str] = {}
    for _, line in links_block.lines if links_block else []:
        m = re.match(r"^(\d+)\.\s+.+?→\s*(https?://\S+)", line)
        if m:
            links[int(m.group(1))] = m.group(2)

    drafts: list[ItemDraft] = []
    if details_block:
        _, project_blocks = split_blocks(details_block.lines, "### ")
        for pb in project_blocks:
            m = re.match(r"^PROJECT\s+(\d+):\s*(.+)$", pb.title)
            if not m:
                problems.append(f"line {pb.lineno}: heading '{pb.title}' is not 'PROJECT N: name'")
                continue
            number = int(m.group(1))
            drafts.append(build_project(number, m.group(2).strip(), parse_item_block(pb.lines),
                                        overview.get(number), links.get(number), problems))
    exp_drafts: list[ItemDraft] = []
    if experience_block:
        _, exp_blocks = split_blocks(experience_block.lines, "### ")
        for eb in exp_blocks:
            exp_drafts.append(build_experience(eb.title, parse_item_block(eb.lines), problems))

    skills, framing_text = parse_skills(skills_block, problems) if skills_block else ([], None)

    # Vocabulary: lexicon + every Technologies-line name + name-like skills.
    master_terms: list[str] = []
    for draft in drafts:
        for entry in draft.item.technologies:
            master_terms.extend(split_tech_item(entry))
    for group in skills:
        for item in group.all_items():
            if is_tech_like(item.name):
                if "/" not in item.name or item.name in NO_SLASH_SPLIT:
                    master_terms.append(item.name)
                master_terms.extend(a for a in item.aliases if is_tech_like(a))
    vocab = build_vocabulary(lexicon, master_terms)

    by_tech: dict[str, set[str]] = {}
    for draft in drafts:
        for entry in draft.item.technologies:
            for name in split_tech_item(entry):
                by_tech.setdefault(name.lower(), set()).add(draft.item.id)
    framing = parse_database_framing(framing_text, by_tech, problems)

    for draft in drafts + exp_drafts:
        finish_item(draft, vocab, framing)
        _attach_rules(draft.item, problems)
        if draft.item.kind == "project" and not draft.item.technologies:
            notes.append(f"{draft.item.id} has no Technologies line; its technologies come from its text")

    seen_ids: set[str] = set()
    for draft in drafts + exp_drafts:
        if draft.item.id in seen_ids:
            problems.append(f"two items share the id '{draft.item.id}'")
        seen_ids.add(draft.item.id)
    for number in sorted(set(overview) - {d.item.number for d in drafts}):
        problems.append(f"overview row {number} ('{overview[number]['name']}') has no project details")

    languages: list[str] = []
    for group in skills:
        if "language" in group.name.lower():
            languages = [i.name for i in group.all_items()]
            break
    language_check = None
    stats_list = _first(r"^- \*\*Programming Languages:\*\*\s*(.+)$", stats_block.text()) if stats_block else None
    if stats_list:
        listed = split_top_level(stats_list, " — ")[-1]
        stats_names = [split_parenthetical(p)[0] for p in split_top_level(listed)]
        if set(stats_names) == set(languages):
            language_check = f"Summary Statistics lists the same {len(stats_names)}"
        else:
            diff = sorted(set(stats_names) ^ set(languages))
            problems.append(f"language lists differ between Skills and Summary Statistics: {', '.join(diff)}")

    other_sections = {
        b.title: b.text() for b in sections
        if b not in (overview_block, details_block, experience_block, skills_block)
    }
    declared_p = _first(r"^\*\*Total Projects:\*\*\s*(\d+)", markdown)
    declared_e = _first(r"^\*\*Total Professional Experience:\*\*\s*(\d+)", markdown)
    source = SourceInfo(
        file=source_name,
        sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        version=_first(r"^\*\*Document Version:\*\*\s*(.+)$", markdown),
        last_updated=_first(r"^\*\*Last Updated:\*\*\s*(.+)$", markdown),
        declared_projects=int(declared_p) if declared_p else None,
        declared_experience=int(declared_e) if declared_e else None,
    )
    if source.declared_projects is not None and source.declared_projects != len(drafts):
        problems.append(f"the file says {source.declared_projects} projects; parsed {len(drafts)}")
    if source.declared_experience is not None and source.declared_experience != len(exp_drafts):
        problems.append(f"the file says {source.declared_experience} experience entries; parsed {len(exp_drafts)}")

    master = MasterData(
        source=source,
        projects=[d.item for d in drafts],
        experience=[d.item for d in exp_drafts],
        skills=skills,
        languages=languages,
        database_framing=framing,
        other_sections=other_sections,
        problems=problems,
    )
    return master, notes, language_check


def load_lexicon(paths: Paths) -> dict[str, Any]:
    return load_yaml(paths.data / "tech_lexicon.yaml") or {}


class _Dumper(yaml.SafeDumper):
    """Short string lists in flow style ([a, b, c]); everything else in block style."""


def _represent_list(dumper: yaml.SafeDumper, data: list[Any]) -> yaml.Node:
    flow = bool(data) and all(isinstance(x, str) and len(x) <= 40 and "\n" not in x for x in data)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=flow)


_Dumper.add_representer(list, _represent_list)


def dump_master(master: MasterData) -> str:
    body = yaml.dump(
        master.model_dump(mode="json"), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100000,
    )
    return MASTER_HEADER.format(file=master.source.file) + body


def run_convert(paths: Paths, cfg: Config) -> ConvertResult:
    source = paths.master_list(cfg)
    markdown = source.read_text(encoding="utf-8")
    master, notes, language_check = parse_master(markdown, load_lexicon(paths), str(source.relative_to(paths.root)))
    paths.master_yaml.write_text(dump_master(master), encoding="utf-8")
    meta = ResumeMetaFile.model_validate(load_yaml(paths.resume_meta))
    master_project_ids = {p.id for p in master.projects}
    master_exp_ids = {e.id for e in master.experience}
    missing = sorted((master_project_ids - set(meta.project_display_names)) | (master_exp_ids - set(meta.experience)))
    unknown = sorted((set(meta.project_display_names) - master_project_ids) | (set(meta.experience) - master_exp_ids))
    return ConvertResult(master, paths.master_yaml, source, notes, missing, unknown, language_check)


# --------------------------------------------------------------------------- summary


def format_summary(result: ConvertResult) -> str:
    m = result.master
    out: list[str] = []
    version = f" ({m.source.version})" if m.source.version else ""
    out.append(f"Parsed {m.source.file}{version} -> {result.master_path.name}")
    out.append("")
    said = f", the file says {m.source.declared_projects}" if m.source.declared_projects is not None else ""
    out.append(f"Projects: {len(m.projects)} parsed{said}")
    out.append(f"  {'#':>2}  {'id':<22} {'status':<30} {'dates':<24} {'bullets':>7} {'metrics':>7} {'tech':>5} {'nums':>5} {'rules':>5}")
    for p in m.projects:
        n_metrics = sum(len(v) for v in p.metrics.values())
        dates = p.dates.display if p.dates else "?"
        out.append(
            f"  {p.number or '':>2}  {p.id:<22} {(p.status or '?')[:30]:<30} {dates:<24} {len(p.bullets()):>7} "
            f"{n_metrics:>7} {len(p.allowed_tech):>5} {len(p.allowed_numbers):>5} {len(p.wording_rules):>5}"
        )
    out.append("")
    said = f", the file says {m.source.declared_experience}" if m.source.declared_experience is not None else ""
    out.append(f"Experience: {len(m.experience)} parsed{said}")
    for e in m.experience:
        dates = e.dates.display if e.dates else "?"
        out.append(f"  {e.id:<16} {e.name[:44]:<44} {(e.organization or '?')[:40]:<40} {dates:<24} {len(e.bullets())} bullets")
    out.append("")
    n_items = sum(len(g.all_items()) for g in m.skills)
    out.append(f"Skills: {len(m.skills)} groups, {n_items} entries. Languages: {len(m.languages)}"
               + (f" ({result.language_check})" if result.language_check else ""))
    if m.database_framing:
        for rule in m.database_framing.rules:
            out.append(f"Database framing: {rule.built_with} -> also {rule.also} for {', '.join(rule.applies_to)}")
    variants = [f"{i.id} ({len(i.variants)})" for i in m.items() if i.variants]
    out.append(f"Variant notes: {', '.join(variants) if variants else 'none'}")
    out.append("Wording rules:")
    for item in m.items():
        for rule in item.wording_rules:
            origin = "master list" if rule.source != "spec 5.1" else "spec 5.1"
            out.append(f"  {item.id:<20} {rule.id:<26} from {origin}")
    if result.notes:
        out.append("Notes:")
        out.extend(f"  {n}" for n in result.notes)
    out.append(f"Kept verbatim, not used for checks: {', '.join(m.other_sections) or 'none'}")
    if result.meta_missing:
        out.append(f"resume_meta.yaml has no entry for: {', '.join(result.meta_missing)}")
    if result.meta_unknown:
        out.append(f"resume_meta.yaml has ids not in the master list: {', '.join(result.meta_unknown)}")
    out.append("")
    if m.problems:
        out.append(f"Could not parse or check ({len(m.problems)}):")
        out.extend(f"  {p}" for p in m.problems)
    else:
        out.append("Could not parse: nothing. Every line was captured.")
    return "\n".join(out)


def show_item(master: MasterData, item_id: str) -> str:
    try:
        item = master.item(item_id)
    except KeyError:
        ids = ", ".join(i.id for i in master.items())
        raise HelperError(f"No item '{item_id}'. Ids: {ids}") from None
    return yaml.dump(item.model_dump(mode="json"), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100000)
