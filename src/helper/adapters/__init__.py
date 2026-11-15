"""Which adapter handles a posting link (spec 10.2), and the sites helper refuses or leaves to Vatsal."""

from __future__ import annotations

from types import ModuleType

from ..ats import detect_ats
from ..errors import NeedsUser, NotBuiltYet

MANUAL = {"workday": "Workday", "icims": "iCIMS", "smartrecruiters": "SmartRecruiters", "taleo": "Taleo",
          "successfactors": "SuccessFactors"}


def adapter_for(url: str) -> ModuleType:
    from . import ashby, greenhouse, lever

    ats = detect_ats(url)
    if ats == "greenhouse" or greenhouse.parse_ref(url):
        return greenhouse
    if ats == "lever" and lever.parse_ref(url):
        return lever
    if (ats == "ashby" and ashby.parse_ref(url)) or "ashby_jid=" in url:
        return ashby
    if ats in ("linkedin", "handshake"):
        site = "LinkedIn Easy Apply" if ats == "linkedin" else "Handshake"
        raise NeedsUser(f"helper never automates {site} (rule 7). Find this job on the company's own careers page and "
                        "use that link. If you want, I can read the job description from this posting so the resume "
                        "can still be built: `helper resume --jd-file ...`.")
    if ats in MANUAL:
        raise NeedsUser(f"{MANUAL[ats]} needs manual or assisted handling, and usually an account, which helper never "
                        "creates (rule 8). `helper resume <url> --jd-file ...` still builds the resume for it.")
    raise NotBuiltYet("helper can't fill this site yet (a generic adapter arrives in milestone 10). "
                      "`helper resume <url> --jd-file ...` builds the resume.")
