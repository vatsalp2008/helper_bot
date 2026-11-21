"""Which adapter handles a posting link (spec 10.2), and the sites helper refuses or leaves to Vatsal."""

from __future__ import annotations

from types import ModuleType

from ..ats import detect_ats
from ..errors import NeedsUser

MANUAL = {"smartrecruiters": "SmartRecruiters", "taleo": "Taleo", "successfactors": "SuccessFactors"}


def adapter_for(url: str, purpose: str = "apply") -> ModuleType:
    """The adapter for a posting link. `purpose` "resume" only reads the posting (no form), so sites helper never
    fills can still give their job description for a resume and cover letter."""
    from . import ashby, generic, greenhouse, icims, lever, oracle, workday

    ats = detect_ats(url)
    if ats == "greenhouse" or greenhouse.parse_ref(url):
        return greenhouse
    if ats == "lever" and lever.parse_ref(url):
        return lever
    if (ats == "ashby" and ashby.parse_ref(url)) or "ashby_jid=" in url:
        return ashby
    if workday.parse_ref(url):
        return workday
    if icims.parse_ref(url):
        return icims
    if oracle.parse_ref(url):  # Oracle's Candidate Experience, page by page (2026-11-19)
        return oracle
    if ats == "workday":
        raise NeedsUser("This Workday link isn't a job posting (it should contain /job/<location>/<title>_<id>). Open "
                        "the posting on Workday and use that link.")
    if ats in ("linkedin", "handshake"):
        site = "LinkedIn Easy Apply" if ats == "linkedin" else "Handshake"
        raise NeedsUser(f"helper never automates {site} (rule 7). Find this job on the company's own careers page and "
                        "use that link. If you want, I can read the job description from this posting so the resume "
                        "can still be built: `helper resume --jd-file ...`.")
    if ats in MANUAL:
        if purpose == "resume":
            from . import smartrecruiters

            if smartrecruiters.parse_ref(url):  # its pages turn helper's browser away; its public API doesn't
                return smartrecruiters
            return generic  # the posting page is public: read its job description
        raise manual_stop(MANUAL[ats], url)
    return generic  # anything else: the best-effort adapter, which always stops before Submit


def manual_stop(site: str, url: str) -> NeedsUser:
    return NeedsUser(f"helper doesn't fill {site} applications: they need an account at each company, and helper never "
                     "creates one there (rule 8; the account exceptions cover Workday and iCIMS only). Apply in your "
                     "own browser. "
                     f"helper still builds the tailored resume and cover letter from this posting: `helper resume {url}`. "
                     "After you apply, `helper mark <id> applied` logs it.")
