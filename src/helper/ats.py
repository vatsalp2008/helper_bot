"""Which applicant tracking system a posting URL belongs to (spec 9 step 2). Adapters arrive in milestones 7 to 10."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

_HOSTS: list[tuple[str, str]] = [
    ("greenhouse", r"(^|\.)greenhouse\.io$"),
    ("lever", r"(^|\.)lever\.co$"),
    ("ashby", r"(^|\.)ashbyhq\.com$"),
    ("workday", r"(^|\.)myworkdayjobs\.com$|(^|\.)workday\.com$"),
    ("icims", r"(^|\.)icims\.com$"),
    ("smartrecruiters", r"(^|\.)smartrecruiters\.com$"),
    ("taleo", r"(^|\.)taleo\.net$"),
    ("successfactors", r"(^|\.)successfactors\.(com|eu)$|(^|\.)sapsf\.com$"),
    ("linkedin", r"(^|\.)linkedin\.com$"),
    ("handshake", r"(^|\.)joinhandshake\.com$"),
]


def detect_ats(url: Optional[str]) -> str:
    if not url:
        return "unknown"
    host = (urlparse(url).hostname or "").lower()
    for name, pattern in _HOSTS:
        if re.search(pattern, host):
            return name
    return "unknown"
