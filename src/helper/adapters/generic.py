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
  // ARIA widgets with no <input> of their own are questions too: Rippling's dropdowns are role=combobox divs, and its
  // rating questions are role=radio divs in a role=radiogroup (apexanalytix, Raptor Maps, 2026-11-18)
  const CONTROLS = 'input, select, textarea, [role=combobox], [role=radiogroup]';
  const isField = c => ['INPUT', 'SELECT', 'TEXTAREA'].includes(c.tagName);
  const all = [...document.querySelectorAll(CONTROLS)].filter(c => {
    if (c.closest('header, nav, footer, [role=search], [role=navigation]')) return false;
    if (!isField(c)) {  // a radio group with real inputs, or a combobox wrapping its input, is read by those inputs
      if (c.querySelector('input, select, textarea')) return false;
      if (c.getAttribute('role') === 'radiogroup' && !c.querySelector('[role=radio]')) return false;
      return visible(c);
    }
    const t = (c.type || '').toLowerCase();
    if (skipType(t)) return false;
    return visible(c) || t === 'file' || t === 'radio' || t === 'checkbox';  // styled controls hide the real input
  });
  const optionLabel = c => text(c.labels && c.labels[0]) || c.getAttribute('aria-label') || c.value || '';
  // a widget's own name says nothing about the question: BambooHR labels both upload boxes "file-input", and the
  // cover letter box got the resume (All Options, 2026-11-18); the words next to it name it
  const GENERIC = /^(file[-_ ]?input|file|upload|search|textbox|input|select|combobox|choose)\s*(\.\.\.|…)?$/i;
  const DROP_ZONE = /^(drop|drag|select|choose|browse|upload|attach)\b/i;  // "Drop or select (.doc / .docx / .pdf)"
  // a required mark the stylesheet draws (::after { content: "*" }) isn't in the text: Rippling marks its custom
  // questions that way, and helper took them for optional ones (apexanalytix, 2026-11-18)
  const starred = el => !!el && el.nodeType === 1 && [el, ...el.querySelectorAll('*')].some(x =>
    ['::after', '::before'].some(p => /^["']\s*\*\s*["']$/.test(getComputedStyle(x, p).content || '')));
  let labelEl = null;  // where the label was read, for its drawn mark
  const own = (c, members) => {
    const kind = (c.type || '').toLowerCase();
    if (c.labels && c.labels.length && (!['radio', 'checkbox'].includes(kind) || (kind === 'checkbox' && !members))) {
      const words = text(c.labels[0]);  // a lone checkbox's own label is the question ("I agree to the privacy policy")
      if (!(kind === 'file' && DROP_ZONE.test(words))) { labelEl = c.labels[0]; return words; }  // Rippling: drop zone
    }
    const by = c.getAttribute('aria-labelledby');
    if (by) {
      const ids = by.split(/\s+/).filter(id => id !== c.id);
      const named = ids.map(id => text(document.getElementById(id))).join(' ').trim();
      if (named) { labelEl = document.getElementById(ids[ids.length - 1]); return named; }
    }
    const aria = c.getAttribute('aria-label') || '';
    if (aria && !members && !GENERIC.test(aria.trim())) return aria;
    return '';
  };
  const wrapper = members => {  // the widest ancestor holding these controls and no other question's
    // a radio group's own container first: Rippling hides each input four levels inside its option (2026-11-18)
    const group = members[0].type === 'radio' || members[0].type === 'checkbox'
      ? members[0].closest('[role=radiogroup], [role=group], fieldset') : null;
    const holds = group && members.every(m => group.contains(m));
    let w = holds ? group.parentElement : members[0].parentElement, best = holds ? group : null;
    for (let i = 0; i < 6 && w && w !== document.body; i++, w = w.parentElement) {
      const inside = [...w.querySelectorAll(CONTROLS)].filter(x => !skipType((x.type || '').toLowerCase()));
      if (inside.some(x => !members.includes(x) && !members.some(m => x.contains(m) || m.contains(x)))) break;
      best = w;
    }
    if (best) return best;
    // no wrapper of its own (fields side by side in one parent): a single field is tagged itself, so tagging the
    // next field doesn't overwrite it
    const kind = (members[0].type || '').toLowerCase();
    return members.length === 1 && !['radio', 'checkbox'].includes(kind) ? members[0] : members[0].parentElement;
  };
  const nearbyLabel = (box, members) => {
    const fs = members[0].closest('fieldset');
    if (fs && fs.querySelector('legend') && box.contains(fs)) {
      labelEl = fs.querySelector('legend');
      return text(labelEl);
    }
    for (const el of box.querySelectorAll('legend, label, p, h3, h4, h5, span, div')) {
      if (el.querySelector('input, select, textarea') || members.some(m => m.labels && [...m.labels].includes(el))) continue;
      // an option's words or a dropdown's shown value aren't the question ("Full Stack", "Select...": Rippling)
      if (members.some(m => el.contains(m)) || el.closest('[role=radio], [role=checkbox], [role=option], [role=listbox], '
                                                        + '[role=combobox]')) continue;
      const t = text(el);
      if (t && t.length > 1) { labelEl = el; return t; }
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
    const role = c.getAttribute('role');
    labelEl = null;
    const named = own(c, members.length > 1 || t === 'radio' ? members : null) || nearbyLabel(box, members);
    // a dropdown's own search box with no name of its own is part of the field beside it (Rippling's phone country
    // code, "Search", was asked of Vatsal as a required question; Raptor Maps, 2026-11-18)
    if (!named && role === 'combobox' && GENERIC.test((c.placeholder || c.getAttribute('aria-label') || '').trim())) return;
    let label = named || c.placeholder || c.name || '';
    label = label.replace(/\s+/g, ' ').trim();
    if (!label) return;
    let type = 'text', options = [], listbox = false;
    if (t === 'file') type = 'file';
    else if (c.tagName === 'SELECT') { type = 'select'; options = [...c.options].map(o => text(o)).filter(o => o && !/^(select|choose|--)/i.test(o)); }
    else if (c.tagName === 'TEXTAREA') type = 'textarea';
    else if (role === 'radiogroup') {
      type = 'radio';
      options = [...c.querySelectorAll('[role=radio]')].map(r => (r.getAttribute('aria-label') || text(r)).trim());
    }
    else if (role === 'combobox' && !isField(c)) { type = 'select'; listbox = true; }  // its choices open on a click
    else if (t === 'radio') { type = 'radio'; options = members.map(optionLabel); }
    else if (t === 'checkbox') {
      if (members.length > 1) { type = 'multi_select'; options = members.map(optionLabel); }
      else { type = 'boolean'; options = [optionLabel(c) || label]; }
    } else if (t === 'date') type = 'date';
    // a search box that opens a list: a dropdown when its choices show without typing (discover() opens it to see),
    // a place search otherwise
    else if (role === 'combobox' && c.getAttribute('aria-haspopup') === 'listbox') listbox = true;
    const required = members.some(m => m.required || m.getAttribute('aria-required') === 'true')
      || /\*|\(required\)/i.test(label) || starred(labelEl) || (box !== c && starred(box));
    const key = 'generic:' + (c.name || c.id || 'field-' + n);
    if (out.some(o => o.key === key)) return;
    box.setAttribute('data-helper-key', key);
    out.push({key, label: label.replace(/^\s*\*+\s*/, '').replace(/\s*\*+\s*$/, '').replace(/\s*\(required\)\s*$/i, '').trim(),
              type, required, options, listbox});
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
    // the whole label, so "Apply With LinkedIn", "Apply with Indeed" and "Apply Now" aren't Submit (Fortinet, 2026-11-16)
    const label = (b && (b.innerText || b.value || '')).trim();
    if (b && /^(submit( (my |your )?application)?|send( (my )?application)?|complete application|finish|apply)$/i.test(label))
      window.__helperSubmitClicked = true;
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
    location: Optional[str] = None
    robot_check: Optional[str] = None  # the site the Apply link led to, when it showed a robot check


_LD_JS = "() => [...document.querySelectorAll('script[type=\"application/ld+json\"]')].map(s => s.textContent)"
_LD_RE = re.compile(r"(?is)<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>")
_LD_TEXT_KEYS = ("description", "responsibilities", "qualifications", "skills", "experienceRequirements",
                 "educationRequirements")


def _ld_location(place: Any) -> Optional[str]:
    place = place[0] if isinstance(place, list) and place else place
    address = place.get("address") if isinstance(place, dict) else None
    if not isinstance(address, dict):
        return None
    parts = [address.get("addressLocality"), address.get("addressRegion")]
    return ", ".join(p for p in parts if isinstance(p, str) and p.strip() and p.strip().upper() != "UNAVAILABLE") or None


def job_posting_ld(blocks: list[str]) -> Optional[Scan]:
    """The schema.org JobPosting many career sites embed for search engines (title, employer, location, the full
    description). Its employer is the hiring company even on a page a job board shows."""
    for raw in blocks:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        items = data if isinstance(data, list) else (data.get("@graph") or [data]) if isinstance(data, dict) else []
        for item in items:
            kind = item.get("@type") if isinstance(item, dict) else None
            if "JobPosting" not in (kind if isinstance(kind, list) else [kind]):
                continue
            parts = [item.get(k) for k in _LD_TEXT_KEYS]
            text = "\n\n".join(html_to_text(p) for p in parts
                               if isinstance(p, str) and p.strip() and p.strip().upper() != "UNAVAILABLE")
            org = item.get("hiringOrganization")
            company = org.get("name") if isinstance(org, dict) else org if isinstance(org, str) else ""
            return Scan(title=str(item.get("title") or "").strip(), company=str(company or "").strip(),
                        description=text.strip(), location=_ld_location(item.get("jobLocation")))
    return None


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
        if f.get("listbox") and not f["options"]:
            # an ARIA dropdown keeps its choices in a list that opens on a click (Rippling, 2026-11-18): open it to
            # read them, so answers pick one; a place search shows nothing until typed into and stays a text box
            choices = dom.listbox_options(root, f["key"])
            if choices:
                f["type"], f["options"] = "select", choices
        section = "eeoc" if re.search(r"(?i)^(gender|race|ethnicity|veteran|disabilit|hispanic|sex)\b", f["label"]) else "questions"
        questions.append(FormQuestion(label=f["label"], field_name=f["key"], type=f["type"], required=f["required"],
                                      options=f["options"], section=section))
    return questions


_GENERIC_SITE_NAME = re.compile(r"(?i)^\s*(candidate experience( site)?|careers?( site| page| portal)?|jobs?|job search|"
                                r"job board|career opportunities|recruiting|talent)\s*$")
_COPYRIGHT_RE = re.compile(r"(?:©|\(c\)|Copyright)\s*(?:©\s*)?(?:\d{4}(?:\s*[-–]\s*\d{4})?\s*)?"
                           r"([A-Z][A-Za-z0-9&'. -]{1,60}?)(?:,?\s+(?:Inc|LLC|Ltd|Corp|Corporation|Co)\b\.?|\.|\s+All rights)",
                           re.IGNORECASE)


def rippling_employer(url: str) -> str:
    """ats.rippling.com/<employer-slug>/jobs/<id>: the employer, from its slug ("raptor-maps-inc" is Raptor Maps)."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts or parts[0] in ("jobs", "careers"):
        return ""
    words = [w for w in parts[0].split("-") if w and w.lower() not in ("inc", "llc", "corp", "co", "careers", "jobs")]
    if len(words) == 1:
        return words[0]  # a one-word brand keeps its own spelling ("apexanalytix")
    return " ".join(w.title() for w in words)


def copyright_owner(text: str) -> str:
    """'Copyright © 2023 Fortinet, Inc. All Rights Reserved.' -> 'Fortinet'."""
    m = _COPYRIGHT_RE.search(text or "")
    name = m.group(1).strip(" ,.") if m else ""
    return "" if not name or name.lower() in ("all", "the") or _GENERIC_SITE_NAME.match(name) else name


def read_posting(page: Any) -> tuple[str, str, str]:
    """(role title, company, description) from the page itself."""
    data = page.evaluate(r"""() => {
      const t = el => el ? (el.innerText || '').trim() : '';
      const meta = n => (document.querySelector(`meta[property="${n}"], meta[name="${n}"]`) || {}).content || '';
      const main = document.querySelector('main, article, [role=main], #content, .job-description, [class*=description]');
      const blocks = [...document.querySelectorAll('div, section')].map(d => [t(d).length, d]).sort((a, b) => b[0] - a[0]);
      const foot = t(document.querySelector('footer')) || (t(document.body) || '').slice(-3000);
      return {h1: t(document.querySelector('h1')), title: document.title, site: meta('og:site_name'),
              ogtitle: meta('og:title'), text: t(main) || (blocks[0] ? t(blocks[0][1]) : t(document.body)),
              footer: foot};
    }""")
    title = data["h1"] or data["ogtitle"] or data["title"]
    host = (urlparse(page.url).hostname or "").removeprefix("www.").removeprefix("careers.").removeprefix("jobs.")
    company = data["site"] or (host.split(".")[0].replace("-", " ").title() if host else "")
    if _GENERIC_SITE_NAME.match(company) or "oraclecloud" in host:
        # Oracle's "Candidate Experience site" is no employer (Fortinet, 2026-11-16): use the copyright line
        owner = copyright_owner(data.get("footer") or "")
        company = owner or ("" if _GENERIC_SITE_NAME.match(company) else company)
    if host.endswith("rippling.com"):  # "Rippling Recruiting" is the hiring site (Raptor Maps, apexanalytix, 2026-11-18)
        company = rippling_employer(page.url) or company
    text = data["text"].strip()
    try:
        ld = job_posting_ld(page.evaluate(_LD_JS))
    except Exception:
        ld = None
    if ld:  # the page's own JobPosting data names the employer, and its description has no menus or ads
        ld_company = "" if _GENERIC_SITE_NAME.match(ld.company or "") else ld.company  # Oracle names itself
        return ld.title or title.strip(), ld_company or company.strip(), ld.description if readable(
            ld.title, ld.description) else text
    return title.strip(), company.strip(), text


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


_ROBOT = (r"are you a (human|robot)|verify you are (a )?human|(you are|you're|i'?m) not a robot|"
          r"checking (if the site connection is secure|your browser)|just a moment\.\.\.|"
          r"press (&|and) hold|security check to (continue|access)")
_ROBOT_RE = re.compile(_ROBOT, re.I)
_UNREADABLE_RE = re.compile(r"access denied|403 forbidden|page not found|\b404\b|no longer (available|accepting|open|supported)|"
                            r"browser (is )?not supported|unsupported browser|"
                            r"(job|position|posting) (has been |is )?(closed|filled|expired|removed)|"
                            r"(enable|requires) javascript|" + _ROBOT, re.I)


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
    scan = Scan(title=title, company=company, description=description, location=_page_location(page))
    form = _form_frame(page)
    if form is None:
        opened = _click_apply(page, new_tab=True)
        if opened is not None:
            page = opened
            delegate = known_link(_sources(page))
            if delegate:
                return Scan(delegate=delegate, title=title, company=company, description=description)
            if robot_check(page):
                scan.robot_check = urlparse(page.url).hostname or page.url
                return scan
            if _applies_elsewhere(page.url):
                scan.apply_url = page.url
                return scan
            form = _form_frame(page)
    if form is None:
        scan.login_wall = _login_wall(page)
        return scan
    scan.apply_url = page.url
    scan.questions = discover(form)
    return scan


def _page_location(page: Any) -> Optional[str]:
    try:
        ld = job_posting_ld(page.evaluate(_LD_JS))
    except Exception:
        return None
    return ld.location if ld else None


def robot_check(page: Any) -> bool:
    """A "check you are not a robot" page (Cloudflare and the like). helper never gets past one (rule 6)."""
    try:
        return bool(_ROBOT_RE.search(page.locator("body").inner_text(timeout=3000)[:2000]))
    except Exception:
        return False


def _manual_site(url: str) -> Optional[str]:
    """Taleo, SmartRecruiters or SuccessFactors: sites helper never fills (rule 8)."""
    from ..ats import detect_ats
    from . import MANUAL

    return MANUAL.get(detect_ats(url))


def _applies_elsewhere(url: str) -> bool:
    """An Apply button that leads to a site with accounts: iCIMS (helper fills it) or one helper never fills."""
    from . import icims

    return bool(_manual_site(url) or icims.parse_ref(url))


def _settle(page: Any) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(800)


def _click_apply(page: Any, new_tab: bool = False) -> Optional[Any]:
    """Follow the page's Apply link or button (reading only: nothing is typed) and return the page to read next.
    With new_tab, a link that opens a new tab is clicked the way a person would (job boards often only allow that)
    and the new tab is returned."""
    for el in page.locator("a, button, [role=button]").all()[:400]:
        try:
            if not el.is_visible() or not _APPLY_RE.match(el.inner_text(timeout=500) or ""):
                continue
            href = el.get_attribute("href")
            if new_tab and href and (el.get_attribute("target") or "").lower() == "_blank":
                tab = _open_in_tab(page, el)
                if tab is not None:
                    return tab
            if href and not href.startswith(("#", "javascript")):
                page.goto(urljoin(page.url, href), wait_until="domcontentloaded")
            else:
                el.click()
            _settle(page)
            return page
        except Exception:
            continue
    return None


def _open_in_tab(page: Any, el: Any) -> Optional[Any]:
    try:
        with page.context.expect_page(timeout=10000) as opened:
            el.click()
        tab = opened.value
        tab.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        return None
    _settle(tab)
    last = None
    for _ in range(6):  # job boards pass through redirect pages: wait until the address stops changing
        if tab.url == last:
            break
        last = tab.url
        tab.wait_for_timeout(1500)
    return tab


_APPLY_URL_RE = re.compile(r"\"apply_?url\"\s*:\s*\"([^\"]+)\"", re.I)
_ANCHOR_RE = re.compile(r"(?is)<a\b([^>]*)>(.*?)</a>")
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)


def _apply_links(html: str, base: str) -> list[str]:
    """Where a page's Apply button goes, from its HTML: apply_url values in its data, and links that say Apply."""
    links = [u.replace("\\/", "/") for u in _APPLY_URL_RE.findall(html)]
    for attrs, inner in _ANCHOR_RE.findall(html):
        href = _HREF_RE.search(attrs)
        if href and re.search(r"(?i)\bapply\b", f"{re.sub(r'<[^>]+>', ' ', inner)} {attrs}"):
            links.append(urljoin(base, href.group(1)))
    return links


def read_html(url: str, session: Optional[Any] = None) -> Optional[Scan]:
    """The posting from the page's HTML alone, for sites that turn helper's hidden browser away (Jibe answers it with
    403 Forbidden): its schema.org JobPosting data, and where its Apply button goes."""
    import requests

    session = session or requests.Session()
    try:
        r = session.get(url, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    html = r.text or ""
    found = job_posting_ld(_LD_RE.findall(html))
    if found is None:
        return None
    links = _apply_links(html, url)
    found.delegate = known_link(links)
    found.apply_url = next((link for link in links if _applies_elsewhere(link)), None)
    return found


def _handed_over(link: str) -> Optional[Job]:
    from . import adapter_for

    adapter = adapter_for(link)
    return adapter.fetch_job(link) if adapter.__name__ != __name__ else None


def fetch_job(url: str, session: Optional[Any] = None, scan: Callable[[str], Scan] = scan_posting) -> Job:
    from ..ats import detect_ats
    from ..errors import HelperError, NeedsUser

    result = scan(url)
    if result.robot_check:
        raise NeedsUser(f"This posting's Apply link leads to {result.robot_check}, which shows a \"check you are not a "
                        "robot\" page, and helper never gets past those (rule 6). Open the link in your browser, click "
                        "Apply, and send me the employer's own posting link. To build the resume from this page "
                        "anyway, save its description and run `helper resume --jd-file <file> --company <employer> "
                        "--role <role>`.")
    if result.delegate and (job := _handed_over(result.delegate)) is not None:
        return job
    if not readable(result.title, result.description):
        fallback = read_html(url, session)
        if fallback is not None and fallback.delegate and (job := _handed_over(fallback.delegate)) is not None:
            return job
        result = fallback or result
    if not readable(result.title, result.description):
        raise HelperError(f"couldn't read a job description at {url} (the site blocked helper's browser, the posting "
                          "is closed, or the page was empty). Save the description to a file and use `--jd-file`.")
    ats = detect_ats(url)
    return Job(url=url, ats=ats if ats != "unknown" else "generic", company=result.company or "Unknown",
               role_title=result.title or "Unknown role", location=result.location,
               description_text=html_to_text(result.description) if "<" in result.description else result.description,
               questions=result.questions, source="page", fetched_at=now_iso(), apply_url=result.apply_url or url)


def find_form(page: Any, timeout_ms: int = 15000) -> Optional[Any]:
    form = common.find_form(page, ANCHORS, timeout_ms=timeout_ms)
    if form is None and _click_apply(page) is not None:
        form = common.find_form(page, ANCHORS, timeout_ms=timeout_ms)
    return form


def fill_form(root: Any, answers: list[Any], resume_pdf: Path, *, country: Optional[str], city: Optional[str],
              pause: Callable[[], None]) -> list[FieldResult]:
    """Fill every planned answer the page has (found again by its label). Never touches Submit."""
    by_label: dict[str, list[tuple[str, str]]] = {}
    for f in root.evaluate(DISCOVER_JS):
        by_label.setdefault(" ".join(f["label"].lower().split()), []).append((f["key"], f["type"]))
    results: list[FieldResult] = []
    # a country pick can reset the state list after it (All Options' BambooHR, 2026-11-18: State went back to
    # "-Select-" and the submit failed), so the country goes first
    answers = sorted(answers, key=lambda a: 0 if getattr(a, "key", None) == "address_country" else 1)
    for a in answers:
        if a.kind in ("skip", "unknown") or (a.value is None and a.type != "file"):
            continue
        block = root.locator(f"[data-helper-key={json.dumps(a.field)}]")
        if not block.count():
            # a site that names its boxes anew on every load (Rippling, 2026-11-18: name, email and phone were "not
            # found"): the same question is the one with the same label and type, when exactly one has it
            same = by_label.get(" ".join(a.label.lower().split()), [])
            # a search box planned as a dropdown (its choices were read) reads as a text box again on the page
            keys = [k for k, t in same if t == a.type] or ([k for k, _ in same] if len(same) == 1 else [])
            if len(keys) == 1:
                block = root.locator(f"[data-helper-key={json.dumps(keys[0])}]")
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


_SUBMIT_TEXT = re.compile(r"(?i)^\s*(submit( (my |your )?application)?|apply( now)?|send( (my )?application)?|"
                          r"complete application|finish)\s*$")


def submit_buttons(form: Any) -> list[Any]:
    """The form's final Submit buttons: visible, enabled, saying Submit / Apply / Send application."""
    found = []
    buttons = form.locator("button, input[type=submit], [role=button]")
    for i in range(min(buttons.count(), 200)):
        b = buttons.nth(i)
        try:
            if not (b.is_visible() and b.is_enabled()):
                continue
            text = (b.inner_text(timeout=500) or b.get_attribute("value") or "").strip()
        except Exception:
            continue
        if _SUBMIT_TEXT.match(text):
            found.append(b)
    return found


def disabled_submit_buttons(form: Any) -> int:
    """Final Submit buttons the page shows but keeps turned off: Rippling's Apply stays aria-disabled until the site is
    satisfied (apexanalytix, 2026-11-18), which says more than "no Submit button"."""
    count = 0
    buttons = form.locator("button, input[type=submit], [role=button]")
    for i in range(min(buttons.count(), 200)):
        b = buttons.nth(i)
        try:
            if not b.is_visible() or b.is_enabled():
                continue
            text = (b.inner_text(timeout=500) or b.get_attribute("value") or "").strip()
        except Exception:
            continue
        if _SUBMIT_TEXT.match(text):
            count += 1
    return count


def submit(form: Any) -> bool:
    """Auto-submit (Vatsal, 2026-11-16): click the final Submit once, only when exactly one such button is there."""
    found = submit_buttons(form)
    if len(found) != 1:
        return False
    found[0].click()
    return True


def is_confirmation(page: Any, form: Optional[Any] = None) -> bool:
    return common.is_confirmation(page, form)


def is_submit_request(url: str, method: str) -> bool:
    """Never, on a company's own site: their pages save drafts with the same kind of request (Eightfold posted to an
    'apply' path while helper was still filling, and an unsent application was logged as sent; Boston Scientific,
    2026-11-17). A click on the final Submit button (`submit_clicked`) and the confirmation page decide instead."""
    return False


def submit_clicked(page: Any) -> bool:
    for frame in _frames(page):
        try:
            if frame.evaluate("() => window.__helperSubmitClicked === true"):
                return True
        except Exception:
            continue
    return False
