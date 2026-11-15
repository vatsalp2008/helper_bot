"""Which adapter handles a posting link (spec 10.2), and the sites helper refuses or leaves to Vatsal."""

from __future__ import annotations

from types import ModuleType

from ..ats import detect_ats
from ..errors import NeedsUser, NotBuiltYet

MANUAL = {"workday": "Workday", "icims": "iCIMS", "smartrecruiters": "SmartRecruiters", "taleo": "Taleo",
          "successfactors": "SuccessFactors"}


def adapter_for(url: str) -> ModuleType:
    from . import greenhouse

    ats = detect_ats(url)
    if ats == "greenhouse" or greenhouse.parse_ref(url):
        return greenhouse
    if ats in ("linkedin", "handshake"):
        site = "LinkedIn Easy Apply" if ats == "linkedin" else "Handshake"
        raise NeedsUser(f"helper never automates {site} (rule 7). Find this job on the company's own careers page and "
                        "use that link. If you want, I can read the job description from this posting so the resume "
                        "can still be built: `helper resume --jd-file ...`.")
    if ats in ("lever", "ashby"):
        raise NotBuiltYet(f"{ats.title()} applications arrive in milestone 8. For now: `helper resume --jd-file ...` "
                          "builds the resume, and you apply by hand.")
    if ats in MANUAL:
        raise NeedsUser(f"{MANUAL[ats]} needs manual or assisted handling, and usually an account, which helper never "
                        "creates (rule 8). `helper resume <url> --jd-file ...` still builds the resume for it.")
    raise NotBuiltYet("helper can't fill this site yet (a generic adapter arrives in milestone 10). "
                      "`helper resume <url> --jd-file ...` builds the resume.")
