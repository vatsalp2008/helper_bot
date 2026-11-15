"""Any other site (milestone 10): a best-effort adapter that always stops before Submit and never auto-submits.

1. It first looks for a known applicant tracking system behind a company's own careers page (an embedded or linked
   Greenhouse, Lever, Ashby or Workday posting) and hands the posting to that adapter.
2. Otherwise it reads the job description from the page and finds the application form, on the page or behind an
   Apply link. A page that asks you to sign in or create an account is left to Vatsal (rule 8).
3. It reads every question by its label, and fills what helper can answer. Vatsal reviews and submits; helper logs it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

from ..htmltext import html_to_text
from ..models import FormQuestion, Job
from ..tracker.db import now_iso
from . import common, dom
from .common import FieldResult

ANCHORS = ("form input[type=email]", "input[type=email]", "form input[type=file]", "form textarea", "input[type=tel]")
_KNOWN = re.compile(
    r"https?://(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/[^\s\"'<>]+"
    r"|https?://jobs(?:\.eu)?\.lever\.co/[\w.-]+/[0-9a-f-]{36}[^\s\"'<>]*"
    r"|https?://jobs\.ashbyhq\.com/[\w.%-]+/[0-9a-f-]{36}[^\s\"'<>]*"
    r"|https?://[\w-]+\.wd\d+\.myworkdayjobs\.com/[^\s\"'<>]*/job/[^\s\"'<>]+"
    r"|https?://wd\d+\.myworkdaysite\.com/[^\s\"'<>]*recruiting/[^\s\"'<>]+/job/[^\s\"'<>]+", re.I)
_APPLY_RE = re.compile(r"(?i)^\s*(apply|apply now|apply for this (job|position|role)|apply here|start (your )?application"
                       r"|i'?m interested)\s*$")
_WALL_RE = re.compile(r"(?i)\b(sign in|log in|login|create (an |your )?account|register)\b")

# One entry per question: {key, label, type, required, options}. Each question's wrapper gets data-helper-key, so
# dom.fill_dom_form-style filling finds it again.
DISCOVER_JS = r"""
() => {
  const text = el => el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  const skipType = t => ['hidden', 'submit', 'button', 'reset', 'image', 'search', 'password'].includes(t);
  const all = [...document.querySelectorAll('input, select, textarea')].filter(c => {
    const t = (c.type || '').toLowerCase();
    if (skipType(t) || c.closest('header, nav, footer, [role=search], [role=navigation]')) return false;
    return visible(c) || t === 'file' || t === 'radio' || t === 'checkbox';  // styled controls hide the real input
  });
  const optionLabel = c => text(c.labels && c.labels[0]) || c.getAttribute('aria-label') || c.value || '';
  const own = (c, members) => {
    const kind = (c.type || '').toLowerCase();
    if (c.labels && c.labels.length && (!['radio', 'checkbox'].includes(kind) || (kind === 'checkbox' && !members)))
      return text(c.labels[0]);  // a lone checkbox's own label is the question ("I agree to the privacy policy")
    const by = c.getAttribute('aria-labelledby');
    if (by) return by.split(/\s+/).map(id => text(document.getElementById(id))).join(' ');
    if (c.getAttribute('aria-label') && !members) return c.getAttribute('aria-label');
    return '';
  };
  const wrapper = members => {  // the widest ancestor holding these controls and no other question's
    let w = members[0].parentElement, best = w;
    for (let i = 0; i < 6 && w && w !== document.body; i++, w = w.parentElement) {
      const inside = [...w.querySelectorAll('input, select, textarea')].filter(x => !skipType((x.type || '').toLowerCase()));
      if (inside.some(x => !members.includes(x))) break;
      best = w;
    }
    return best;
  };
  const nearbyLabel = (box, members) => {
    const fs = members[0].closest('fieldset');
    if (fs && fs.querySelector('legend') && box.contains(fs)) return text(fs.querySelector('legend'));
    for (const el of box.querySelectorAll('legend, label, p, h3, h4, h5, span, div')) {
      if (el.querySelector('input, select, textarea') || members.some(m => m.labels && [...m.labels].includes(el))) continue;
      const t = text(el);
      if (t && t.length > 1) return t;
    }
    return '';
  };
  const out = [], done = new Set();
  all.forEach((c, n) => {
    if (done.has(c)) return;
    const t = (c.type || '').toLowerCase();
    let members = [c];
    if ((t === 'radio' || t === 'checkbox') && c.name) members = all.filter(x => x.name === c.name && x.type === c.type);
    members.forEach(m => done.add(m));
    const box = wrapper(members);
    let label = own(c, members.length > 1 || t === 'radio' ? members : null) || nearbyLabel(box, members)
                || c.placeholder || c.name || '';
    label = label.replace(/\s+/g, ' ').trim();
    if (!label) return;
    let type = 'text', options = [];
    if (t === 'file') type = 'file';
    else if (c.tagName === 'SELECT') { type = 'select'; options = [...c.options].map(o => text(o)).filter(o => o && !/^(select|choose|--)/i.test(o)); }
    else if (c.tagName === 'TEXTAREA') type = 'textarea';
    else if (t === 'radio') { type = 'radio'; options = members.map(optionLabel); }
    else if (t === 'checkbox') {
      if (members.length > 1) { type = 'multi_select'; options = members.map(optionLabel); }
      else { type = 'boolean'; options = [optionLabel(c) || label]; }
    } else if (t === 'date') type = 'date';
    const required = members.some(m => m.required || m.getAttribute('aria-required') === 'true') || /\*|\(required\)/i.test(label);
    const key = 'generic:' + (c.name || c.id || 'field-' + n);
    if (out.some(o => o.key === key)) return;
    box.setAttribute('data-helper-key', key);
    out.push({key, label: label.replace(/^\s*\*+\s*/, '').replace(/\s*\*+\s*$/, '').replace(/\s*\(required\)\s*$/i, '').trim(),
              type, required, options});
  });
  return out;
}
"""

SUBMIT_WATCH_JS = r"""
() => {
  if (window.__helperSubmitWatch) return;
  window.__helperSubmitWatch = true;
  document.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('button, input[type=submit], [role=button]');
    if (b && /submit|send application|apply/i.test(b.innerText || b.value || '')) window.__helperSubmitClicked = true;
  }, true);
}
"""


@dataclass
class Scan:
    title: str = ""
    company: str = ""
    description: str = ""
    apply_url: Optional[str] = None
    questions: list[FormQuestion] = field(default_factory=list)
    delegate: Optional[str] = None  # a known applicant tracking system's posting link
    login_wall: bool = False


def parse_ref(url: str) -> Optional[str]:
    return url if urlparse(url).scheme in ("http", "https", "file") else None


def known_link(texts: list[str]) -> Optional[str]:
    """The first Greenhouse, Lever, Ashby or Workday posting link among these (iframe sources come first)."""
    for t in texts:
        m = _KNOWN.search(t or "")
        if m:
            return m.group(0).rstrip(").,;")
    return None


def discover(root: Any) -> list[FormQuestion]:
    found = root.evaluate(DISCOVER_JS)
    questions = []
    for f in found:
        section = "eeoc" if re.search(r"(?i)^(gender|race|ethnicity|veteran|disabilit|hispanic|sex)\b", f["label"]) else "questions"
        questions.append(FormQuestion(label=f["label"], field_name=f["key"], type=f["type"], required=f["required"],
                                      options=f["options"], section=section))
    return questions


def read_posting(page: Any) -> tuple[str, str, str]:
    """(role title, company, description) from the page itself."""
    data = page.evaluate(r"""() => {
      const t = el => el ? (el.innerText || '').trim() : '';
      const meta = n => (document.querySelector(`meta[property="${n}"], meta[name="${n}"]`) || {}).content || '';
      const main = document.querySelector('main, article, [role=main], #content, .job-description, [class*=description]');
      const blocks = [...document.querySelectorAll('div, section')].map(d => [t(d).length, d]).sort((a, b) => b[0] - a[0]);
      return {h1: t(document.querySelector('h1')), title: document.title, site: meta('og:site_name'),
              ogtitle: meta('og:title'), text: t(main) || (blocks[0] ? t(blocks[0][1]) : t(document.body))};
    }""")
    title = data["h1"] or data["ogtitle"] or data["title"]
    host = (urlparse(page.url).hostname or "").removeprefix("www.").removeprefix("careers.").removeprefix("jobs.")
    company = data["site"] or (host.split(".")[0].replace("-", " ").title() if host else "")
    return title.strip(), company.strip(), data["text"].strip()


_FORM_SIZE_JS = r"""
() => {
  const visible = el => !!(el.offsetParent || el.getClientRects().length);
  const fields = [...document.querySelectorAll('input, select, textarea')].filter(c => {
    const t = (c.type || '').toLowerCase();
    return !['hidden', 'submit', 'button', 'reset', 'image', 'search', 'password'].includes(t)
      && !c.closest('header, nav, footer, [role=search], [role=navigation]') && (visible(c) || t === 'file');
  });
  const key = fields.some(c => c.type === 'email' || c.type === 'file' || /mail|resume|cv/i.test(c.name || c.id || ''));
  return {count: fields.length, key};
}
"""


def _has_form(root: Any) -> bool:
    """An application form: several real questions, one of them an email or resume field (not a newsletter box)."""
    try:
        size = root.evaluate(_FORM_SIZE_JS)
    except Exception:
        return False
    return size["count"] >= 3 and size["key"]


_UNREADABLE_RE = re.compile(r"(?i)access denied|page not found|\b404\b|no longer (available|accepting|open)|"
                            r"(job|position|posting) (has been |is )?(closed|filled|expired|removed)|"
                            r"(enable|requires) javascript|are you a (human|robot)|verify you are human")


def readable(title: str, description: str) -> bool:
    """A real job description, not a block page, a closed posting or an empty shell."""
    return len(description) >= 500 and not _UNREADABLE_RE.search(f"{title} {description[:600]}")


def _frames(page: Any) -> list[Any]:
    return [page.main_frame] + [f for f in page.frames if f is not page.main_frame]


def _sources(page: Any) -> list[str]:
    """Where a known applicant tracking system could show up: frames first, then links, then the page itself."""
    frames = [f.url for f in page.frames if f is not page.main_frame]
    try:
        links = page.evaluate("() => [...document.querySelectorAll('a[href], iframe[src], form[action]')]"
                              ".map(e => e.href || e.src || e.action)")
    except Exception:
        links = []
    return frames + links + [page.url]


def _form_frame(page: Any) -> Optional[Any]:
    return next((f for f in _frames(page) if _has_form(f)), None)


def _login_wall(page: Any) -> bool:
    try:
        if page.locator("input[type=password]:visible").count():
            return True
        body = page.locator("body").inner_text(timeout=2000)
    except Exception:
        return False
    return bool(_WALL_RE.search(body[:3000])) and _form_frame(page) is None


def scan_posting(url: str, timeout_ms: int = 45000) -> Scan:
    """Read a posting in a hidden browser (no profile, nothing typed): a known system behind it, else the job
    description, the application form and its questions."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            _settle(page)
            return scan_page(page)
        finally:
            browser.close()


def scan_page(page: Any) -> Scan:
    delegate = known_link(_sources(page))
    if delegate:
        return Scan(delegate=delegate)
    title, company, description = read_posting(page)
    scan = Scan(title=title, company=company, description=description)
    form = _form_frame(page)
    if form is None and _click_apply(page):
        delegate = known_link(_sources(page))
        if delegate:
            return Scan(delegate=delegate, title=title, company=company, description=description)
        form = _form_frame(page)
    if form is None:
        scan.login_wall = _login_wall(page)
        return scan
    scan.apply_url = page.url
    scan.questions = discover(form)
    return scan


def _settle(page: Any) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def _click_apply(page: Any) -> bool:
    """Follow the page's Apply link or button (reading only: nothing is typed)."""
    for el in page.locator("a, button, [role=button]").all()[:400]:
        try:
            if not el.is_visible() or not _APPLY_RE.match(el.inner_text(timeout=500) or ""):
                continue
            href = el.get_attribute("href")
            if href and not href.startswith(("#", "javascript")):
                page.goto(urljoin(page.url, href), wait_until="domcontentloaded")
            else:
                el.click()
            _settle(page)
            return True
        except Exception:
            continue
    return False


def fetch_job(url: str, session: Optional[Any] = None, scan: Callable[[str], Scan] = scan_posting) -> Job:
    from ..ats import detect_ats
    from ..errors import HelperError

    result = scan(url)
    if result.delegate:
        from . import adapter_for

        adapter = adapter_for(result.delegate)
        if adapter.__name__ != __name__:
            return adapter.fetch_job(result.delegate)
    if not readable(result.title, result.description):
        raise HelperError(f"couldn't read a job description at {url} (the site blocked helper's browser, the posting "
                          "is closed, or the page was empty). Save the description to a file and use `--jd-file`.")
    ats = detect_ats(url)
    return Job(url=url, ats=ats if ats != "unknown" else "generic", company=result.company or "Unknown",
               role_title=result.title or "Unknown role", description_text=html_to_text(result.description)
               if "<" in result.description else result.description, questions=result.questions,
               source="page", fetched_at=now_iso(), apply_url=result.apply_url or url)


def find_form(page: Any, timeout_ms: int = 15000) -> Optional[Any]:
    form = common.find_form(page, ANCHORS, timeout_ms=timeout_ms)
    if form is None and _click_apply(page):
        form = common.find_form(page, ANCHORS, timeout_ms=timeout_ms)
    return form


def fill_form(root: Any, answers: list[Any], resume_pdf: Path, *, country: Optional[str], city: Optional[str],
              pause: Callable[[], None]) -> list[FieldResult]:
    """Fill every planned answer the page has (found again by its label). Never touches Submit."""
    root.evaluate(DISCOVER_JS)
    results: list[FieldResult] = []
    for a in answers:
        if a.kind in ("skip", "unknown") or (a.value is None and a.type != "file"):
            continue
        block = root.locator(f"[data-helper-key={json.dumps(a.field)}]")
        if not block.count():
            results.append(FieldResult(a.field, a.label, not a.required, None,
                                       "not shown on this form" if not a.required else "field not found on the page"))
            continue
        try:
            ok, shown, detail = dom._fill_one(root, block, a, resume_pdf, city, _iso_date)
        except Exception as exc:
            ok, shown, detail = False, a.value, f"{type(exc).__name__}: {str(exc)[:120]}"
        results.append(FieldResult(a.field, a.label, ok, shown, detail))
        pause()
    try:
        root.evaluate(SUBMIT_WATCH_JS)
    except Exception:
        pass
    return results


def _iso_date(value: str) -> str:
    """A date box (type=date) takes YYYY-MM-DD."""
    from ..dates import parse_month_year

    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", value)
    if m:
        return f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}"
    my = parse_month_year(value.replace(",", "").replace(" 1 ", " "))
    return f"{my.year:04d}-{my.month:02d}-01" if my else value


def no_form_reason(page: Any) -> str:
    """Why `helper fill` found nothing to fill on this page."""
    if _login_wall(page):
        return ("This site asks you to sign in or create an account before its form, and helper never creates "
                "accounts there (rule 8). Sign in and apply in the window yourself; helper watches for the "
                "confirmation and logs it.")
    return ("helper couldn't find an application form on this page, so it filled nothing. Fill it in the window "
            "yourself; helper watches for the confirmation and logs it.")


def is_confirmation(page: Any, form: Optional[Any] = None) -> bool:
    return common.is_confirmation(page, form)


def is_submit_request(url: str, method: str) -> bool:
    return method == "POST" and bool(re.search(r"(?i)/(apply|application|applications|submit|candidates?)\b",
                                               urlparse(url).path))


def submit_clicked(page: Any) -> bool:
    for frame in _frames(page):
        try:
            if frame.evaluate("() => window.__helperSubmitClicked === true"):
                return True
        except Exception:
            continue
    return False
