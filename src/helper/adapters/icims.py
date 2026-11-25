"""iCIMS (Vatsal, 2026-11-15): helper signs in, or creates the account, with ICIMS_EMAIL / ICIMS_PASSWORD from .env
(ICIMS_USERNAME where a site asks for a username), then fills the application page by page, reading each page by its
labels the way the generic adapter does.

- Every iCIMS career site runs the application inside an iframe (in_iframe=1). The company's own page around it can
  hold unrelated logins (City National's page has its online-banking sign-in), so the login is typed only into
  iCIMS's own email and password fields inside that frame, and never anywhere else.
- A CAPTCHA (iCIMS uses hCaptcha), an emailed code or a failed sign-in is left to Vatsal in the window; a sign-in is
  never retried.
- helper never clicks a button that says Submit. It stops there and Vatsal clicks it; if more pages follow, helper
  carries on with them. The run is review-only (no auto-submit).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from ..browser import captcha_visible
from ..errors import HelperError
from ..models import FormQuestion, Job
from ..tracker.db import now_iso
from . import common, generic
from .common import FieldResult
from .workday import FlowResult

HOST_RE = re.compile(r"(?i)^[\w.-]+\.icims\.com$")
MAX_PAGES = 30

_NEXT_RE = re.compile(r"(?i)^\s*(next|continue|save (and|&) (continue|next)|proceed|next step|log ?in|sign ?in|"
                      r"create (an |my )?account|register|start application|apply for this job online|"
                      r"update (my )?profile|save profile|"
                      # the email step's only button on Schwab's site (2026-11-24): a privacy notice, not an agreement
                      r"i acknowledge (the )?privacy (notice|policy|statement))\b")
_SUBMIT_RE = re.compile(r"(?i)\bsubmit\b|\bfinish\b|complete (my |your )?application|send (my )?application")
_USERNAME_RE = re.compile(r"(?i)\buser ?name\b|\blogin (id|name)\b|\buser id\b")
_EMAIL_LABEL_RE = re.compile(r"(?i)e-?mail")
_STOP_WORDS = ("Paused", "yourself")


@dataclass(frozen=True)
class IcimsRef:
    host: str
    job_id: str

    @property
    def job_url(self) -> str:  # the posting itself, without the company's page around it
        return f"https://{self.host}/jobs/{self.job_id}/job?in_iframe=1"

    @property
    def apply_url(self) -> str:
        return f"https://{self.host}/jobs/{self.job_id}/login"

    @property
    def account_key(self) -> str:
        return self.host


def is_icims_host(url: str) -> bool:
    return bool(HOST_RE.match(urlparse(url).hostname or ""))


def parse_ref(url: str) -> Optional[IcimsRef]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    m = re.match(r"^/jobs/(\d+)(?:/|$)", parsed.path)
    return IcimsRef(host, m.group(1)) if HOST_RE.match(host) and m else None


def fetch_job(url: str, session: Optional[Any] = None) -> Job:
    """The posting from its iCIMS page: every iCIMS posting carries schema.org JobPosting data."""
    import requests

    ref = parse_ref(url)
    if ref is None:
        raise HelperError(f"{url} isn't an iCIMS posting link (<company>.icims.com/jobs/<id>/...)")
    session = session or requests.Session()
    try:
        r = session.get(ref.job_url, timeout=30)
    except requests.RequestException as exc:
        raise HelperError(f"Couldn't reach iCIMS ({type(exc).__name__}). Check your internet connection and try "
                          "again.") from None
    if r.status_code != 200 or "notfound=1" in (getattr(r, "url", "") or "").lower():
        raise HelperError(f"iCIMS has no open job {ref.job_id} at {ref.host}; it may be closed. If it isn't, save the "
                          "job description as text and use `--jd-file`.")
    found = generic.job_posting_ld(generic._LD_RE.findall(r.text or ""))
    if found is None or not generic.readable(found.title, found.description):
        raise HelperError(f"couldn't read the job description at {url}. Save it as text and use `--jd-file`.")
    company = found.company or ref.host.split(".")[0].removeprefix("careers-").replace("-", " ").title()
    return Job(url=url, ats="icims", company=company, role_title=found.title or "Unknown role", location=found.location,
               job_id=ref.job_id, description_text=found.description, source="icims_page", fetched_at=now_iso(),
               apply_url=ref.apply_url)


# --------------------------------------------------------------------------- the flow


@dataclass
class Credentials:
    email: str
    password: str
    username: Optional[str] = None

    def __repr__(self) -> str:  # never shows the password
        return f"Credentials({self.email!r}, password=***, username={self.username!r})"


@dataclass
class FlowContext:  # the same shape as Workday's, so apply.py drives both
    company: str
    plan: Callable[[list[FormQuestion]], list[Any]]
    ask_claude: Callable[[list[Any], Callable[[], bool]], None]
    resume_pdf: Path
    work: list[Any]
    schools: list[Any]
    say: Callable[[str], None]
    pause: Callable[[], None]
    skills: list[str] = field(default_factory=list)
    certifications: list[dict[str, Any]] = field(default_factory=list)
    websites: list[str] = field(default_factory=list)
    credentials: Optional[Credentials] = None
    has_account: bool = False
    go_on: Callable[[], bool] = lambda: False
    wait_seconds: float = 1800
    poll_seconds: float = 2.0
    # apply.py passes it to both adapters (Workday's sign-in-first memory); iCIMS has no use for it yet. Missing here,
    # every iCIMS fill crashed before opening (Constellation, 2026-11-18).
    on_account: Callable[[str], None] = lambda kind: None


def app_frame(page: Any) -> Optional[Any]:
    """iCIMS's own application frame: an icims.com address with in_iframe=1 (never the company's page around it).
    iCIMS's IForms (the veteran and disability self-identification forms, Cotiviti 2026-11-15) open as a whole page
    instead, at .../form?form=...; that page is used for filling only (the login is typed in the frame alone)."""
    for frame in page.frames:
        parsed = urlparse(frame.url)
        if HOST_RE.match(parsed.hostname or "") and "in_iframe=1" in parsed.query:
            return frame
    top = urlparse(page.main_frame.url)
    if HOST_RE.match(top.hostname or "") and (re.search(r"/form\b", top.path) or "form=" in top.query):
        return page.main_frame
    return None


def _wait_frame(page: Any, seconds: float = 25) -> Optional[Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        frame = app_frame(page)
        if frame is not None:
            return frame
        page.wait_for_timeout(500)
    return None


# Which page this is: its address, headings and sign-in boxes (not its other fields, which can appear while a page is
# being filled). iCIMS swaps the email step for the password step without changing the address.
_SIGNATURE_JS = r"""() => {
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  return location.href.replace(/[?&](hashed|jan1offset|jun1offset|width|height)=[^&]*/g, '') + '|' +
    [...document.querySelectorAll('h1, h2, h3, legend, .iCIMS_PageTitle')].map(e => (e.innerText || '').trim()).join('/')
    + '|pw=' + [...document.querySelectorAll('input[type=password]')].filter(visible).length
    + '|email=' + [...document.querySelectorAll('input[name=css_loginName]')].filter(visible).length;
}"""

_BUTTONS_JS = r"""() => {
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  document.querySelectorAll('[data-helper-button]').forEach(e => e.removeAttribute('data-helper-button'));
  return [...document.querySelectorAll('button, input[type=submit], input[type=button], a[role=button], a.iCIMS_PrimaryButton, a.iCIMS_Button, [role=button]')]
    .filter(visible).map((e, i) => {
      e.setAttribute('data-helper-button', String(i));
      return {i, text: (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '').trim().slice(0, 80)};
    });
}"""


def signature(frame: Any) -> str:
    try:
        return str(frame.evaluate(_SIGNATURE_JS))
    except Exception:
        return ""


def _buttons(frame: Any) -> list[dict[str, Any]]:
    try:
        return list(frame.evaluate(_BUTTONS_JS))
    except Exception:
        return []


def _forward_button(frame: Any) -> tuple[Optional[dict[str, Any]], bool]:
    """(the button that moves the application on, whether it says Submit). Submit buttons are never clicked."""
    buttons = _buttons(frame)
    submit = next((b for b in reversed(buttons) if _SUBMIT_RE.search(b["text"])), None)
    nxt = next((b for b in reversed(buttons) if _NEXT_RE.match(b["text"]) and not _SUBMIT_RE.search(b["text"])), None)
    if nxt is not None:
        return nxt, False
    return submit, submit is not None


def _click(frame: Any, button: dict[str, Any]) -> bool:
    """Click a forward button. False when it stays disabled (a consent box to tick first, Constellation 2026-11-18):
    the caller hands the window over instead of waiting 30 seconds and crashing."""
    target = frame.locator(f"[data-helper-button='{button['i']}']").first
    for _ in range(10):
        try:
            if target.is_enabled():
                break
        except Exception:
            return False
        frame.wait_for_timeout(500)
    else:
        return False
    target.click(timeout=15000)
    return True


def _settle(page: Any, frame: Any) -> None:
    try:
        frame.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def _visible(frame: Any, selector: str) -> list[Any]:
    loc = frame.locator(selector)
    return [loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()]


def _type_secret(frame: Any, box: Any, value: str) -> bool:
    """Type the login only inside iCIMS's own frame, and the password only into password boxes."""
    parsed = urlparse(frame.url)
    if not (HOST_RE.match(parsed.hostname or "") and "in_iframe=1" in parsed.query):
        return False
    box.fill(value)
    return True


def _stage(frame: Any) -> str:
    passwords = _visible(frame, "input[type=password]")
    if len(passwords) >= 2:
        return "create"
    if len(passwords) == 1:
        return "password"
    if _visible(frame, "input[name=css_loginName], #enterEmailSubmitButton"):
        return "email"
    return "form"


def _wait_for_change(page: Any, before: str, ctx: FlowContext) -> str:
    """Wait while Vatsal acts in the window: 'confirmed', 'changed' (a new page) or 'timeout'."""
    deadline = time.monotonic() + ctx.wait_seconds
    while time.monotonic() < deadline:
        if is_confirmation(page):
            return "confirmed"
        frame = app_frame(page)
        if frame is not None and signature(frame) != before:
            return "changed"
        if ctx.go_on():
            return "changed"
        page.wait_for_timeout(int(ctx.poll_seconds * 1000))
    return "timeout"


def _moved(page: Any, before: str, seconds: float = 10) -> str:
    """After a click: 'changed' (the next page), 'captcha' (a check appeared) or 'same'."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if captcha_visible(page):
            return "captcha"
        frame = app_frame(page)
        if (frame is not None and signature(frame) != before) or is_confirmation(page):
            return "changed"
    return "same"


def _hand_over(page: Any, frame: Any, ctx: FlowContext, message: str) -> str:
    ctx.say(f"Paused for you: {message}")
    return _wait_for_change(page, signature(frame), ctx)


def run_flow(page: Any, ctx: FlowContext) -> FlowResult:
    out = FlowResult()
    frame = _wait_frame(page)
    if frame is None:
        ctx.say("helper couldn't find iCIMS's application frame on this page, so it typed nothing. Continue in the "
                "window yourself; helper still watches for the confirmation.")
        return out
    refilled: set[str] = set()
    reloads = 0
    for _ in range(MAX_PAGES):
        try:
            done = _one_page(page, app_frame(page) or frame, ctx, out, refilled)
        except Exception as exc:  # iCIMS reloaded its frame mid-step: take the new frame and look again
            if not _frame_replaced(exc) or reloads >= 5:
                raise
            reloads += 1
            page.wait_for_timeout(1500)
            frame = _wait_frame(page) or frame
            continue
        if done:
            return out
    ctx.say(f"helper stopped after {MAX_PAGES} pages; finish the application in the window.")
    return out


def _frame_replaced(exc: Exception) -> bool:
    return bool(re.search(r"(?i)frame was detached|execution context was destroyed|frame got detached", str(exc)))


def _one_page(page: Any, frame: Any, ctx: FlowContext, out: FlowResult, refilled: set[str]) -> bool:
    """Handle the page in front of helper. True when the flow is over (confirmed, or left to Vatsal for good)."""
    _settle(page, frame)
    if is_confirmation(page):
        out.reached_review = True
        return True
    if captcha_visible(page):
        return _hand_over(page, frame, ctx, "iCIMS is showing a CAPTCHA check. Solve it in the window; helper "
                                            "never does (rule 6), and carries on when the page moves.") == "timeout"
    if common.asks_for_emailed_code(page, frame):
        return _hand_over(page, frame, ctx, "iCIMS sent a code to your email. Enter it in the window; helper "
                                            "carries on when the page moves.") == "timeout"
    stage = _stage(frame)
    before = signature(frame)
    if stage in ("email", "password", "create"):
        return not _account_step(page, frame, stage, ctx, out, before)
    return not _form_page(page, frame, ctx, out, before, refilled)


# the consent text beside a box on the email step: a privacy notice is ticked, never anything binding
_PRIVACY_CONSENT_RE = re.compile(r"(?i)privacy|personal data|data protection|\bgdpr\b")
_BINDING_RE = re.compile(r"(?i)arbitrat|class action|waive|jury")
_CONSENT_TEXT_JS = r"""b => {
  const ids = (b.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
  const described = ids.map(id => (document.getElementById(id) || {}).innerText || '').join(' ');
  const label = b.labels && b.labels[0] ? b.labels[0].innerText : '';
  if (described.trim()) return described + ' ' + label;
  let text = '';  // the words around it, up to where another box's words begin
  for (let p = b, i = 0; i < 5 && p.parentElement; i++) {
    const up = p.parentElement;
    if ([...up.querySelectorAll('input[type=checkbox]')].some(x => x !== b)) break;
    p = up; text = up.innerText || '';
  }
  return text + ' ' + label;
}"""


def _email_step_extras(frame: Any, ctx: FlowContext) -> list[FieldResult]:
    """The rest of the email step. Medpace's Next stays disabled until "I accept" (its privacy notice and consent to
    processing personal data) is ticked; Schwab's refused the email without Country Code and Phone Number (both
    2026-11-24). Only those: the account's other details come after."""
    results: list[FieldResult] = []
    for box in _visible(frame, "input[type=checkbox]"):
        try:
            words = str(box.evaluate(_CONSENT_TEXT_JS))
        except Exception:
            continue
        if _PRIVACY_CONSENT_RE.search(words) and not _BINDING_RE.search(words):
            results.append(FieldResult("icims:privacy_consent", "Privacy notice consent", common.tick(box), "ticked",
                                       "the email step's privacy consent"))
    questions = [q for q in generic.discover(frame) if q.type in ("text", "select") and not _EMAIL_LABEL_RE.search(q.label)]
    # Schwab labels its phone box just "Number", beside "Phone Country Code": its name (css_phoneNumber) says phone
    questions = [q.model_copy(update={"label": "Phone Number"})
                 if q.type == "text" and re.search(r"(?i)phone", q.field_name or "") and not re.search(r"(?i)\bphone\b", q.label)
                 else q for q in questions]
    answers = [a for a in ctx.plan(questions) if a.key in ("phone", "phone_country_code")] if questions else []
    if answers:
        results += generic.fill_form(frame, answers, ctx.resume_pdf, country=None, city=None, pause=ctx.pause)
    return results


def _account_step(page: Any, frame: Any, stage: str, ctx: FlowContext, out: FlowResult, before: str) -> bool:
    creds = ctx.credentials
    if creds is None:
        return _hand_over(page, frame, ctx, "there's no iCIMS login in .env (ICIMS_EMAIL, ICIMS_PASSWORD). Sign in or "
                                            "create the account in the window.") != "timeout"
    if stage == "email":
        box = (_visible(frame, "input[name=css_loginName]") or _visible(frame, "input[type=email]"))
        if not box or not _type_secret(frame, box[0], creds.email):
            return _hand_over(page, frame, ctx, "helper couldn't find iCIMS's email box. Sign in in the window.") \
                != "timeout"
        out.account = out.account or "email entered"
        out.results += _email_step_extras(frame, ctx)
    elif stage == "password":
        if not _type_secret(frame, _visible(frame, "input[type=password]")[0], creds.password):
            return _hand_over(page, frame, ctx, "sign in in the window.") != "timeout"
        out.account = "signed in"
    else:  # create the account: names and other questions from the profile, then the password twice
        questions = [q for q in generic.discover(frame)]
        answers = ctx.plan(questions)
        for a in answers:
            if _USERNAME_RE.search(a.label) and creds.username:
                a.value, a.kind, a.status, a.source = creds.username, "auto", "ready", "ICIMS_USERNAME"
            elif _EMAIL_LABEL_RE.search(a.label) and a.type in ("text", "email"):
                a.value, a.kind, a.status, a.source = creds.email, "auto", "ready", "ICIMS_EMAIL"
        out.results += generic.fill_form(frame, answers, ctx.resume_pdf, country=None, city=None, pause=ctx.pause)
        for box in _visible(frame, "input[type=password]"):
            _type_secret(frame, box, creds.password)
        out.account = "created"
    button, says_submit = _forward_button(frame)
    if button is None or says_submit:
        return _hand_over(page, frame, ctx, "helper couldn't find the button that goes on from iCIMS's sign-in page. "
                                            "Click it in the window.") != "timeout"
    if not _click(frame, button):
        return _hand_over(page, frame, ctx, "iCIMS's button to go on stays disabled (often a consent box to tick "
                                            "first). Tick it and go on in the window.") != "timeout"
    if _moved(page, before) == "same":
        frame = app_frame(page) or frame
        errors = common.visible_errors(frame)
        what = {"email": "your email", "password": "the password", "create": "the new account's details"}[stage]
        out.account = None if stage != "email" else out.account
        if errors:
            why = f"iCIMS didn't accept {what} ({'; '.join(errors[:3])})"
        else:
            why = f"the page didn't go on after helper entered {what}. If a CAPTCHA is showing, solve it"
        return _hand_over(page, frame, ctx, f"{why}. helper never retries a sign-in; finish this step in the window "
                                            "and it carries on from the next page.") != "timeout"
    return True


def _form_page(page: Any, frame: Any, ctx: FlowContext, out: FlowResult, before: str, refilled: set[str]) -> bool:
    questions = generic.discover(frame)
    answers = ctx.plan(questions) if questions else []
    if answers:
        ctx.ask_claude(answers, lambda: signature(app_frame(page) or frame) != before)
        out.results += generic.fill_form(frame, answers, ctx.resume_pdf, country=None, city=None, pause=ctx.pause)
        out.answers += answers
    missing = [a for a in answers if a.required and (a.kind == "unknown" or a.value is None) and a.type != "file"]
    button, says_submit = _forward_button(frame)
    if says_submit:
        out.reached_review = True
        label = button["text"] if button else "Submit"
        note = (" Answer these first: " + "; ".join(a.label for a in missing[:6]) + ".") if missing else ""
        ctx.say(f"helper stops here: review the application in the window and click '{label}' yourself (helper never "
                f"clicks Submit).{note} If more pages follow, helper carries on with them.")
        result = _wait_for_change(page, signature(frame), ctx)
        if result == "changed":
            out.reached_review = False
        return result == "changed"
    if missing:
        if _hand_over(page, frame, ctx, "these required questions need your answer in the window: "
                                        + "; ".join(a.label for a in missing[:6]) + ". Click Next when done.") != "changed":
            return False
        return True
    if button is None:
        return _hand_over(page, frame, ctx, "helper couldn't find a Next button on this page. Go on in the window.") \
            == "changed"
    pre_click = signature(frame)
    if not _click(frame, button):
        return _hand_over(page, frame, ctx, "iCIMS's Next button stays disabled (often a consent box to tick first). "
                                            "Tick it and click Next in the window.") == "changed"
    moved = _moved(page, pre_click)
    after = app_frame(page) or frame
    if moved == "same":  # the page didn't go on
        errors = common.visible_errors(after)
        why = "; ".join(errors[:3]) if errors else "it stayed on the same page"
        if before not in refilled:  # fill it once more, then leave it to Vatsal
            refilled.add(before)
            ctx.say(f"iCIMS didn't accept the page ({why}); filling it once more.")
            return True
        return _hand_over(page, after, ctx, f"iCIMS still doesn't accept this page ({why}). Fix it in the window and "
                                            "click Next.") == "changed"
    return True


# --------------------------------------------------------------------------- after the flow


def is_confirmation(page: Any, form: Optional[Any] = None) -> bool:
    frame = app_frame(page)
    if common.is_confirmation(page, frame, hosts=("icims.com",)):
        return True
    try:
        text = (frame or page).locator("body").inner_text(timeout=2000)[:3000]
    except Exception:
        return False
    return bool(re.search(r"(?i)you have successfully (applied|submitted)|your application is complete", text))


def is_submit_request(url: str, method: str) -> bool:
    return method == "POST" and is_icims_host(url) and "submit" in urlparse(url).query.lower()


def find_form(page: Any, timeout_ms: int = 20000) -> Optional[Any]:
    return _wait_frame(page, timeout_ms / 1000)
