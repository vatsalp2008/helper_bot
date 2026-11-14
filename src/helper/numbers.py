"""Find numbers in text, the same way for the master list and for resume bullets.

A number keeps the markers that carry meaning: `~` (approximate), a trailing `+` (lower bound), and
`<` `>` `≤` `≥` (bounds). Scale suffixes are folded into the value (10K == 10,000), so harmless
reformatting still matches. Digits glued to letters (p99, INT8, EC2, 3D, llama3.2) and dotted versions
(0.9.15) are identifiers, not numbers; find_identifier_tokens returns those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SCALE = {"k": 1e3, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_MULTI_CHAR_UNITS = ("ms", "MB", "GB", "TB", "KB")
_BOUND_MARKERS = "~<>≤≥"
_SIGNS = "+-−"  # plus, hyphen-minus, unicode minus
_ORDINAL_RE = re.compile(r"(st|nd|rd|th)(?![A-Za-z])")

# "one" is left out on purpose: it is too common in ordinary phrasing to treat as a metric.
WORD_NUMBERS = {
    "zero": 0, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "hundred": 100, "hundreds": 100, "thousand": 1e3, "thousands": 1e3, "million": 1e6, "millions": 1e6,
    "billion": 1e9, "billions": 1e9, "dozen": 12, "dozens": 12,
}
_WORD_RE = re.compile(  # (?!\.\w) skips names such as Three.js
    r"\b(" + "|".join(sorted(WORD_NUMBERS, key=len, reverse=True)) + r")\b(?!\.\w)", re.IGNORECASE
)
_DIGITS_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_TOKEN_SPLIT_RE = re.compile(r"[\s/→×—–;:()\[\]{}\"'!?`*|]+|,(?!\d)")


@dataclass(frozen=True)
class NumberMention:
    raw: str  # as written, with its markers: "~77%", "10M+", "$45,000", "<50ms", "two"
    value: float  # canonical value: "10M" -> 10000000.0, "45,000" -> 45000.0
    marker: str  # precision marker: "~", "<", ">", "≤", "≥", or "+" (trailing); "" when none
    start: int
    end: int
    is_word: bool = False


def _is_word_char(text: str, i: int) -> bool:
    return i < len(text) and (text[i].isalnum() or text[i] == "_")


def find_numbers(text: str, include_words: bool = True) -> list[NumberMention]:
    found: list[NumberMention] = []
    for m in _DIGITS_RE.finditer(text):
        s, e = m.start(), m.end()
        before = text[s - 1] if s > 0 else ""
        if before.isalpha() or before == "_":
            continue  # identifier such as p99, INT8, EC2, F1
        if before == "." and s >= 2 and text[s - 2].isdigit():
            continue  # tail of a dotted version such as 0.9.15
        if e + 1 < len(text) and text[e] == "." and text[e + 1].isdigit():
            continue  # head of a dotted version such as 0.9.15 or 19.4.22
        suffix = ""
        for unit in _MULTI_CHAR_UNITS:
            if text.startswith(unit, e) and not _is_word_char(text, e + len(unit)):
                suffix = unit
                break
        else:
            ch = text[e : e + 1]
            if ch in _SCALE and not _is_word_char(text, e + 1):
                suffix = ch
            elif ch in ("x", "X", "s") and not _is_word_char(text, e + 1):
                suffix = ch
            elif ch == "%":
                suffix = ch
            elif ch and (ch.isalpha() or ch == "_"):
                ordinal = _ORDINAL_RE.match(text, e)
                if not ordinal:
                    continue  # digits glued to letters: identifier such as 3D or 4a6992838e
                suffix = ordinal.group(1)
        j = e + len(suffix)
        plus = text[j : j + 1] == "+"
        if plus:
            j += 1
        # Prefix: optional "$", optional sign, optional bound marker (one space allowed: "p < 0.01").
        k = s
        if k > 0 and text[k - 1] == "$":
            k -= 1
        marker = ""
        if k > 0 and text[k - 1] in _BOUND_MARKERS:
            marker = text[k - 1]
            k -= 1
        elif k > 1 and text[k - 1] == " " and text[k - 2] in _BOUND_MARKERS:
            marker = text[k - 2]
            k -= 2
        elif k > 0 and text[k - 1] in _SIGNS and (k == 1 or not text[k - 2].isalnum()):
            k -= 1  # a sign such as +1.3 or -0.4: kept in raw, not a precision marker
            if k > 0 and text[k - 1] in _BOUND_MARKERS:
                marker = text[k - 1]
                k -= 1
        value = float(m.group().replace(",", ""))
        if suffix in _SCALE:
            value *= _SCALE[suffix]
        if not marker and plus:
            marker = "+"
        found.append(NumberMention(raw=text[k:j], value=value, marker=marker, start=k, end=j))
    if include_words:
        for m in _WORD_RE.finditer(text):
            found.append(NumberMention(
                raw=m.group(), value=float(WORD_NUMBERS[m.group().lower()]), marker="",
                start=m.start(), end=m.end(), is_word=True,
            ))
        found.sort(key=lambda n: n.start)
    return found


def find_identifier_tokens(text: str) -> set[str]:
    """Tokens mixing letters and digits that are not plain numbers: p99, INT8, EC2, 3D, mAP@0.7, v19.4.22."""
    tokens: set[str] = set()
    for chunk in _TOKEN_SPLIT_RE.split(text):
        token = chunk.strip(".-+")
        if not token or not any(c.isdigit() for c in token) or not any(c.isalpha() for c in token):
            continue
        if find_numbers(token, include_words=False):
            continue  # contains a real number (10M+, 0.8s, 3-row, I-1631); find_numbers already covers it
        tokens.add(token)
    return tokens


def same_value(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
