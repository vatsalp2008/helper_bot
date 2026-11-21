"""Workday: the posting from Workday's public job data, then the apply flow filled page by page.

Workday is the same software at every company. Posting links look like
  https://<tenant>.wd<N>.myworkdayjobs.com[/<lang>]/<site>/job/<location>/<title>_<req id>
  https://wd<N>.myworkdaysite.com[/<lang>]/recruiting/<tenant>/<site>/job/<location>/<title>_<req id>
and the same path under /wday/cxs/<tenant>/<site>/ returns the posting as JSON.

The apply flow is a series of pages (My Information, My Experience, Application Questions, Voluntary Disclosures,
Self Identify, Review). Many companies first ask for an account: helper signs in, or creates the account, with the
application login in .env (Vatsal, 2026-11-14). It never clicks Submit: the flow ends on the Review page.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

from ..answers import PlannedAnswer, match_option
from ..browser import captcha_visible
from ..errors import HelperError
from ..htmltext import html_to_text
from ..models import FormQuestion, Job
from ..tracker.db import now_iso
from . import common
from . import workday_page as wp
from .common import FieldResult

HOSTS = ("myworkdayjobs.com", "myworkdaysite.com", "workday.com")
ACCOUNT_FORM_WAIT = 30  # seconds for the sign-in form before helper says so (it keeps watching after)
_JOBS_RE = re.compile(r"^https?://(?P<tenant>[\w-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
                      r"(?P<site>[^/?#]+)/(?P<path>job/[^?#]+?)/?(?:/apply(?:/[^?#]*)?)?(?:[?#].*)?$")
_SITE_RE = re.compile(r"^https?://(?P<dc>wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/(?P<tenant>[^/]+)/"
                      r"(?P<site>[^/?#]+)/(?P<path>job/[^?#]+?)/?(?:/apply(?:/[^?#]*)?)?(?:[?#].*)?$")


@dataclass(frozen=True)
class WorkdayRef:
    host: str
    tenant: str
    site: str
    path: str  # "job/<location>/<title>_<req id>"
    recruiting: bool  # the myworkdaysite.com form of the link

    @property
    def api_url(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/{self.path}"

    @property
    def posting_url(self) -> str:
        prefix = f"recruiting/{self.tenant}/" if self.recruiting else ""
        return f"https://{self.host}/{prefix}{self.site}/{self.path}"

    @property
    def apply_url(self) -> str:
        return f"{self.posting_url}/apply/applyManually"

    @property
    def account_key(self) -> str:
        """What every posting of this company's Workday has in its link (one account per company)."""
        return f"{self.host}/recruiting/{self.tenant}/" if self.recruiting else self.host


def parse_ref(url: str) -> Optional[WorkdayRef]:
    for pattern, recruiting in ((_JOBS_RE, False), (_SITE_RE, True)):
        m = pattern.match(url.strip())
        if m:
            host = urlparse(url).hostname or ""
            return WorkdayRef(host.lower(), m["tenant"], m["site"], m["path"], recruiting)
    return None


def is_workday_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in HOSTS)


# --------------------------------------------------------------------------- intake


def fetch_job(url: str, session: Optional[Any] = None) -> Job:
    ref = parse_ref(url)
    if ref is None:
        raise HelperError(f"{url} isn't a Workday posting link (.../job/<location>/<title>_<id>)")
    session = session or requests.Session()
    try:
        r = session.get(ref.api_url, headers={"Accept": "application/json"}, timeout=30)
    except requests.RequestException as exc:
        raise HelperError(f"Couldn't reach Workday ({type(exc).__name__}). Check your internet connection and "
                          "try again.") from None
    if r.status_code in (403, 404):
        raise HelperError(f"Workday says this posting isn't open ({r.status_code}). If it is, save the job "
                          "description to a file and use `--jd-file`.")
    if r.status_code != 200:
        raise HelperError(f"Workday returned {r.status_code} for this posting")
    return job_from_api(r.json(), url, ref)


def _strip_code_word(name: str) -> str:
    """Drop a leading internal code word: one with a digit (C01, LS3002), or capitals that are the initials or the
    start of the words after it (GAIC Great American Insurance Company, LAB Labcorp). 'GE Vernova' keeps its GE."""
    parts = name.split()
    if len(parts) < 2:
        return name
    code, rest = parts[0], parts[1:]
    if re.fullmatch(r"[A-Z0-9]{2,8}", code) and re.search(r"\d", code) and re.search(r"[A-Za-z]", rest[0]):
        return " ".join(rest)
    if re.fullmatch(r"[A-Z]{3,6}", code):
        initials = "".join(w[0] for w in rest if w[:1].isalpha()).upper()
        if initials.startswith(code) or rest[0].upper().startswith(code):
            return " ".join(rest)
    return name


def company_name(data: dict[str, Any], ref: WorkdayRef) -> str:
    """'ADUS-Adobe Inc.' -> 'Adobe'. Falls back to the tenant name in the link."""
    name = ((data.get("hiringOrganization") or {}).get("name") or "").strip()
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)  # "(US01)"
    name = re.sub(r"^(?:[A-Z]{2,6}\d*|\d{2,6})\s*[-_:]\s*", "", name)  # an internal company code: "ADUS-"
    name = re.sub(r"^\d{1,6}\s+", "", name)  # "100 Salesforce", "1 DexCom"
    name = re.sub(r"(?i)^company\s+\d+\s*[-_:]\s*", "", name)  # "Company 19 - John Hancock..." (2026-11-17)
    name = _strip_code_word(name)  # "GAIC Great American...", "LAB Labcorp...", "C01 Carrier", "LS3002 GE Vernova"
    for _ in range(3):
        name = re.sub(r"[,\s]+(Inc|LLC|L\.L\.C|Ltd|Limited Partnership|Limited|Corp|Corporation|Co|N\.V|B\.V|GmbH|plc|S\.A|AG"
                      r"|SE|LP|L\.P)\.?$",
                      "", name, flags=re.I).strip(" ,.")
        name = re.sub(r"\s+(US|USA|U\.S\.|America|Americas|North America)$", "", name).strip(" ,.")
    return name or ref.tenant.replace("-", " ").title()


def job_from_api(data: dict[str, Any], url: str, ref: WorkdayRef) -> Job:
    info = data.get("jobPostingInfo") or {}
    if info.get("canApply") is False:
        raise HelperError("Workday says this posting no longer takes applications")
    html = info.get("jobDescription") or ""
    locations = [info.get("location"), *(info.get("additionalLocations") or [])]
    location = "; ".join(l for l in locations if l) or None
    country = ((info.get("country") or {}).get("descriptor") or "").strip()
    # "Remote" alone hides the country: GE Aerospace's Germany-only internship (2026-11-17)
    if country and not re.search(r"(?i)united states|\bUSA?\b", country) and country.lower() not in (location or "").lower():
        location = f"{location}, {country}" if location else country
    return Job(
        url=url, ats="workday", company=company_name(data, ref), role_title=(info.get("title") or "").strip(),
        location=location, job_id=str(info.get("jobReqId") or "") or None,
        description_text=html_to_text(html), description_html=html or None, questions=[], source="workday_api",
        fetched_at=now_iso(), apply_url=ref.apply_url,
    )


# --------------------------------------------------------------------------- filling


@dataclass
class Credentials:
    email: str
    password: str
    long_password: Optional[str] = None  # for sites that need a longer one (APPLICATION_PASSWORD_LONG)
    uses_long: bool = False  # this company's account was made with the long one (data/accounts.yaml)
    note_long: Optional[Callable[[], None]] = None  # records that it was

    def __repr__(self) -> str:  # never shows the password
        return f"Credentials({self.email!r}, password=***)"


@dataclass
class FlowContext:
    company: str
    plan: Callable[[list[FormQuestion]], list[PlannedAnswer]]  # plans one page's questions
    ask_claude: Callable[[list[PlannedAnswer], Callable[[], bool]], None]  # drafts written answers mid-fill
    resume_pdf: Path
    work: list[wp.WorkEntry]
    schools: list[wp.SchoolEntry]
    say: Callable[[str], None]
    pause: Callable[[], None]
    skills: list[str] = field(default_factory=list)  # the resume's skills lines
    certifications: list[dict[str, Any]] = field(default_factory=list)  # data/certifications.yaml
    websites: list[str] = field(default_factory=list)  # LinkedIn, GitHub, Portfolio
    credentials: Optional[Credentials] = None
    has_account: bool = False  # an earlier application or account at this company's Workday
    on_account: Callable[[str], None] = lambda kind: None  # records an account helper created or signed in to
    go_on: Callable[[], bool] = lambda: False  # Vatsal says go on (after verifying the account email)
    wait_seconds: float = 1800
    poll_seconds: float = 2.0


@dataclass
class FlowResult:
    results: list[FieldResult] = field(default_factory=list)
    answers: list[PlannedAnswer] = field(default_factory=list)
    reached_review: bool = False
    account: Optional[str] = None  # "signed in" | "created" | "by Vatsal"
    notes: list[str] = field(default_factory=list)
    done: set[str] = field(default_factory=set)  # slow sections already filled (skills, certifications)
    closed: Optional[str] = None  # the site's words when the posting has been taken down


def find_form(page: Any, timeout_ms: int = 20000) -> Optional[Any]:
    anchors = ("[data-automation-id=applyFlowPage]", "[data-automation-id=email]", "[data-automation-id=SignInWithEmailButton]")
    return common.find_form(page, anchors, hosts=HOSTS, timeout_ms=timeout_ms)


def is_confirmation(page: Any, form: Optional[Any] = None) -> bool:
    if common.is_confirmation(page, form, hosts=HOSTS):
        return True
    try:
        body = page.locator("body").inner_text(timeout=2000)
    except Exception:
        return False
    return bool(re.search(r"(?i)you(?:'|’)?ve (?:successfully )?applied|application submitted|successfully applied", body))


def is_submit_request(url: str, method: str) -> bool:
    return method == "POST" and is_workday_host(url) and bool(re.search(r"(?i)/submit", urlparse(url).path))


submit_clicked = wp.submit_clicked
submit_review = wp.submit_review
page_errors = wp.errors_shown


def run_flow(page: Any, ctx: FlowContext) -> FlowResult:
    """Fill every page up to Review. Returns early, with the window left to Vatsal, when he has to act."""
    out = FlowResult()
    deadline = time.monotonic() + ctx.wait_seconds
    filled_steps: dict[str, int] = {}
    lost = 0
    starts = 0  # Apply clicks that didn't open the application yet
    reloads = 0  # refreshes after Workday's "Something went wrong"
    trace: list[Any] = [None]
    while time.monotonic() < deadline:
        _settle(page)
        _dismiss_cookies(page)
        if captcha_visible(page):
            ctx.say("A CAPTCHA is showing: solve it in the browser window (helper never does, rule 6). Waiting...")
            if not _wait_until(page, ctx, lambda: not captcha_visible(page)):
                return out
        if wp.went_wrong(page):  # "Please refresh the page and then try again": do what it says, twice at most
            reloads += 1
            if reloads > 2:
                ctx.say("Workday keeps showing 'Something went wrong' after refreshing. Refresh the page in the window "
                        "(or open the application from Candidate Home); helper continues once its pages show.")
                if not _wait_until(page, ctx, lambda: not wp.went_wrong(page) and wp.current_step(page) is not None):
                    return out
                reloads = 0
                continue
            ctx.say("Workday showed 'Something went wrong. Please refresh the page'; helper refreshes it.")
            page.wait_for_timeout(5000 * reloads)
            page.reload(wait_until="domcontentloaded")
            continue
        step = wp.current_step(page)
        kind = wp.step_kind(step)
        _trace(page, step, kind, trace)
        if kind == "account" and not account_screen(page):
            # the step title can show before the sign-in form does (Symbotic, wd504): wait for the form, never Next
            def form_or_moved_on() -> bool:
                return bool(account_screen(page)) or wp.step_kind(wp.current_step(page)) != "account"

            if not _wait_until(page, ctx, form_or_moved_on, seconds=ACCOUNT_FORM_WAIT):
                ctx.say(f"Workday's account page hasn't shown its sign-in form after {ACCOUNT_FORM_WAIT} seconds. "
                        "helper keeps watching and signs in once it shows; you can also sign in or create the "
                        "account in the window yourself.")
                # the form can still come on a slow page (Symbotic, 2026-11-20, with 15 windows open): before, helper
                # waited for Vatsal alone and left the form it was there to fill
                if not _wait_until(page, ctx, form_or_moved_on):
                    return out
                if not account_screen(page):
                    out.account = out.account or "by Vatsal"
            continue
        if account_screen(page) and kind in ("account", "questions"):
            if not handle_account(page, ctx, out):
                return out
            continue
        if step is None:
            gone = wp.posting_gone(page)
            if gone:  # HPE (2026-11-19) waited a day on "The page you are looking for doesn't exist."
                ctx.say(f"This posting is closed: Workday says '{gone}'. Nothing was sent.")
                out.closed = gone
                return out
            if _start_apply(page):
                # an Apply button that never opens the application looped here silently for hours (Labcorp,
                # 2026-11-17): after a few tries, say so and leave it to Vatsal
                starts += 1
                if starts > 6:
                    ctx.say(f"helper clicked Workday's Apply button {starts} times, but the application never opened "
                            f"(page: {_short_url(page)}). Open the application in the window; helper continues once "
                            "its pages show.")
                    if not _wait_until(page, ctx, lambda: wp.current_step(page) is not None):
                        return out
                    starts = 0
                continue
            lost += 1
            if lost > 15:
                ctx.say("helper can't find Workday's application pages here. Fill the application in the window "
                        "yourself; helper still watches for your submit and logs it.")
                return out
            page.wait_for_timeout(1000)
            continue
        lost = 0
        if kind == "review":
            wp.install_submit_watch(page)
            out.reached_review = True
            return out
        filled_steps[step] = filled_steps.get(step, 0) + 1
        if filled_steps[step] > 2:  # back on a page helper already filled twice: leave it to Vatsal
            ctx.say(f"Workday keeps returning to '{step}'. Finish this page in the window and click Next; helper "
                    "continues on the next page.")
            if not _wait_for_new_step(page, ctx, step):
                return out
            continue
        missing = _fill_step(page, kind, step, ctx, out)
        if missing:
            ctx.say("Answer these yourself in the browser window, then click Next (helper continues on the next page):\n"
                    + "\n".join(f"  - {m}" for m in missing))
            if not _wait_for_new_step(page, ctx, step):
                return out
            continue
        try:
            wp.click_next(page)
        except RuntimeError:
            out.reached_review = True
            return out
        except Exception as exc:  # no Next button helper can click (Samsung, 2026-11-18): hand over, keep the window
            if filled_steps[step] < 2:  # a page still loading (HPE, 2026-11-18): fill it once more and try again
                page.wait_for_timeout(2000)
                continue
            ctx.say(f"helper couldn't click Workday's Next button on '{step}' ({str(exc).splitlines()[0][:100]}). "
                    "Click it yourself in the window; helper continues on the next page." + _page_says(page))
            if not _wait_for_new_step(page, ctx, step):
                return out
            continue
        if _wait_for_new_step(page, ctx, step, quiet=True, seconds=20):
            continue
        errors = wp.errors_shown(page)
        if any(re.search(r"(?i)school or university", e) for e in errors) and "education removed" not in out.done:
            out.done.add("education removed")
            gone = wp.delete_education(page)
            ctx.say(f"Workday wouldn't take the school, so helper removed {gone} education entr"
                    f"{'y' if gone == 1 else 'ies'} and goes on (Vatsal, 2026-11-16).")
            out.results = [r for r in out.results if not r.field.startswith("workday:school:")]
            try:
                wp.click_next(page)
            except RuntimeError:
                out.reached_review = True
                return out
            if _wait_for_new_step(page, ctx, step, quiet=True, seconds=20):
                continue
            errors = wp.errors_shown(page)
        if filled_steps[step] < 2:  # fields Workday redrew or loaded late: fill the page once more
            ctx.say(f"Workday flagged {len(errors) or 'some'} field(s) on '{step}'"
                    + (": " + "; ".join(e[:80] for e in errors[:3]) if errors else "") + ". helper fills the page again.")
            continue
        why = {r.field: r.detail for r in out.results if r.field.startswith("workday:school:")}  # the last pass's
        why = [f"  - {detail}" for detail in why.values() if detail]
        ctx.say(f"Workday didn't accept '{step}'" + (":\n" + "\n".join(f"  - {e}" for e in errors) if errors else ".")
                + ("\nWhat helper saw in the education entries:\n" + "\n".join(why) if why and kind == "experience"
                   else "")
                + "\nFix it in the window and click Next; helper continues on the next page.")
        if not _wait_for_new_step(page, ctx, step):
            return out
    ctx.say("helper ran out of time before the Review page; finish the application in the window.")
    return out


def _page_says(page: Any) -> str:
    """What the page shows, for a hand-over helper can't explain (HPE's My Information had no fields and no Next
    button, 2026-11-18): a screenshot in the run folder and the start of the page's text. The text goes on its own
    line: only a message's first line reaches the job-hunt list, and the page can show his name or email."""
    try:
        if wp.DEBUG_DIR is not None:
            page.screenshot(path=str(wp.DEBUG_DIR / "handover.png"), full_page=True)
        text = " ".join(page.locator("[data-automation-id=applyFlowPage], main, body").first.inner_text(
            timeout=3000).split())
    except Exception:
        return ""
    return f"\nThe page says: \"{text[:300]}{'...' if len(text) > 300 else ''}\"" if text else ""


def _settle(page: Any) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(600)


def _dismiss_cookies(page: Any) -> None:
    banner = page.locator("[data-automation-id=legalNoticeDeclineButton]:visible")
    try:
        if banner.count():
            banner.first.click()
            page.wait_for_timeout(500)
    except Exception:
        pass


def _short_url(page: Any) -> str:
    try:
        return str(page.url).split("?")[0]
    except Exception:
        return "?"


def _trace(page: Any, step: Optional[str], kind: str, last: list[Any]) -> None:
    """HELPER_DEBUG=1: one line each time the page, step or sign-in screen changes."""
    if not os.environ.get("HELPER_DEBUG"):
        return
    try:
        state = (_short_url(page), step, kind, account_screen(page))
    except Exception as exc:
        state = (_short_url(page), step, kind, f"error {type(exc).__name__}")
    if state != last[0]:
        last[0] = state
        print(f"[debug] page={state[0]} step={step!r} kind={kind} account_screen={state[3]!r}", flush=True)


def _start_apply(page: Any) -> bool:
    """On the posting itself or Workday's 'Start your application' choice: pick Apply Manually."""
    for selector in ("[data-automation-id=applyManually]:visible", "[data-automation-id=adventureButton]:visible"):
        target = page.locator(selector)
        if target.count():
            if os.environ.get("HELPER_DEBUG"):
                print(f"[debug] clicking {selector} on {_short_url(page)}", flush=True)
            target.first.click()
            page.wait_for_timeout(1500)
            return True
    return False


def _wait_until(page: Any, ctx: FlowContext, done: Callable[[], bool], seconds: Optional[float] = None) -> bool:
    end = time.monotonic() + (seconds if seconds is not None else ctx.wait_seconds)
    while time.monotonic() < end:
        try:
            if page.is_closed():
                return False
            if done():
                return True
            page.wait_for_timeout(ctx.poll_seconds * 1000)
        except Exception:
            return False
    return False


def _wait_for_new_step(page: Any, ctx: FlowContext, step: Optional[str], *, quiet: bool = False,
                       seconds: Optional[float] = None) -> bool:
    return _wait_until(page, ctx, lambda: wp.current_step(page) not in (step, None) or bool(account_screen(page)) and
                       wp.step_kind(step) != "account", seconds)


# --------------------------------------------------------------------------- one page


def _wait_for_fields(page: Any, seconds: float = 20) -> int:
    """Workday shows a page's heading before its fields arrive (right after sign-in especially). Wait until the
    fields are there and their number stops changing."""
    end = time.monotonic() + seconds
    last, steady = -1, 0
    while time.monotonic() < end:
        try:
            count = page.locator("[data-automation-id^='formField-']:visible").count()
        except Exception:
            count = 0
        steady = steady + 1 if count and count == last else 0
        if steady >= 2:
            return count
        last = count
        page.wait_for_timeout(700)
    return max(last, 0)


def _fill_step(page: Any, kind: str, step: str, ctx: FlowContext, out: FlowResult) -> list[str]:
    """Fill the page. Returns the required questions only Vatsal can answer."""
    _wait_for_fields(page)
    vatsals: list[str] = []
    if kind == "experience":
        out.results += wp.fill_work(page, ctx.work, ctx.pause)
        if "education removed" not in out.done:  # not again once Workday refused the school field
            out.results += wp.fill_education(page, ctx.schools, ctx.pause)
        for name, filler, items in (("skills", wp.fill_skills, ctx.skills),
                                    ("certifications", wp.fill_certifications, ctx.certifications)):
            if name not in out.done:  # a second pass over the page doesn't search them again
                out.results += filler(page, items, ctx.pause)
                out.done.add(name)
        out.results += wp.fill_websites(page, ctx.websites, ctx.pause)
        out.results += _upload_resume(page, ctx)
        vatsals += wp.required_gpa_fields(page)
    section = "eeoc" if kind in ("disclosures", "self_identify") else "questions"
    handled: set[str] = set()
    missing: list[str] = []
    # A follow-up shows only once its parent is answered, and can have its own: Marvell's export questions went three
    # deep (U.S. person? -> citizen of Cuba, Iran...? -> select all your countries) and helper, filling a page twice
    # at most, left the last one (2026-11-18). So after filling, look again and fill what appeared.
    for round_no in range(4):
        questions, found = wp.discover(page, skip_special=kind == "experience", section=section)
        questions = [q for q in questions if q.field_name not in handled]
        if not questions:
            break
        handled |= {q.field_name for q in questions}
        ctx.say(f"{step}: filling {len(questions)} question(s)." if round_no == 0 else
                f"{step}: {len(questions)} more question(s) appeared after those answers; filling them.")
        missing += _fill_questions(page, questions, found, step, ctx, out)
        page.wait_for_timeout(800)
    return vatsals + missing


def _fill_questions(page: Any, questions: list[Any], found: dict[str, Any], step: str, ctx: FlowContext,
                    out: FlowResult) -> list[str]:
    """Plan and fill one batch of a page's questions. Returns the required ones only Vatsal can answer."""
    answers = ctx.plan(questions)
    out.answers += answers
    open_items = [a for a in answers if a.kind == "free_text" or (a.kind == "unknown" and a.required)]
    if open_items:
        ctx.ask_claude(answers, lambda: wp.current_step(page) != step)
    missing = []
    for a in answers:
        if a.kind == "file":
            if a.key in ("resume", "transcript", "cover_letter"):
                out.results.append(fill_answer(page, a, found.get(a.field, wp.Found()), ctx))
            elif a.required:
                missing.append(a.label)  # a transcript or another document: Vatsal's
            continue
        if a.kind in ("skip", "unknown") or a.value is None:
            if a.required and a.kind != "skip":
                missing.append(a.label)
            continue
        result = fill_answer(page, a, found.get(a.field, wp.Found()), ctx)
        out.results.append(result)
        if not result.ok and a.required:
            missing.append(f"{a.label} ({result.detail})")
        ctx.pause()
    return missing


def _upload_resume(page: Any, ctx: FlowContext) -> list[FieldResult]:
    group = page.locator("[role=group][aria-labelledby='Resume/CV-section']")
    where = group if group.count() else page.locator("[data-automation-id^='formField-']:has(input[type=file])").first
    if not where.count():
        return []
    ok, shown, detail = wp.upload(page, where, ctx.resume_pdf)
    return [FieldResult("workday:resume", "Resume/CV", ok, shown, detail)]


def phone_digits(value: str) -> str:
    """'+12062800258' -> '2062800258': Workday has its own country code box."""
    digits = re.sub(r"\D", "", value)
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def any_source_but_referral(option: str) -> float:
    """"How did you hear" fallback: any option except a referral (CLAUDE.md)."""
    return 0.0 if not option.strip() or re.search(r"(?i)referr", option) else 1.0


def how_did_you_hear_terms(value: str, company: str) -> list[str]:
    c = company.strip()
    return [value, f"{c}.com", f"{c} careers", f"{c} career site", f"{c} website", "company website", "careers website",
            "career website", "company career site", "corporate website", "careers page", "Other"]


def fill_answer(page: Any, a: PlannedAnswer, found: wp.Found, ctx: FlowContext) -> FieldResult:
    where = wp.block(page, a.field)
    value = a.value or ""
    try:
        if not where.count():  # Workday redrew the section: tag the blocks again
            page.evaluate(wp.DISCOVER_JS)
            where = wp.block(page, a.field)
        if not where.count():
            return FieldResult(a.field, a.label, not a.required, None, "not on the page any more")
        if a.key == "phone":
            value = phone_digits(value)
        if a.type == "select":
            ok, shown, detail = wp.choose_dropdown(page, where, value)
        elif a.type == "prompt":
            if a.key == "how_did_you_hear":
                terms = how_did_you_hear_terms(value, ctx.company)
                accept = lambda t: max(match_option(term, [t])[1] for term in terms)  # noqa: E731
            else:
                terms = [value]
                accept = lambda t: match_option(value, [t])[1]  # noqa: E731
            ok, shown, detail = wp.choose_prompt(page, where, terms, accept)
            if not ok and a.key == "how_did_you_hear":  # Vatsal, 2026-11-16: social media or any option, and move on
                ok, shown, detail = wp.choose_prompt(page, where, ["Social Media", "LinkedIn", "Internet", "Job Board",
                                                                   "Other"], any_source_but_referral)
        elif a.type == "radio":
            ok, shown, detail = wp.check_choices(where, "radio", [value])
        elif a.type == "multi_select":
            ok, shown, detail = wp.check_choices(where, "checkbox", [v.strip() for v in value.split(";") if v.strip()])
        elif a.type == "boolean":
            if value.strip().lower() in ("yes", "true", "i agree", "agree", "checked") or value == a.label:
                box = where.locator("input[type=checkbox]").first
                # click-and-wait with the label as a fallback: Workday's consent box ignored check() (Camping World)
                ticked = common.tick(box)
                ok, shown, detail = ticked, "checked" if ticked else None, "" if ticked else "the box didn't stay ticked"
            else:
                ok, shown, detail = True, None, "left unchecked"
        elif a.type == "date":
            digits = wp.date_digits(value, found.dates or ["Month", "Day", "Year"])
            ok, shown, detail = bool(digits), wp.type_date(page, where, digits), "" if digits else f"can't read '{value}'"
        elif a.type == "file":
            ok, shown, detail = wp.upload(page, where, Path(a.value) if a.key in ("transcript", "cover_letter") and a.value else ctx.resume_pdf)
        else:
            box = where.locator("textarea, input:not([type=hidden]):not([type=checkbox]):not([type=radio])").first
            if box.input_value() != value:
                box.fill(value)
            ok, shown, detail = True, value, ""
    except Exception as exc:
        ok, shown, detail = False, value, f"{type(exc).__name__}: {str(exc)[:120]}"
    return FieldResult(a.field, a.label, ok, shown, detail)


# --------------------------------------------------------------------------- the account step


def account_screen(page: Any) -> Optional[str]:
    """'create', 'sign_in' or 'chooser' (Sign in with email / Apple / Google), else None."""
    try:
        if page.locator("[data-automation-id=verifyPassword]:visible").count():
            return "create"
        if page.locator("[data-automation-id=password]:visible").count():
            return "sign_in"
        if page.locator("[data-automation-id=SignInWithEmailButton]:visible").count():
            return "chooser"
    except Exception:
        return None
    return None


_ACCOUNT_OUTCOMES = (
    ("exists", r"already (exists|in use|registered|associated)|account (with|for) this email"),
    # words, not letters: "blocked" (a bot check) is no locked account (Rockwell, Symbotic, 2026-11-20)
    ("locked", r"\blocked\b|\btoo many (attempts|tries)\b"),
    ("bad_login", r"wrong (email|password)|incorrect|invalid (email|password|user|credentials|sign ?in)|"
                  r"(email|password).{0,40}(not match|isn't right|is not right)"),
    ("verify", r"verif|activat|confirm your (email|account)|check your (email|inbox)"),
    ("captcha", r"captcha"),
)


def _account_outcome(page: Any, ctx: FlowContext, seconds: float = 25) -> tuple[str, str]:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        page.wait_for_timeout(700)
        if not account_screen(page) and wp.step_kind(wp.current_step(page)) != "account":
            return "done", ""
        texts = []
        for selector in ("[data-automation-id=errorMessage]:visible", "[role=alert]:visible",
                         "[data-automation-id*=rror]:visible", "[data-automation-id*=alert]:visible"):
            try:
                texts += [" ".join(t.split()) for t in page.locator(selector).all_inner_texts() if t.strip()]
            except Exception:
                continue
        message = " ".join(dict.fromkeys(texts))
        for outcome, pattern in _ACCOUNT_OUTCOMES:
            if message and re.search(pattern, message, re.I):
                return outcome, message
        if captcha_visible(page):
            return "captcha", ""
        if message:
            return "error", message
    try:
        body = page.locator("body").inner_text(timeout=2000)
        if re.search(r"(?i)verif\w* (your )?(email|account)|we (have )?sent .{0,40}email", body):
            return "verify", ""
    except Exception:
        pass
    return "unknown", ""


def _show(page: Any, screen: str) -> None:
    """Switch the account box to the sign-in or create-account form."""
    if account_screen(page) == "chooser":
        page.locator("[data-automation-id=SignInWithEmailButton]").first.click()
        page.wait_for_timeout(1500)
    current = account_screen(page)
    if screen == "create" and current != "create":
        link = page.locator("[data-automation-id=createAccountLink]:visible")
        if link.count():
            link.first.click()
            page.wait_for_timeout(1500)
    if screen == "sign_in" and current != "sign_in":
        link = page.locator("[data-automation-id=signInLink]:visible")
        if link.count():
            link.first.click()
            page.wait_for_timeout(1500)


def _type_secret(page: Any, selector: str, secret: str) -> None:
    if not is_workday_host(page.url):
        raise RuntimeError("not a Workday page: the login is only typed on Workday's own sites")
    page.locator(selector).first.fill(secret)


def _submit_account_form(page: Any, button: str) -> None:
    overlay = page.locator("[data-automation-id=click_filter]:visible")
    if overlay.count() == 1:
        overlay.first.click()
    else:
        page.locator(f"[data-automation-id={button}]").first.click(force=True)


def _min_password_length(page: Any) -> Optional[int]:
    """The site's minimum password length, from its "Password Requirements" list (Visa: "A minimum of 12 characters")."""
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return None
    m = re.search(r"(?i)\b(?:minimum of|at least)\s+(\d{1,2})\s+characters", text)
    return int(m.group(1)) if m else None


def _create_account(page: Any, creds: Credentials) -> None:
    password = creds.password
    need = _min_password_length(page)
    if need and len(password) < need and creds.long_password and len(creds.long_password) >= need:
        password = creds.long_password  # Vatsal, 2026-11-16: the long one wherever the usual one is too short
        if creds.note_long:
            creds.note_long()
    _type_secret(page, "[data-automation-id=email]:visible", creds.email)
    _type_secret(page, "[data-automation-id=password]:visible", password)
    _type_secret(page, "[data-automation-id=verifyPassword]:visible", password)
    consent = page.locator("[data-automation-id=createAccountCheckbox]:visible")
    if consent.count() and not consent.first.is_checked():
        consent.first.check(force=True)
    _submit_account_form(page, "createAccountSubmitButton")


def _sign_in(page: Any, creds: Credentials) -> None:
    password = creds.long_password if creds.uses_long and creds.long_password else creds.password
    _type_secret(page, "[data-automation-id=email]:visible", creds.email)
    _type_secret(page, "[data-automation-id=password]:visible", password)
    _submit_account_form(page, "signInSubmitButton")


def handle_account(page: Any, ctx: FlowContext, out: FlowResult) -> bool:
    """Sign in, or create the account, once each at most (a second wrong password could lock the account)."""
    creds = ctx.credentials
    if creds is None:
        ctx.say("This company's Workday asks for an account, and WORKDAY_EMAIL / WORKDAY_PASSWORD aren't in .env. "
                "Sign in or create the account in the browser window; helper continues after that.")
        return _wait_for_account(page, ctx, out)
    if out.account == "created":  # created earlier in this fill and Workday asks to sign in: never create again
        order = ["sign_in"]
    else:
        order = ["sign_in", "create"] if ctx.has_account else ["create", "sign_in"]
    outcome, message = "unknown", ""
    for action in order:
        _show(page, action)
        if account_screen(page) != action:
            continue
        (_create_account if action == "create" else _sign_in)(page, creds)
        outcome, message = _account_outcome(page, ctx)
        if outcome == "done":
            out.account = "created" if action == "create" else "signed in"
            ctx.on_account(out.account)  # next time, sign in first (Bloomberg, Postman, 2026-11-16)
            ctx.say(f"Workday account: {out.account}.")  # never the login itself (CLAUDE.md: not printed or logged)
            return True
        if (action, outcome) in (("create", "exists"), ("sign_in", "bad_login")):
            continue
        break
    if outcome == "verify":
        ctx.say(f"Workday sent a verification email to {creds.email}. Open it and click the link. If Workday then asks "
                "you to sign in, either sign in yourself in the window, or tell Claude you verified and helper signs "
                "in for you.")
        while _wait_until(page, ctx, lambda: not account_screen(page) or ctx.go_on()):
            if not account_screen(page):
                out.account = "by Vatsal"
                return True
            _show(page, "sign_in")
            if account_screen(page) == "sign_in":
                _sign_in(page, creds)
                if _account_outcome(page, ctx)[0] == "done":
                    out.account = "signed in"
                    return True
            break
    shown = re.sub(r"[\w.%+-]+@[\w.-]+\.\w+", "<email>", message)[:160]  # the login is never printed
    reasons = {
        "bad_login": "Workday didn't accept the email and password, and the account already exists",
        "locked": "Workday says the account is locked",
        "captcha": "a CAPTCHA is showing (helper never solves one, rule 6)",
        "error": f"Workday said: {shown}",
        "unknown": "helper couldn't tell whether the account step worked",
        "exists": "an account with this email exists, but signing in didn't work",
    }
    said = f" (Workday: \"{shown}\")" if shown and outcome != "error" else ""  # his to read, word for word
    ctx.say(f"Paused at the account step: {reasons.get(outcome, outcome)}{said}. Sign in yourself in the window (or "
            "use 'Forgot your password?'); helper never retries a sign-in, and it continues once you're past this step.")
    return _wait_for_account(page, ctx, out)


def _wait_for_account(page: Any, ctx: FlowContext, out: FlowResult) -> bool:
    if _wait_until(page, ctx, lambda: not account_screen(page) and wp.step_kind(wp.current_step(page)) != "account"):
        out.account = out.account or "by Vatsal"
        return True
    return False
