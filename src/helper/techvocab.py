"""Find technology names in text with one shared vocabulary: the master list plus data/tech_lexicon.yaml.

`helper convert` uses it to collect each item's technologies from that item's own text. The resume
validator uses the same vocabulary to spot technologies in bullets, so both sides always agree on what
counts as a technology and how its spellings map to one canonical name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Never split these on "/" (they are one name, not two).
NO_SLASH_SPLIT = {"CI/CD", "shadcn/ui", "A/B", "I/O", "TCP/IP", "PL/SQL", "Pub/Sub", "Google Pub/Sub"}

_PARENS_RE = re.compile(r"^(?P<base>.*?)\s*\((?P<inner>[^()]*(?:\([^()]*\)[^()]*)*)\)\s*$")
_VERSION_RE = re.compile(r"\s+v?\d+(?:\.\d+)*$")
_STYLE_RE = re.compile(r"(?:[\w.]+/)*[\w.]+-style\b")
_VS_RE = re.compile(r"\bvs\.?\s+[^,;:)\n]*", re.IGNORECASE)
_MASK = "\x00"


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` outside parentheses."""
    parts, depth, current = [], 0, []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and text.startswith(sep, i):
            parts.append("".join(current))
            current = []
            i += len(sep)
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def split_parenthetical(text: str) -> tuple[str, Optional[str]]:
    """'AWS (ECS, RDS)' -> ('AWS', 'ECS, RDS'). Only a trailing parenthetical counts."""
    m = _PARENS_RE.match(text.strip())
    if not m or not m.group("base").strip():
        return text.strip(), None
    return m.group("base").strip(), m.group("inner").strip()


def looks_like_name(text: str) -> bool:
    """True for proper-name-like text ('Rust', 'Cosmos DB', 'Gemma 4'); False for descriptions ('strict')."""
    words = text.split()
    if not words or len(words) > 4:
        return False
    return all(w[0].isupper() or w[0].isdigit() or any(c.isupper() for c in w[1:]) for w in words)


def is_tech_like(name: str) -> bool:
    """Skills-section entries such as 'prompt engineering' or 'Multi-agent systems' are concepts, not tools."""
    words = name.split()
    if not words:
        return False
    if len(words) == 1:
        word = words[0]
        return word[0].isupper() or any(c.isupper() or c.isdigit() or c in ".#+/*" for c in word)
    return not any(w.isalpha() and w.islower() for w in words[1:])


def split_tech_item(item: str) -> list[str]:
    """One Technologies-line entry -> every name it contains.

    'AWS (ECS, RDS, VPC, IAM)' -> AWS, ECS, RDS, VPC, IAM;  'Java 21' -> Java 21, Java;
    'LSTM/GRU' -> LSTM/GRU, LSTM, GRU;  'Yjs + y-websocket' -> Yjs, y-websocket;  'TypeScript (strict)' -> TypeScript
    """
    item = item.replace("**", "").strip()
    base, inner = split_parenthetical(item)
    names: list[str] = []
    for part in re.split(r"\s+\+\s+", base):
        pieces = [p.strip() for p in part.split("/")] if "/" in part and part not in NO_SLASH_SPLIT else [part]
        if len(pieces) > 1 and all(len(p) >= 2 for p in pieces):
            names.extend(pieces)  # "LSTM/GRU" names two technologies, not a third one
        else:
            names.append(part)
    if inner:
        for piece in split_top_level(inner):
            if looks_like_name(piece):
                names.append(piece)
    for name in list(names):
        stripped = _VERSION_RE.sub("", name)
        if stripped and stripped != name:
            names.append(stripped)
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return list(seen)


def mask_comparisons(text: str) -> str:
    """Blank out comparisons that are not claims: 'Airbnb/Uber/Stripe-style', 'vs 150MB+ Electron'."""
    for rx in (_STYLE_RE, _VS_RE):
        text = rx.sub(lambda m: _MASK * len(m.group()), text)
    return text


@dataclass(frozen=True)
class TechMatch:
    canonical: str
    text: str
    start: int
    end: int


@dataclass
class _Entry:
    spelling: str
    canonical: str
    pattern: re.Pattern[str]


def _pattern(spelling: str, case_sensitive: bool) -> re.Pattern[str]:
    parts = [re.escape(p) for p in re.split(r"[\s-]+", spelling) if p]
    body = r"[\s-]+".join(parts)
    if not case_sensitive and spelling[-1].isalpha():
        body += r"(?:s|es)?"
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(r"(?<![\w.#+-])" + body + r"(?![\w#+²³])", flags)


class TechVocabulary:
    def __init__(
        self,
        spellings: dict[str, str],
        case_sensitive: set[str],
        ignore: Iterable[str] = (),
        exclude: Iterable[str] = (),
    ) -> None:
        """`exclude`d spellings still resolve to their canonical name but are never matched in text."""
        self._canonical_by_spelling = dict(spellings)
        self._lower: dict[str, str] = {}
        for spelling, canonical in spellings.items():
            if spelling not in case_sensitive:
                self._lower.setdefault(spelling.lower(), canonical)
        self._case_sensitive = set(case_sensitive)
        excluded = set(exclude)
        self._entries = sorted(
            (_Entry(s, c, _pattern(s, s in case_sensitive)) for s, c in spellings.items() if s not in excluded),
            key=lambda e: len(e.spelling),
            reverse=True,
        )
        self._ignore = [_pattern(p, True) for p in sorted(ignore, key=len, reverse=True)]

    @property
    def canonical_names(self) -> set[str]:
        return set(self._canonical_by_spelling.values())

    def canonical(self, name: str) -> Optional[str]:
        name = name.strip()
        if name in self._canonical_by_spelling:
            return self._canonical_by_spelling[name]
        if name in self._case_sensitive:
            return None
        return self._lower.get(name.lower())

    def find(self, text: str) -> list[TechMatch]:
        masked = text
        for rx in self._ignore:
            masked = rx.sub(lambda m: _MASK * len(m.group()), masked)
        found: list[TechMatch] = []
        for entry in self._entries:
            hits = list(entry.pattern.finditer(masked))
            for m in hits:
                found.append(TechMatch(entry.canonical, text[m.start() : m.end()], m.start(), m.end()))
            for m in reversed(hits):
                masked = masked[: m.start()] + _MASK * (m.end() - m.start()) + masked[m.end() :]
        return sorted(found, key=lambda t: t.start)

    def find_canonical(self, text: str) -> set[str]:
        return {m.canonical for m in self.find(text)}


def build_vocabulary(lexicon: dict[str, Any], master_terms: Iterable[str]) -> TechVocabulary:
    """Lexicon aliases first (they decide canonical names), then common names, then master-list names."""
    spellings: dict[str, str] = {}
    by_lower: dict[str, str] = {}

    def resolve(name: str) -> str:
        return spellings.get(name) or by_lower.get(name.lower()) or name

    def add(spelling: str, canonical: str) -> None:
        spelling = spelling.strip()
        if not spelling or spelling in spellings:
            return
        spellings[spelling] = canonical
        by_lower.setdefault(spelling.lower(), canonical)

    for canonical, aliases in (lexicon.get("aliases") or {}).items():
        add(str(canonical), str(canonical))
        for alias in aliases or []:
            add(str(alias), str(canonical))
    for names in (lexicon.get("common") or {}).values():
        for name in names or []:
            add(str(name), resolve(str(name)))
    for term in master_terms:
        base = _VERSION_RE.sub("", term)
        if base and base != term:  # "React 19" is a spelling of React, not its own technology
            add(base, resolve(base))
            add(term, resolve(base))
        else:
            add(term, resolve(term))

    case_sensitive = {str(s) for s in lexicon.get("case_sensitive") or []}
    case_sensitive |= {s for s in spellings if len(s) <= 2}
    return TechVocabulary(
        spellings,
        case_sensitive,
        ignore=[str(i) for i in lexicon.get("ignore") or []],
        exclude=[str(e) for e in lexicon.get("exclude") or []],
    )
