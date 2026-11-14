"""Apply one item's wording rules (from master.yaml) to resume text."""

from __future__ import annotations

import re
from typing import Optional

from .models import WordingRule


def violations(rule: WordingRule, text: str, title: Optional[str] = None) -> list[str]:
    """Readable reasons why `text` or `title` breaks `rule`; empty when it passes.

    `text` is one bullet for a rule with scope "bullet", and the whole entry (tech line and every
    bullet, joined) for a rule with scope "entry".
    """
    found = []
    for pattern in rule.forbid:
        m = re.search(pattern, text)
        if m:
            found.append(f"{rule.id}: '{m.group()}' breaks the rule: {rule.description}")
    for req in rule.require:
        if re.search(req.when, text) and not re.search(req.must, text):
            found.append(f"{rule.id}: {rule.description}")
    for conflict in rule.conflicts:
        a, b = re.search(conflict.a, text), re.search(conflict.b, text)
        if a and b:
            found.append(f"{rule.id}: '{a.group()}' and '{b.group()}' can't both appear: {rule.description}")
    if rule.title_prefix and title is not None and not title.startswith(rule.title_prefix):
        found.append(f"{rule.id}: the title must start with '{rule.title_prefix}'")
    return found
