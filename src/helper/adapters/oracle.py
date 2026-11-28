"""Oracle Recruiting Cloud's Candidate Experience (Vatsal asked for it, 2026-11-19; Vertiv, Fortinet, Ralliant).

- The posting's Apply goes to .../job/<id>/apply/email: an email box, a hidden "honeypot" box that must stay empty,
  and Next. helper types the application email from the profile there and nothing else. Oracle may then email a
  one-time code; only Vatsal enters it, in the window.
- The application is the job's own flow (recruitingCEApplyFlows, readable before the email: Personal Info, Job
  Application Questions, Experience, More About You, Review and Submit on Vertiv's). Its questions can't be read before
  the code (the metadata answers 401), so each page is read by its labels when it shows, the way the iCIMS and
  company-site adapters do, and Claude is asked mid-fill for what helper's rules can't answer.
- helper never clicks Submit here: it stops at the Review page for Vatsal (review-only while this adapter is new).
  Each page's markup is saved in the run folder (oracle_page_N.html) so the adapter can be tuned to real pages.
"""

from __future__ import annotations

import calendar
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from ..browser import captcha_visible
from ..errors import HelperError
from ..htmltext import html_to_text
from ..models import Job
from .. import mailcodes
from . import common, generic
from .icims import Credentials, FlowContext  # noqa: F401  (the same shapes, so apply.py drives every flow alike)
from .workday import FlowResult

HOST_RE = re.compile(r"(?i)(^|\.)oraclecloud\.com$")
# Requisition ids can start with letters ("R271283"), and a company can serve Candidate Experience from its own
# domain (Dell's enterpriseplatform.dell.com, 2026-11-23): the app's own path is what makes the page Oracle's
_PATH_RE = re.compile(r"(?i)/hcmUI/CandidateExperience/([^/]+)/sites/([^/]+)/(?:[^/]+/)*?(?:job|preview)/([A-Za-z]*\d+)")
_APP_PATH_RE = re.compile(r"(?i)^/hcmUI/CandidateExperience/")


def is_oracle_page(url: str) -> bool:
    """Oracle's own Candidate Experience app: on oraclecloud.com, or on a company domain under its /hcmUI path."""
    parsed = urlparse(url or "")
    return bool(HOST_RE.search(parsed.hostname or "") or _APP_PATH_RE.search(parsed.path or ""))
MAX_PAGES = 15
DEBUG_DIR: Optional[Path] = None  # set per run by apply.py: each page's markup goes there

# never "Continue Working", the answer to Oracle's idle check (_keep_session)
_NEXT_RE = re.compile(r"(?i)^\s*(next|continue(?!\s+working)|save (and|&) continue|proceed)\b")
_SUBMIT_RE = re.compile(r"(?i)^\s*submit\b|\bsubmit (my |your )?application\b")
_APPLY_RE = re.compile(r"(?i)^\s*apply( now)?\s*$")
_CONSENT_RE = re.compile(r"(?i)\b(agree|accept|terms|consent|privacy|acknowledge)\b")
_HONEYPOT_RE = re.compile(r"(?i)honey ?pot")


@dataclass(frozen=True)
class OracleRef:
    host: str
    lang: str
    site: str
    job_id: str

    @property
    def job_url(self) -> str:
        return f"https://{self.host}/hcmUI/CandidateExperience/{self.lang}/sites/{self.site}/job/{self.job_id}"

    @property
    def apply_url(self) -> str:
        return f"{self.job_url}/apply/email"


def parse_ref(url: str) -> Optional[OracleRef]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    m = _PATH_RE.search(parsed.path)
    return OracleRef(host, m.group(1), m.group(2), m.group(3)) if is_oracle_page(url) and m else None


# Oracle's own job API (public, read-only) has the posting's text without the page around it: the page read took in
# its map credits, "Similar Jobs" and "TRENDING" (Ralliant, 2026-11-19: "OpenStreetMap" became a keyword) and found
# no location.
_DETAILS_URL = ("https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                "&finder=ById;Id=%22{job_id}%22,siteNumber={site}")
_TEXT_PARTS = ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr",
               "OrganizationDescriptionStr", "CorporateDescriptionStr")


@dataclass
class Details:
    title: str
    location: Optional[str]
    html: str
    text: str


def job_details(ref: OracleRef, session: Optional[Any] = None) -> Optional[Details]:
    """The posting from Oracle's job API, or None when it doesn't answer (the page's own text is used then)."""
    import requests

    try:
        r = (session or requests).get(_DETAILS_URL.format(host=ref.host, job_id=ref.job_id, site=ref.site),
                                      headers={"ora-irc-language": ref.lang}, timeout=30)
        items = (r.json().get("items") or []) if r.status_code == 200 else []
    except (requests.RequestException, ValueError, AttributeError):
        return None
    item = items[0] if items and isinstance(items[0], dict) else {}
    parts = [item.get(k) or "" for k in _TEXT_PARTS]
    text = "\n\n".join(html_to_text(p) for p in parts if p.strip())
    if not text.strip():
        return None
    places = [item.get("PrimaryLocation") or ""]
    places += [p.get("Name") or "" for p in item.get("secondaryLocations") or [] if isinstance(p, dict)]
    return Details(title=(item.get("Title") or "").strip(), location="; ".join(dict.fromkeys(p for p in places if p))
                   or None, html="\n".join(p for p in parts if p.strip()), text=text)


# words that come before a company's name in a sentence without being part of it ("About Ralliant", "At Vertiv")
_NOT_A_NAME = {"about", "at", "join", "with", "the", "our", "for", "in", "to", "from", "by", "why", "how", "welcome",
               "life", "careers", "career", "working", "work", "inside", "within", "across", "as", "and", "of", "on",
               "is", "are", "this", "that", "your", "we", "you", "a", "an", "where", "here", "when", "who", "what"}


def fuller_employer(company: str, text: str) -> str:
    """A site named for a division ("Aerospace": Honeywell's Oracle site, 2026-11-20) isn't the employer's whole name:
    the posting's own longer name for it ("Honeywell Aerospace") when that's how it mostly says it."""
    name = (company or "").strip()
    if not name or " " in name:
        return company
    mentions = len(re.findall(rf"\b{re.escape(name)}\b", text or ""))
    fuller = Counter(w for w in re.findall(rf"\b([A-Z][\w&'.-]*) {re.escape(name)}\b", text or "")
                     if w.lower() not in _NOT_A_NAME)
    if not fuller:
        return company
    word, n = fuller.most_common(1)[0]
    return f"{word} {name}" if n >= 2 and n * 2 >= mentions else company


def fetch_job(url: str, session: Optional[Any] = None) -> Job:
    """The posting: its company from the page, and its title, location and text from Oracle's job API when that
    answers (the page's own text otherwise). The window opens its apply page."""
    ref = parse_ref(url)
    if ref is None:
        raise HelperError(f"{url} isn't an Oracle posting link (.../hcmUI/CandidateExperience/<lang>/sites/<site>/job/<id>)")
    job = generic.fetch_job(url, session)
    details = job_details(ref, session)
    if details is not None:
        job.role_title = details.title or job.role_title
        job.location = details.location or job.location
        job.description_text, job.description_html = details.text, details.html
    # "JPMC Candidate Experience page": the site's name with the employer in front (2026-11-23)
    job.company = fuller_employer(generic.site_employer(job.company) or job.company, job.description_text)
    job.ats, job.job_id, job.apply_url = "oracle", job.job_id or ref.job_id, ref.apply_url
    # the boxes a posting page shows are Oracle's email step (Email Address, the honeypot, the privacy box; Honeywell,
    # 2026-11-20), not the application's questions, which come page by page during the fill
    job.questions = []
    return job


# --------------------------------------------------------------------------- the page in front of helper

# Which page this is: its address, its headings and step, and whether the email or code boxes are there (not its
# other fields, which change while the page is being filled). Oracle's idle check is a dialog over the same page.
_SIGNATURE_JS = r"""() => {
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  const heads = [...document.querySelectorAll('h1, h2, h3, legend, [role=heading], [aria-current=step]')]
    .filter(e => visible(e) && !e.closest('.session-expire-dialog'))
    .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 12).join('/');
  return location.pathname + '|' + heads
    + '|email=' + [...document.querySelectorAll('input[name=primary-email]')].filter(visible).length
    + '|submit=' + [...document.querySelectorAll('button')].filter(b => visible(b) && /^\s*submit\b/i.test(b.innerText || '')).length;
}"""

_BUTTONS_JS = r"""() => {
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  document.querySelectorAll('[data-helper-button]').forEach(e => e.removeAttribute('data-helper-button'));
  return [...document.querySelectorAll('button, input[type=submit], input[type=button], a[role=button], [role=button]')]
    .filter(e => visible(e) && !e.closest('.session-expire-dialog')).map((e, i) => {
      e.setAttribute('data-helper-button', String(i));
      const text = (e.innerText || e.value || '').trim() || e.getAttribute('aria-label') || e.title || '';
      return {i, text: text.replace(/\s+/g, ' ').slice(0, 80)};
    });
}"""


def signature(page: Any) -> str:
    try:
        return str(page.main_frame.evaluate(_SIGNATURE_JS))
    except Exception:
        return ""


def _buttons(page: Any) -> list[dict[str, Any]]:
    try:
        return list(page.main_frame.evaluate(_BUTTONS_JS))
    except Exception:
        return []


def _button(page: Any, pattern: re.Pattern[str]) -> Optional[dict[str, Any]]:
    return next((b for b in reversed(_buttons(page)) if pattern.search(b["text"])), None)


def _click(page: Any, button: dict[str, Any]) -> bool:
    """False when the button stays disabled (a box to tick first): the window is handed over instead."""
    target = page.main_frame.locator(f"[data-helper-button='{button['i']}']").first
    for _ in range(10):
        try:
            if target.is_enabled():
                break
        except Exception:
            return False
        page.wait_for_timeout(500)
    else:
        return False
    target.click(timeout=15000)
    return True


def _visible(page: Any, selector: str) -> list[Any]:
    loc = page.main_frame.locator(selector)
    return [loc.nth(i) for i in range(loc.count()) if loc.nth(i).is_visible()]


# Honeywell (2026-11-20, a Friday night): Oracle's "Planned Outage" page, which helper reported as "couldn't find a Next
# button". Its own sentence only: an energy posting can ask for "planned outage" experience.
_OUTAGE_RE = re.compile(r"(?i)\bservice you are trying to access is (?:currently )?undergoing scheduled maintenance\b")


def _stage(page: Any) -> str:
    try:
        if _OUTAGE_RE.search(page.main_frame.locator("body").inner_text(timeout=2000)[:3000]):
            return "outage"
    except Exception:
        pass
    if _visible(page, "input[name=primary-email]"):
        return "email"
    if common.asks_for_emailed_code(page):
        return "code"
    if "/apply" not in urlparse(page.url).path and _button(page, _APPLY_RE):
        return "posting"
    try:  # every application page carries Oracle's apply-flow markup; the job search and profile pages don't
        in_flow = bool(page.main_frame.locator("[class*=apply-flow]").count())
    except Exception:
        in_flow = True
    if not in_flow and "/apply" not in urlparse(page.url).path:
        return "left"  # Ralliant and Honeywell (2026-11-20): Oracle ended a 13-hour session and showed its job search
    return "form"


# Oracle draws each step after its page loads: the first look at Vertiv's (2026-11-19) found only the empty shell
_DRAWN = ("input[name=primary-email]", "[id^=pin-code-]", ".apply-flow-pagination__button",
          "input:not([type=hidden]), select, textarea")


def _settle(page: Any, seconds: float = 20) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if any(_visible(page, s) for s in _DRAWN) or is_confirmation(page):
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    page.wait_for_timeout(800)
    _keep_session(page)


# Oracle's idle check (Ralliant and Vertiv, 2026-11-19): about half an hour after the last save, "Are You Still With
# Us?" counts down 3 minutes and then signs the candidate out, and the filled application goes with the session.
# helper keeps the window open for Vatsal (stuck_window_hours), so it answers "Continue Working" while it waits.
_IDLE_CHECK = ".session-expire-dialog"


def _keep_session(page: Any, ctx: Optional[FlowContext] = None) -> bool:
    try:
        # the pattern runs in the page as a JavaScript regex: a flag, never an inline (?i)
        buttons = page.main_frame.locator(f"{_IDLE_CHECK} button", has_text=re.compile(r"\bcontinue\b", re.IGNORECASE))
        for i in range(buttons.count()):
            if buttons.nth(i).is_visible():
                buttons.nth(i).click(timeout=5000)
                if ctx is not None:
                    ctx.say("Oracle asked if anyone is still there; helper clicked 'Continue Working' so the filled "
                            "application stays open.")
                return True
    except Exception:
        pass
    return False


def _page_moved(page: Any, before: str, ctx: Optional[FlowContext] = None) -> bool:
    """While helper waits (for Claude's answers, or for Vatsal): the idle check is answered first, so it never counts
    as a new page and the session doesn't end under the wait."""
    _keep_session(page, ctx)
    return signature(page) != before


def _wait_for_change(page: Any, before: str, ctx: FlowContext) -> str:
    """Wait while Vatsal acts in the window: 'confirmed', 'changed' (a new page) or 'timeout'."""
    deadline = time.monotonic() + ctx.wait_seconds
    while time.monotonic() < deadline:
        if is_confirmation(page):
            return "confirmed"
        if _page_moved(page, before, ctx) or ctx.go_on():
            return "changed"
        page.wait_for_timeout(int(ctx.poll_seconds * 1000))
    return "timeout"


_SAVING_RE = re.compile(r"(?i)\bsaving job application info\b")  # shown only once SUBMIT is clicked
_PROFILE_PATH_RE = re.compile(r"/my-profile/?$")
_SUBMITTED: set[int] = set()  # pages (id) where Oracle took the SUBMIT; is_confirmation reads it
# pages (id) that reached the SUBMIT page: landing on the profile later means it went in, even when helper had left
# its wait at SUBMIT because Vatsal went back to a section (Dell, 2026-11-23: logged "closed" after his submit)
_AT_SUBMIT: set[int] = set()


def _landed_after_submit(page: Any) -> bool:
    """A page that reached SUBMIT and now shows the candidate's profile: Oracle took the application."""
    if id(page) in _AT_SUBMIT and _PROFILE_PATH_RE.search(urlparse(page.url).path):
        _SUBMITTED.add(id(page))
        return True
    return False


def _wait_at_submit(page: Any, ctx: FlowContext) -> str:
    """At SUBMIT, Vatsal reviews and submits: 'confirmed', 'changed' or 'timeout'.

    Oracle redraws sections while he reviews (new field ids, headings shown and hidden), which isn't a new page, and
    refilling it could undo what he changed (Vertiv, 2026-11-20): only leaving the page counts. After his SUBMIT,
    Oracle shows "Creating candidate info / Saving job application info" and lands on the candidate's profile
    (/my-profile), which is its confirmation (Ralliant, 2026-11-20: logged "not submitted" before)."""
    path = urlparse(page.url).path
    saving = False
    deadline = time.monotonic() + ctx.wait_seconds
    while time.monotonic() < deadline:
        if is_confirmation(page):
            return "confirmed"
        _keep_session(page, ctx)
        try:
            saving = saving or bool(_SAVING_RE.search(page.main_frame.locator("body").inner_text(timeout=2000)))
        except Exception:
            pass
        here = urlparse(page.url).path
        if here != path and (saving or _PROFILE_PATH_RE.search(here)):
            _SUBMITTED.add(id(page))
            return "confirmed"
        if here != path or (not saving and _button(page, _SUBMIT_RE) is None) or ctx.go_on():
            return "changed"  # another page: an emailed code, an error page, Oracle's job search after a session end
        page.wait_for_timeout(int(ctx.poll_seconds * 1000))
    return "timeout"


def _moved(page: Any, before: str, seconds: float = 12) -> str:
    """After a click: 'changed' (the next page), 'captcha' (a check appeared) or 'same'."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if captcha_visible(page):
            return "captcha"
        if signature(page) != before or is_confirmation(page):
            return "changed"
    return "same"


_VERIFY_RE = re.compile(r"(?i)^\s*verify\s*$")


def _emailed_code(page: Any, ctx: FlowContext, out: FlowResult) -> bool:
    """Oracle's "Confirm your identity" code, read from Gmail and typed in (Vatsal, 2026-11-27). Once per run: a second
    code page (a wrong or expired code) is his. True when Oracle took the code and moved on."""
    if ctx.from_email is None or "emailed code" in out.done:
        return False
    out.done.add("emailed code")
    ctx.say("Oracle emailed a one-time code; helper is reading it from Gmail (up to 4 minutes).")
    code = ctx.from_email("oracle", time.time() - 180, urlparse(page.url).hostname or "")
    if not code:
        ctx.say("No verification code email came from this company's Oracle site.")
        return False
    if not mailcodes.enter_code(page.main_frame, code):
        ctx.say("helper couldn't type the emailed code into Oracle's code boxes.")
        return False
    for _ in range(30):
        if _stage(page) != "code":
            break
        button = _button(page, _VERIFY_RE)
        if button is not None and _click(page, button):
            page.wait_for_timeout(1500)
        else:
            page.wait_for_timeout(500)
    if _stage(page) == "code":
        ctx.say("Oracle still asks for the code after helper typed it in.")
        return False
    ctx.say("helper typed in the code Oracle emailed (read from Gmail, never shown), and Oracle took it.")
    return True


def _hand_over(page: Any, ctx: FlowContext, message: str) -> str:
    ctx.say(f"Paused for you: {message}")
    return _wait_for_change(page, signature(page), ctx)


def _dump(page: Any, n: int) -> None:
    if DEBUG_DIR is None:
        return
    try:
        (DEBUG_DIR / f"oracle_page_{n}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- the flow


def run_flow(page: Any, ctx: FlowContext) -> FlowResult:
    out = FlowResult()
    ref = parse_ref(page.url)
    if ref is not None and "/apply" not in urlparse(page.url).path:  # the posting page: go to its apply page
        page.goto(ref.apply_url, wait_until="domcontentloaded")
    refilled: set[str] = set()
    for n in range(1, MAX_PAGES + 1):
        if _one_page(page, ctx, out, refilled, n):
            return out
    ctx.say(f"helper stopped after {MAX_PAGES} pages; finish the application in the window.")
    return out


def _one_page(page: Any, ctx: FlowContext, out: FlowResult, refilled: set[str], n: int) -> bool:
    """Handle the page in front of helper. True when the flow is over (confirmed, or left to Vatsal for good)."""
    _settle(page)
    _landed_after_submit(page)
    if is_confirmation(page):
        out.reached_review = True
        return True
    if captcha_visible(page):
        return _hand_over(page, ctx, "Oracle is showing a CAPTCHA check. Solve it in the window; helper never does "
                                     "(rule 6), and carries on when the page moves.") == "timeout"
    stage = _stage(page)
    _dump(page, n)
    if stage == "posting":
        button = _button(page, _APPLY_RE)
        if button is None or not _click(page, button):
            return _hand_over(page, ctx, "click Apply in the window.") == "timeout"
        return False
    if stage == "email":
        return not _email_step(page, ctx, out)
    if stage == "code":
        if _emailed_code(page, ctx, out):
            return False
        return _hand_over(page, ctx, "Oracle emailed you a verification code. Enter it in the window; helper carries "
                                     "on with the application when the page moves.") == "timeout"
    if stage == "outage":
        return _hand_over(page, ctx, "Oracle's site is down for scheduled maintenance (its 'Planned Outage' page), so "
                                     "nothing was filled. Reload the window once it's back and helper carries on, or "
                                     "close it and try again later.") == "timeout"
    if stage == "left":
        return _hand_over(page, ctx, "Oracle left the application and shows another page (it ends sessions left open "
                                     "for many hours). helper never clicks SUBMIT on Oracle, so nothing was sent unless "
                                     "you submitted it. To start again, close this window and tell Claude.") == "timeout"
    return not _form_page(page, ctx, out, refilled)


def _email_step(page: Any, ctx: FlowContext, out: FlowResult) -> bool:
    """Type the application email (never into the honeypot box), tick the terms if asked, and go on."""
    before = signature(page)
    email = ctx.credentials.email if ctx.credentials else ""
    box = _visible(page, "input[name=primary-email]")
    if not is_oracle_page(page.url):  # the email goes only into Oracle's own page
        box = []
    if not email or not box:
        return _hand_over(page, ctx, "type your email in the window and click Next.") != "timeout"
    box[0].fill(email)
    for tick in _consent_boxes(page):  # "I agree with the terms" (Vatsal accepts them)
        _accept(tick)
    button = _button(page, _NEXT_RE)
    if button is None or not _click(page, button):
        return _hand_over(page, ctx, "Oracle's Next button stays disabled (a box to tick first?). Go on in the "
                                     "window.") != "timeout"
    out.account = "email entered"
    if _moved(page, before) == "same":
        errors = common.visible_errors(page.main_frame)
        why = f"Oracle didn't take the email ({'; '.join(errors[:3])})" if errors else "the page didn't go on"
        return _hand_over(page, ctx, f"{why}. Finish this step in the window; helper carries on from the next "
                                     "page.") != "timeout"
    return True


_SHOWN_LABEL_JS = "i => { const l = i.labels && i.labels[0]; return l && l.getClientRects().length ? l.innerText : ''; }"


def _consent_boxes(page: Any) -> list[Any]:
    """The terms boxes on the email step. Oracle often hides the real box behind a styled one (Fortinet, 2026-11-19:
    "You need to agree to the terms and conditions"), so a box counts when its label shows, even if the box doesn't."""
    loc = page.main_frame.locator("input[type=checkbox]")
    boxes = []
    for i in range(loc.count()):
        label = loc.nth(i).evaluate(_SHOWN_LABEL_JS) or ""
        if _CONSENT_RE.search(label) and not _HONEYPOT_RE.search(label):
            boxes.append(loc.nth(i))
    return boxes


def _accept(box: Any) -> bool:
    """Tick a terms box. Oracle's styled box comes first: a click on the label could land on the link to the privacy
    statement inside it, which opens a dialog instead."""
    styled = box.locator("xpath=ancestor::label[1]//*[contains(@class, 'checkbox__button')]")
    if not box.evaluate("i => i.checked") and styled.count() and styled.first.is_visible():
        try:
            styled.first.click()
        except Exception:
            pass
    return common.tick(box)


def _form_page(page: Any, ctx: FlowContext, out: FlowResult, refilled: set[str]) -> bool:
    before = signature(page)
    root = page.main_frame
    questions = [q for q in generic.discover(root)
                 if not _HONEYPOT_RE.search(f"{q.label} {q.field_name}") and not _IMPORT_RE.search(q.label)
                 and not _locked(root, q.field_name)]
    _name_phone_parts(questions)
    answers = ctx.plan(questions) if questions else []
    if answers:
        ctx.ask_claude(answers, lambda: _page_moved(page, before, ctx))
        out.results += generic.fill_form(root, answers, ctx.resume_pdf, country=None, city=None, pause=ctx.pause)
        out.answers += answers
    out.results += _upload_documents(page, ctx, answers)
    out.results += _fill_links(page, ctx)
    out.results += _fill_history(page, ctx)
    missing = [a for a in answers if a.required and (a.kind == "unknown" or a.value is None) and a.type != "file"]
    submit = _button(page, _SUBMIT_RE)
    nxt = _button(page, _NEXT_RE)
    if submit is not None and nxt is None:
        out.reached_review = True
        note = (" Answer these first: " + "; ".join(a.label for a in missing[:6]) + ".") if missing else ""
        # Oracle names what it refuses only after SUBMIT: helper looks for required fields still empty first (Honeywell,
        # Ralliant and Fortinet, 2026-11-20: "ready" while City, County, ZIP or a required question were empty)
        gaps = [g for g in _required_gaps(page) if g not in {a.label for a in missing}]
        if gaps:
            note += " These required fields look empty: " + "; ".join(gaps[:8]) + "."
        failed = [r.label for r in out.results if not r.ok and r.field.startswith("oracle:")]
        if failed:
            note += " helper couldn't finish: " + "; ".join(dict.fromkeys(failed[:6])) + "."
        ctx.say(f"helper stops here: review the application in the window and click '{submit['text']}' yourself "
                f"(helper never clicks Submit on Oracle yet).{note}")
        _AT_SUBMIT.add(id(page))
        result = _wait_at_submit(page, ctx)
        if result == "changed":
            out.reached_review = False
        return result == "changed"
    if missing:
        return _hand_over(page, ctx, "these required questions need your answer in the window: "
                                     + "; ".join(a.label for a in missing[:6]) + ". Click Next when done.") == "changed"
    if nxt is None:
        return _hand_over(page, ctx, "helper couldn't find a Next button on this page. Go on in the window.") \
            == "changed"
    if not _click(page, nxt):
        return _hand_over(page, ctx, "Oracle's Next button stays disabled (a box to tick first?). Tick it and click "
                                     "Next in the window.") == "changed"
    if _moved(page, before) == "same":
        errors = common.visible_errors(root)
        why = "; ".join(errors[:3]) if errors else "it stayed on the same page"
        if before not in refilled:  # fill it once more, then leave it to Vatsal
            refilled.add(before)
            ctx.say(f"Oracle didn't accept the page ({why}); filling it once more.")
            return True
        return _hand_over(page, ctx, f"Oracle still doesn't accept this page ({why}). Fix it in the window and click "
                                     "Next.") == "changed"
    return True


# "Import your profile from resume" makes Oracle parse the resume into the form, over what helper typed (Vertiv,
# 2026-11-19): the resume goes into Supporting Documents only
_IMPORT_RE = re.compile(r"(?i)\bimport (your )?profile\b|\bimport\b.{0,20}\bresume\b")


def _name_phone_parts(questions: list[Any]) -> None:
    """Oracle's phone row is a country-code dropdown and the number, both labeled "Phone Number" (Vertiv,
    2026-11-19): the number went into both. The first of the two, or the one with choices, is the country code."""
    phones = [q for q in questions if re.fullmatch(r"(?i)\s*phone( number)?\s*\*?\s*", q.label or "")]
    if len(phones) == 2:
        code = next((q for q in phones if q.options or q.type in ("select", "combobox")), phones[0])
        code.label = "Phone Country Code"


_UPLOAD_RE = {"resume": re.compile(r"(?i)\b(upload|add|attach)\b.{0,20}\b(resume|cv)\b"),
              "cover_letter": re.compile(r"(?i)\b(upload|add|attach)\b.{0,20}\bcover letter\b")}


def _in_import_block(page: Any, button: dict[str, Any]) -> bool:
    """A button in "Import your profile" (Oracle parses the resume into the profile, over what helper typed) isn't
    the Supporting Documents upload."""
    try:
        around = page.main_frame.locator(f"[data-helper-button='{button['i']}']").first.evaluate(
            "e => { let n = e; for (let k = 0; k < 5 && n.parentElement; k++) n = n.parentElement;"
            " return (n.innerText || '').slice(0, 600); }")
    except Exception:
        return False
    return bool(re.search(r"(?i)\bimport\b", around or ""))


def _upload_documents(page: Any, ctx: FlowContext, answers: list[Any]) -> list[common.FieldResult]:
    """Oracle's Supporting Documents block uploads through a button that opens a file picker, with no file box on the
    page until then: the resume (and the cover letter next to it) go in through the picker."""
    done = {a.key for a in answers if a.type == "file" and a.kind == "file"}
    letter = ctx.resume_pdf.with_name(ctx.resume_pdf.name.replace("_Resume", "_CoverLetter"))
    results = []
    for key, path in (("resume", ctx.resume_pdf), ("cover_letter", letter)):
        button = _button(page, _UPLOAD_RE[key])
        if key in done or button is None or not path.is_file() or _in_import_block(page, button):
            continue
        try:
            with page.expect_file_chooser(timeout=8000) as chooser:
                page.main_frame.locator(f"[data-helper-button='{button['i']}']").first.click()
            chooser.value.set_files(str(path))
            page.wait_for_timeout(2500)  # Oracle uploads it right away
            results.append(common.FieldResult(f"oracle:{key}", button["text"], True, path.name))
        except Exception as exc:
            results.append(common.FieldResult(f"oracle:{key}", button["text"], False, None,
                                              f"{type(exc).__name__}: {str(exc).splitlines()[0][:100]}"))
    return results


_LINK_ADD_RE = re.compile(r"(?i)^\s*\+?\s*add another link\b")


def _fill_links(page: Any, ctx: FlowContext) -> list[common.FieldResult]:
    """Oracle's "Link 1" box (and "Add Another Link") takes LinkedIn, GitHub and the portfolio; no answer rule maps a
    box called "Link 1" (Vertiv, Ralliant, Fortinet, Honeywell, 2026-11-20: all left empty)."""
    results: list[common.FieldResult] = []
    urls = [u for u in ctx.websites if u]
    root = page.main_frame
    if not urls or not root.locator("input[name^='siteLink-']").count():
        return results
    for n, url in enumerate(urls, 1):
        box = root.locator(f"input[name='siteLink-{n}']")
        if not box.count():
            add = _button(page, _LINK_ADD_RE)
            if add is None or not _click(page, add):
                break
            page.wait_for_timeout(800)
            box = root.locator(f"input[name='siteLink-{n}']")
            if not box.count():
                break
        try:
            if (box.first.input_value() or "").strip():
                continue
            box.first.fill(url)
            box.first.dispatch_event("change")
            results.append(common.FieldResult(f"oracle:link-{n}", f"Link {n}", True, url))
        except Exception as exc:
            results.append(common.FieldResult(f"oracle:link-{n}", f"Link {n}", False, url, type(exc).__name__))
    return results


# Work and Education History (a timeline) or separate Experience and Education blocks: entries only through their Add
# buttons, which helper never clicked, so a required block stayed empty (Fortinet: "You must add at least one
# experience and one education"; Honeywell: "You need to add at least 1 Experience item"; Vertiv: an entry with no
# Degree; 2026-11-20). Each entry opens Oracle's inline form, saved with its Done button (.save-btn).
_ADD_RE = {"experience": re.compile(r"(?i)^\s*\+?\s*add (an? |another |more )?(work )?experiences?\s*$"),
           "education": re.compile(r"(?i)^\s*\+?\s*add (an? |another |more )?educations?\s*$")}
_SAVE_BUTTON = "button.save-btn"
_OPEN_FORM_JS = r"""() => {
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  document.querySelectorAll('[data-helper-entry-form]').forEach(e => e.removeAttribute('data-helper-entry-form'));
  const save = [...document.querySelectorAll('button.save-btn')].find(visible);
  if (!save) return null;
  const box = save.closest('.standard-apply-flow-timeline-form, .profile-item-container, .standard-apply-flow-profile-item,'
                           + ' minimal-dialog, .app-dialog') || save.parentElement.parentElement.parentElement;
  box.setAttribute('data-helper-entry-form', '1');
  return true;
}"""
_LISTED_JS = r"""(kind) => {
  const text = el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const out = [];
  document.querySelectorAll(`[class*="timeline-item--${kind}s__"]`).forEach(e => out.push(text(e)));
  const re = kind === 'experience' ? /^\s*\+?\s*add (an? |another |more )?(work )?experiences?\s*$/i
                                   : /^\s*\+?\s*add (an? |another |more )?educations?\s*$/i;
  for (const b of document.querySelectorAll('button')) {
    if (!re.test((b.innerText || '').trim())) continue;
    const box = b.closest('.profile-item-container');
    if (box) box.querySelectorAll('.apply-flow-profile-item-tile').forEach(t => out.push(text(t)));
  }
  return out.join(' | ');
}"""
_INVALID_TILES_JS = r"""() => {
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  const out = [];
  document.querySelectorAll('[class*="timeline-item--invalid"], .apply-flow-profile-item-tile--invalid').forEach((t, i) => {
    const edit = t.querySelector('.beautiful-timeline-item__edit-icon, .apply-flow-profile-item-tile__edit-item-icon,'
                                 + ' .apply-flow-profile-item-tile__view-icon') || t;
    if (!visible(t)) return;
    edit.setAttribute('data-helper-invalid-tile', String(i));
    const cls = t.className || '';
    const kind = /educations?__/.test(cls) ? 'education' : /experiences?__/.test(cls) ? 'experience' : '';
    out.push({i, kind, text: (t.innerText || '').replace(/\s+/g, ' ').trim().toLowerCase()});
  });
  return out;
}"""
_REQUIRED_GAPS_JS = r"""() => {
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  const out = [];
  for (const row of document.querySelectorAll('.input-row')) {
    if (!visible(row) || row.closest('[data-helper-entry-form]')) continue;
    const label = row.querySelector('.input-row__label--required');
    if (!label) continue;
    const locked = [...row.querySelectorAll('input, textarea')].some(i => (i.readOnly || i.disabled) && i.value);
    // a plain box with its text typed counts, though Oracle marks the row only once the box loses focus (the
    // e-signature's Full Name, the last box typed, "looked empty" on both JPMC runs, 2026-11-23 and 24); a search
    // box (role=combobox) still counts only once its choice shows
    const typed = [...row.querySelectorAll('input:not([role]):not([type=checkbox]):not([type=radio]), textarea')]
      .some(i => (i.value || '').trim() !== '');
    const filled = row.classList.contains('input-row--filled') || locked || typed
      || row.querySelector('.cx-select-pill-section--selected, [aria-checked=true], input:checked');
    if (!filled) out.push((label.innerText || '').replace(/\s+/g, ' ').replace(/\s*\*\s*$/, '').trim());
  }
  return out.filter(Boolean);
}"""


_LOCKED_JS = r"""b => { const i = b.matches('input, textarea') ? b : b.querySelector('input, textarea');
  return !!(i && (i.readOnly || i.disabled) && i.value); }"""


def _locked(root: Any, key: str) -> bool:
    """A box Oracle locked with its value in it: the email it verified with the code (Vertiv and Ralliant, 2026-11-20:
    filling it waited 30 seconds for a box that never takes typing, then "These required fields look empty")."""
    try:
        return bool(root.locator(f"[data-helper-key={json.dumps(key)}]").first.evaluate(_LOCKED_JS))
    except Exception:
        return False


def _required_gaps(page: Any) -> list[str]:
    """Required fields Oracle draws as empty (no 'input-row--filled'), by their labels."""
    try:  # the last box typed keeps the focus, and Oracle takes its value on blur
        page.main_frame.evaluate("() => document.activeElement && document.activeElement.blur && "
                                 "document.activeElement.blur()")
    except Exception:
        pass
    try:
        return list(dict.fromkeys(page.main_frame.evaluate(_REQUIRED_GAPS_JS)))
    except Exception:
        return []


def _month_year(my: Any, part: str) -> Optional[str]:
    if my is None:
        return None
    return calendar.month_name[my.month] if part == "month" else str(my.year)


def _place(location: Optional[str]) -> dict[str, str]:
    """'Seattle, WA' -> city, state, country; 'Mumbai, India' -> city, country."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    if not parts or re.search(r"(?i)\bremote\b", location or ""):
        return {}
    if len(parts) >= 2 and re.fullmatch(r"[A-Z]{2}", parts[-1]):
        return {"city": parts[0], "state": parts[-1], "country": "United States"}
    return {"city": parts[0], "country": parts[-1]} if len(parts) >= 2 else {}


def _entry_value(label: str, options: list[str], kind: str, entry: Any) -> Optional[str]:
    """The value an Oracle entry form's field takes from a resume job (WorkEntry) or a school (SchoolEntry)."""
    from .workday_page import degree_score

    t = " ".join(label.lower().split())
    part = "month" if re.search(r"\bmonth\b", t) else "year" if re.search(r"\byear\b", t) else None
    if part and re.search(r"\b(start|from|begin)", t):
        return _month_year(entry.start, part)
    if part and re.search(r"\b(end|to|graduat|completion|finish)", t):
        return None if kind == "experience" and entry.current else _month_year(entry.end, part)
    if kind == "experience":
        if re.search(r"\bcurrent", t):
            return "Yes" if entry.current else "No"
        if re.search(r"\b(employer|company|organi[sz]ation)\b", t) and not re.search(r"phone|url|address|city|state|"
                                                                                   r"country|type|email", t):
            return entry.company
        if re.search(r"\b(job )?title\b|\bposition\b", t):
            return entry.title
        place = _place(entry.location)
        for key in ("country", "state", "city"):
            if re.match(rf"^({key}|province)\b" if key == "state" else rf"^{key}\b", t):
                return place.get(key)
        return None
    if re.search(r"\b(degree|education level|qualification)\b", t):
        if options:
            best = max(options, key=lambda o: degree_score(entry.degree, o))
            return best if degree_score(entry.degree, best) >= 0.6 else None
        return entry.degree
    if re.search(r"\b(school|institution|university|college)\b", t):
        return entry.school
    if re.search(r"\b(major|field of study|area of study|discipline|concentration|specialization)\b", t):
        return entry.field_of_study
    return None


def _entries(ctx: FlowContext, kind: str) -> list[Any]:
    return list(ctx.work if kind == "experience" else ctx.schools)


def _entry_key(kind: str, entry: Any) -> str:
    return (entry.company if kind == "experience" else entry.school).lower()


def _fill_entry_form(page: Any, ctx: FlowContext, kind: str, entry: Any, only_empty: bool) -> common.FieldResult:
    """Fill the open entry form from one job or school and save it with Oracle's Done button."""
    name = f"{kind.title()}: {entry.title + ' at ' if kind == 'experience' else ''}" \
           f"{entry.company if kind == 'experience' else entry.school}"
    field = f"oracle:{kind}:{_entry_key(kind, entry)}"
    root = page.main_frame
    form = root.locator("[data-helper-entry-form]").first
    from ..answers import PlannedAnswer

    answers = []
    for q in generic.discover(root):
        block = form.locator(f"[data-helper-key={json.dumps(q.field_name)}]")
        if not block.count():
            continue
        if only_empty and generic._shown_value(block):
            continue
        value = _entry_value(q.label, q.options, kind, entry)
        if value is None:
            continue
        answers.append(PlannedAnswer(field=q.field_name, label=q.label, type=q.type, required=q.required,
                                     options=q.options, kind="auto", value=value, confidence=1.0, source=f"resume {kind}",
                                     searchable=q.searchable))
    results = generic.fill_form(root, answers, ctx.resume_pdf, country=None, city=None, pause=ctx.pause)
    bad = [r.label for r in results if not r.ok]
    save = root.locator(f"[data-helper-entry-form] {_SAVE_BUTTON}")
    if not save.count():
        save = root.locator(_SAVE_BUTTON)
    try:
        save.first.click()
    except Exception as exc:
        return common.FieldResult(field, name, False, None, f"couldn't click Done: {type(exc).__name__}")
    for _ in range(20):  # the form closes once Oracle takes the entry
        page.wait_for_timeout(500)
        if not root.locator(f"{_SAVE_BUTTON}:visible").count():
            return common.FieldResult(field, name, True, "added" if not only_empty else "completed")
    errors = common.visible_errors(form) if form.count() else []
    why = "; ".join(errors[:3]) or ("not filled: " + "; ".join(bad[:4]) if bad else "Oracle kept the form open")
    return common.FieldResult(field, name, False, None, why)


def _open_form(page: Any) -> bool:
    try:
        return bool(page.main_frame.evaluate(_OPEN_FORM_JS))
    except Exception:
        return False


def _fill_history(page: Any, ctx: FlowContext) -> list[common.FieldResult]:
    """Add each resume job and school the page doesn't list yet, and finish the entries Oracle marks invalid."""
    results: list[common.FieldResult] = []
    root = page.main_frame
    if not (_button(page, _ADD_RE["experience"]) or _button(page, _ADD_RE["education"])
            or root.locator("[class*='timeline-item--invalid'], .apply-flow-profile-item-tile--invalid").count()):
        return results
    # 1. entries Oracle already holds but marks invalid (Vertiv's school with no Degree): open, fill the gaps, save
    if _open_form(page):  # one already open
        kind = "education" if root.locator("[data-helper-entry-form]").first.inner_text().lower().count("degree") \
            else "experience"
        entries = _entries(ctx, kind)
        if entries:
            results.append(_fill_entry_form(page, ctx, kind, entries[0], only_empty=True))
    try:
        invalid = root.evaluate(_INVALID_TILES_JS)
    except Exception:
        invalid = []
    for tile in invalid:
        kind = tile["kind"] or "education"
        match = next((e for e in _entries(ctx, kind) if _entry_key(kind, e) in tile["text"]), None)
        if match is None:
            continue
        try:
            root.locator(f"[data-helper-invalid-tile='{tile['i']}']").first.click()
            page.wait_for_timeout(1200)
        except Exception:
            continue
        if _open_form(page):
            results.append(_fill_entry_form(page, ctx, kind, match, only_empty=True))
    # 2. the jobs and schools not listed yet
    for kind in ("experience", "education"):
        for entry in _entries(ctx, kind):
            try:
                listed = str(root.evaluate(_LISTED_JS, kind))
            except Exception:
                listed = ""
            if _entry_key(kind, entry) in listed:
                continue
            add = _button(page, _ADD_RE[kind])
            if add is None:
                break
            if not _click(page, add):
                results.append(common.FieldResult(f"oracle:{kind}", f"Add {kind.title()}", False, None,
                                                  "the Add button stayed disabled"))
                break
            page.wait_for_timeout(1200)
            if not _open_form(page):
                results.append(common.FieldResult(f"oracle:{kind}", f"Add {kind.title()}", False, None,
                                                  "no entry form opened"))
                break
            result = _fill_entry_form(page, ctx, kind, entry, only_empty=False)
            results.append(result)
            if not result.ok:
                return results  # the open form stays for Vatsal; adding more would pile on
            ctx.pause()
    return results


# --------------------------------------------------------------------------- after the flow


def is_confirmation(page: Any, form: Optional[Any] = None) -> bool:
    if id(page) in _SUBMITTED:  # helper saw Oracle take Vatsal's SUBMIT (_wait_at_submit)
        return True
    hosts = ("oraclecloud.com",) + ((urlparse(page.url).hostname or "",) if is_oracle_page(page.url) else ())
    if common.is_confirmation(page, form, hosts=hosts):  # Oracle on a company's own domain too (Dell, 2026-11-23)
        return True
    try:
        text = page.main_frame.locator("body").inner_text(timeout=2000)[:3000]
    except Exception:
        return False
    # Oracle's own wording once the application is in; the posting then says the candidate applied. Never "You're all
    # set": Oracle opens a returning candidate's form with "You're all set, Vatsal! Welcome Back ... Use My Profile"
    # (Ralliant, 2026-11-19), long before anything is sent.
    return bool(re.search(r"(?i)\byour (job )?application (was|has been) (submitted|sent)\b|\bthanks? for applying\b"
                          r"|\byou(['’]ve| have)? applied (for|to) this job\b", text))


def is_submit_request(url: str, method: str) -> bool:
    """Oracle saves the application as a draft along the way, so no request is taken for the submit: only its
    confirmation page counts."""
    return False


def find_form(page: Any, timeout_ms: int = 20000) -> Optional[Any]:
    return page.main_frame if is_oracle_page(page.url) else None
