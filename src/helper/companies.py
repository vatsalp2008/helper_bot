"""Company-name matching shared by the eligibility check, caps, and the duplicate check."""

from __future__ import annotations

import re

_SUFFIX_RE = re.compile(r"(?i)\b(?:inc|llc|ltd|corp|corporation|co|company|plc|gmbh|technologies)\b\.?")


def norm_company(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", _SUFFIX_RE.sub(" ", name).lower()).split())


def contains_word(haystack: str, needle: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])", haystack, re.IGNORECASE))


def same_company(a: str, b: str) -> bool:
    """"Salesforce" and "Salesforce, Inc." match; "Meta" and "Metabase" don't."""
    na, nb = norm_company(a), norm_company(b)
    return bool(na and nb) and (na == nb or contains_word(na, nb) or contains_word(nb, na))


def norm_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", title.lower()).split())
