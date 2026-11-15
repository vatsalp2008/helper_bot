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
        raise HelperError(f"can't reach Workday ({type(exc).__name__})") from None
    if r.status_code in (403, 404):
        raise HelperError(f"Workday says this posting isn't open ({r.status_code}). If it is, save the job "
                          "description to a file and use `--jd-file`.")
    if r.status_code != 200:
        raise HelperError(f"Workday returned {r.status_code} for this posting")
    return job_from_api(r.json(), url, ref)


def company_name(data: dict[str, Any], ref: WorkdayRef) -> str:
    """'ADUS-Adobe Inc.' -> 'Adobe'. Falls back to the tenant name in the link."""
    name = ((data.get("hiringOrganization") or {}).get("name") or "").strip()
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)  # "(US01)"
    name = re.sub(r"^(?:[A-Z]{2,6}\d*|\d{2,6})\s*[-_:]\s*", "", name)  # an internal company code: "ADUS-"
    name = re.sub(r"^\d{2,6}\s+", "", name)  # "100 Salesforce"
    for _ in range(3):
        name = re.sub(r"[,\s]+(Inc|LLC|L\.L\.C|Ltd|Limited|Corp|Corporation|Co|N\.V|B\.V|GmbH|plc|S\.A|AG|SE|LP|L\.P)\.?$",
                      "", name, flags=re.I).strip(" ,.")
        name = re.sub(r"\s+(US|USA|U\.S\.|America|Americas|North America)$", "", name).strip(" ,.")
    return name or ref.tenant.replace("-", " ").title()


def job_from_api(data: dict[str, Any], url: str, ref: WorkdayRef) -> Job:
    info = data.get("jobPostingInfo") or {}
    if info.get("canApply") is False:
        raise HelperError("Workday says this posting no longer takes applications")
    html = info.get("jobDescription") or ""
    locations = [info.get("location"), *(info.get("additionalLocations") or [])]
    return Job(
        url=url, ats="workday", company=company_name(data, ref), role_title=(info.get("title") or "").strip(),
        location="; ".join(l for l in locations if l) or None, job_id=str(info.get("jobReqId") or "") or None,
        description_text=html_to_text(html), description_html=html or None, questions=[], source="workday_api",
        fetched_at=now_iso(), apply_url=ref.apply_url,
    )


# --------------------------------------------------------------------------- filling


@dataclass
class Credentials:
    email: str
    password: str

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
    credentials: Optional[Credentials] = None
    has_account: bool = False  # an earlier application at this company's Workday
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


def run_flow(page: Any, ctx: FlowContext) -> FlowResult:
    """Fill every page up to Review. Returns early, with the window left to Vatsal, when he has to act."""
    out = FlowResult()
    deadline = time.monotonic() + ctx.wait_seconds
    filled_steps: dict[str, int] = {}
    lost = 0
    while time.monotonic() < deadline:
        _settle(page)
        _dismiss_cookies(page)
        if captcha_visible(page):
            ctx.say("A CAPTCHA is showing: solve it in the browser window (helper never does, rule 6). Waiting...")
            if not _wait_until(page, ctx, lambda: not captcha_visible(page)):
                return out
        step = wp.current_step(page)
        kind = wp.step_kind(step)
        if account_screen(page) and kind in ("account", "questions"):
            if not handle_account(page, ctx, out):
                return out
            continue
        if step is None:
            if _start_apply(page):
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
        if _wait_for_new_step(page, ctx, step, quiet=True, seconds=20):
            continue
        errors = wp.errors_shown(page)
        if filled_steps[step] < 2:  # fields Workday redrew or loaded late: fill the page once more
            ctx.say(f"Workday flagged {len(errors) or 'some'} field(s) on '{step}'; helper fills the page again.")
            continue
        ctx.say(f"Workday didn't accept '{step}'" + (":\n" + "\n".join(f"  - {e}" for e in errors) if errors else ".")
                + "\nFix it in the window and click Next; helper continues on the next page.")
        if not _wait_for_new_step(page, ctx, step):
            return out
    ctx.say("helper ran out of time before the Review page; finish the application in the window.")
    return out


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


def _start_apply(page: Any) -> bool:
    """On the posting itself or Workday's 'Start your application' choice: pick Apply Manually."""
    for selector in ("[data-automation-id=applyManually]:visible", "[data-automation-id=adventureButton]:visible"):
        target = page.locator(selector)
        if target.count():
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
    if kind == "experience":
        out.results += wp.fill_work(page, ctx.work, ctx.pause)
        out.results += wp.fill_education(page, ctx.schools, ctx.pause)
        out.results += _upload_resume(page, ctx)
    section = "eeoc" if kind in ("disclosures", "self_identify") else "questions"
    questions, found = wp.discover(page, skip_special=kind == "experience", section=section)
    if not questions:
        return []
    ctx.say(f"{step}: filling {len(questions)} question(s).")
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
        elif a.type == "radio":
            ok, shown, detail = wp.check_choices(where, "radio", [value])
        elif a.type == "multi_select":
            ok, shown, detail = wp.check_choices(where, "checkbox", [v.strip() for v in value.split(";") if v.strip()])
        elif a.type == "boolean":
            if value.strip().lower() in ("yes", "true", "i agree", "agree", "checked") or value == a.label:
                box = where.locator("input[type=checkbox]").first
                if not box.is_checked():
                    box.check(force=True)
                ok, shown, detail = True, "checked", ""
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
    ("locked", r"locked|too many (attempts|tries)"),
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


def _create_account(page: Any, creds: Credentials) -> None:
    _type_secret(page, "[data-automation-id=email]:visible", creds.email)
    _type_secret(page, "[data-automation-id=password]:visible", creds.password)
    _type_secret(page, "[data-automation-id=verifyPassword]:visible", creds.password)
    consent = page.locator("[data-automation-id=createAccountCheckbox]:visible")
    if consent.count() and not consent.first.is_checked():
        consent.first.check(force=True)
    _submit_account_form(page, "createAccountSubmitButton")


def _sign_in(page: Any, creds: Credentials) -> None:
    _type_secret(page, "[data-automation-id=email]:visible", creds.email)
    _type_secret(page, "[data-automation-id=password]:visible", creds.password)
    _submit_account_form(page, "signInSubmitButton")


def handle_account(page: Any, ctx: FlowContext, out: FlowResult) -> bool:
    """Sign in, or create the account, once each at most (a second wrong password could lock the account)."""
    creds = ctx.credentials
    if creds is None:
        ctx.say("This company's Workday asks for an account, and WORKDAY_EMAIL / WORKDAY_PASSWORD aren't in .env. "
                "Sign in or create the account in the browser window; helper continues after that.")
        return _wait_for_account(page, ctx, out)
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
            ctx.say(f"Workday account: {out.account} ({creds.email}).")
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
    reasons = {
        "bad_login": "Workday didn't accept the email and password, and the account already exists",
        "locked": "Workday says the account is locked",
        "captcha": "a CAPTCHA is showing (helper never solves one, rule 6)",
        "error": f"Workday said: {message}",
        "unknown": "helper couldn't tell whether the account step worked",
        "exists": "an account with this email exists, but signing in didn't work",
    }
    ctx.say(f"Paused at the account step: {reasons.get(outcome, outcome)}. Sign in yourself in the window (or use "
            "'Forgot your password?'); helper never retries a sign-in, and it continues once you're past this step.")
    return _wait_for_account(page, ctx, out)


def _wait_for_account(page: Any, ctx: FlowContext, out: FlowResult) -> bool:
    if _wait_until(page, ctx, lambda: not account_screen(page) and wp.step_kind(wp.current_step(page)) != "account"):
        out.account = out.account or "by Vatsal"
        return True
    return False
