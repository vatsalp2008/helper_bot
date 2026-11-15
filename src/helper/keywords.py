"""Deterministic first pass over a job description (spec 9 step 4): sections, named technologies, signals.

Claude does the judgment part of the keyword audit (spec 7.5) from tailor_input.json. This pass gives it, and the
validator, the facts that need no judgment: which technologies the posting names, where, and whether the master
list has them.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import Field

from .models import MasterData, ProjectSetsFile, StrictModel, TechFamiliesFile
from .techvocab import TechVocabulary, mask_comparisons

SectionKind = Literal["required", "preferred", "responsibilities", "other"]

# Checked in this order: "Preferred Qualifications" is preferred, "Basic Qualifications" is required.
_SECTION_PATTERNS: list[tuple[SectionKind, re.Pattern[str]]] = [
    ("preferred", re.compile(
        r"(?i)\b(preferred|nice[- ]to[- ]haves?|bonus|pluses|plus points|desired|desirable|ideally|extra credit)\b")),
    ("required", re.compile(
        r"(?i)\b(requirements?|required|qualifications?|must[- ]haves?|what you(?:'|’)ll (?:need|bring)"
        r"|what we(?:'|’)re looking for|you (?:have|bring)|who you are|about you|minimum|basic|skills)\b")),
    ("responsibilities", re.compile(
        r"(?i)\b(responsibilit(?:y|ies)|what you(?:'|’)ll (?:do|work on|build)|the role|your role|in this role"
        r"|day[- ]to[- ]day|you will|duties|the work|the opportunity)\b")),
]
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-•*·▪◦–]|\d+[.)])\s+")
_WEIGHTS: dict[str, int] = {"required": 3, "responsibilities": 2, "preferred": 1, "other": 1}

# Spec 7.2: include a merged open source project when the posting values these.
_OPEN_SOURCE_RE = re.compile(
    r"(?i)\bopen[- ]source\b|\bcode reviews?\b|\bdebug(?:ging|ged)?\b|\bunfamiliar code(?:\s?bases?)?\b"
    r"|\breliability\b|\bsite reliability\b|\bSRE\b"
)


class Section(StrictModel):
    kind: SectionKind
    heading: Optional[str] = None
    lines: list[str] = Field(default_factory=list)


class TechTerm(StrictModel):
    canonical: str
    spellings: list[str]  # exactly as the posting writes it
    sections: list[SectionKind]
    count: int
    status: Literal["in_items", "skills_only", "not_in_master"]
    items: list[str] = Field(default_factory=list)  # items whose allowed_tech has it
    swaps: list[str] = Field(default_factory=list)  # "<item> (<member>)": items that may name it by an approved swap


class ItemScore(StrictModel):
    id: str
    kind: str
    score: int
    tech: list[str]


class RoleTypeScore(StrictModel):
    id: str
    label: str
    score: int


class Keywords(StrictModel):
    sections: list[Section]
    tech: list[TechTerm]
    open_source_signals: list[str]
    item_scores: list[ItemScore]
    role_type_scores: list[RoleTypeScore]

    def term(self, canonical: str) -> Optional[TechTerm]:
        return next((t for t in self.tech if t.canonical == canonical), None)


def _heading(line: str) -> Optional[str]:
    s = line.strip()
    if not s or len(s) > 80:
        return None
    m = re.match(r"^#{1,6}\s+(.+)$", s)
    if m:
        return m.group(1).strip(" :*")
    m = re.match(r"^\*\*(.+?)\*\*:?$", s)
    if m:
        return m.group(1).strip(" :")
    if _BULLET_PREFIX_RE.match(s):
        return None
    words = s.rstrip(":").split()
    if len(words) > 8 or s.endswith((".", ",", ";")):
        return None
    if s.endswith(":"):
        return s.rstrip(":").strip()
    if len(words) <= 6 and (s.isupper() or any(p.search(s) for _, p in _SECTION_PATTERNS)):
        return s
    return None


def _kind(heading: str) -> SectionKind:
    for kind, pattern in _SECTION_PATTERNS:
        if pattern.search(heading):
            return kind
    return "other"


def split_sections(text: str) -> list[Section]:
    sections = [Section(kind="other")]
    for raw in text.splitlines():
        heading = _heading(raw)
        if heading is not None:
            sections.append(Section(kind=_kind(heading), heading=heading))
            continue
        line = _BULLET_PREFIX_RE.sub("", raw).strip()
        if line:
            sections[-1].lines.append(line)
    return [s for s in sections if s.lines or s.heading]


def extract_keywords(
    text: str,
    master: MasterData,
    vocab: TechVocabulary,
    project_sets: ProjectSetsFile,
    families: TechFamiliesFile,
) -> Keywords:
    sections = split_sections(text)
    found: dict[str, dict[str, object]] = {}
    for section in sections:
        body = "\n".join(([section.heading] if section.heading else []) + section.lines)
        for match in vocab.find(mask_comparisons(body)):
            entry = found.setdefault(match.canonical, {"spellings": [], "sections": [], "count": 0})
            if match.text not in entry["spellings"]:  # type: ignore[operator]
                entry["spellings"].append(match.text)  # type: ignore[attr-defined]
            if section.kind not in entry["sections"]:  # type: ignore[operator]
                entry["sections"].append(section.kind)  # type: ignore[attr-defined]
            entry["count"] = int(entry["count"]) + 1  # type: ignore[call-overload]

    skill_canonicals = {vocab.canonical(n) or n for g in master.skills for i in g.all_items() for n in [i.name, *i.aliases]}
    all_tech = {t for item in master.items() for t in item.allowed_tech}
    terms = []
    for canonical, entry in found.items():
        items = [i.id for i in master.items() if canonical in i.allowed_tech]
        members = {m for f in families.approved() if canonical in f.members for m in f.members if m != canonical}
        swaps = sorted(f"{i.id} ({m})" for i in master.projects if i.id not in items for m in members if m in i.allowed_tech)
        if items:
            status = "in_items"
        elif canonical in skill_canonicals or canonical in all_tech:
            status = "skills_only"
        else:
            status = "not_in_master"
        terms.append(TechTerm(
            canonical=canonical, spellings=entry["spellings"], sections=entry["sections"],  # type: ignore[arg-type]
            count=entry["count"], status=status, items=items, swaps=swaps,  # type: ignore[arg-type]
        ))
    terms.sort(key=lambda t: (-max(_WEIGHTS[s] for s in t.sections), -t.count, t.canonical.lower()))

    weight = {t.canonical: max(_WEIGHTS[s] for s in t.sections) for t in terms}
    scores = []
    for item in master.items():
        hits = sorted((c for c in weight if c in item.allowed_tech), key=str.lower)
        scores.append(ItemScore(id=item.id, kind=item.kind, score=sum(weight[c] for c in hits), tech=hits))
    scores.sort(key=lambda s: (-s.score, s.id))
    by_id = {s.id: s.score for s in scores}
    role_scores = []
    for rid, rt in project_sets.role_types.items():
        best_extra = max((by_id.get(p, 0) for p in rt.plus_one_of), default=0)
        role_scores.append(RoleTypeScore(id=rid, label=rt.label, score=sum(by_id.get(p, 0) for p in rt.projects) + best_extra))
    role_scores.sort(key=lambda r: (-r.score, r.id))

    signals = sorted({m.group().lower() for m in _OPEN_SOURCE_RE.finditer(text)})
    return Keywords(sections=sections, tech=terms, open_source_signals=signals, item_scores=scores,
                    role_type_scores=role_scores)
