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
import time
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
  // range sliders are a video player's seek and volume bars, not questions (Disney's posting page, 2026-11-18)
  const skipType = t => ['hidden', 'submit', 'button', 'reset', 'image', 'search', 'password', 'range', 'color']
    .includes(t);
  // ARIA widgets with no <input> of their own are questions too: Rippling's dropdowns are role=combobox divs, and its
  // rating questions are role=radio divs in a role=radiogroup (apexanalytix, Raptor Maps, 2026-11-18)
  // Oracle's multi-select pills are buttons in a ul role=list ("Check all of the following statements that apply to
  // you:", Honeywell, 2026-11-20): never read, so a required question stayed empty
  const CONTROLS = 'input, select, textarea, [role=combobox], [role=radiogroup], ul.cx-select-pills-container[role=list]';
  const isField = c => ['INPUT', 'SELECT', 'TEXTAREA'].includes(c.tagName);
  const all = [...document.querySelectorAll(CONTROLS)].filter(c => {
    if (c.closest('header, nav, footer, [role=search], [role=navigation]')) return false;
    if (!isField(c)) {  // a radio group with real inputs, or a combobox wrapping its input, is read by those inputs
      if (c.querySelector('input, select, textarea')) return false;
      if (c.getAttribute('role') === 'radiogroup' && !c.querySelector('[role=radio]')) return false;
      if (c.getAttribute('role') === 'list' && !c.querySelector('.cx-select-pill-section')) return false;
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
  // a group that holds only these boxes names their question: each Oracle radio's own aria-labelledby names its
  // option, so the disability question read as "Yes, I have a disability, or have had one in the past" (Ralliant and
  // Vertiv, 2026-11-19)
  const groupName = members => {
    const g = members[0].closest('[role=radiogroup], [role=group], fieldset');
    if (!g || !members.every(m => g.contains(m))) return '';
    if ([...g.querySelectorAll('input, select, textarea')].some(x => !members.includes(x)
        && !skipType((x.type || '').toLowerCase()))) return '';  // a section's group, not this question's
    const ids = (g.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const named = ids.map(id => text(document.getElementById(id))).join(' ').trim();
    if (named) { labelEl = document.getElementById(ids[ids.length - 1]); return named; }
    return (g.getAttribute('aria-label') || '').trim();
  };
  const own = (c, members) => {
    const kind = (c.type || '').toLowerCase();
    if (c.labels && c.labels.length && (!['radio', 'checkbox'].includes(kind) || (kind === 'checkbox' && !members))) {
      const words = text(c.labels[0]);  // a lone checkbox's own label is the question ("I agree to the privacy policy")
      if (!(kind === 'file' && DROP_ZONE.test(words))) { labelEl = c.labels[0]; return words; }  // Rippling: drop zone
    }
    const group = members ? groupName(members) : '';
    if (group) return group;
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
  // Dropzone keeps its real file box at the end of the page, nameless and away from the "Upload Resume" area it serves,
  // so the resume was never uploaded (Disney's careers site, 2026-11-18): the clickable areas and the hidden boxes come
  // in the same order, and the area's group names the box
  const zoneLabel = c => {
    if (!c.classList || !c.classList.contains('dz-hidden-input')) return '';
    const zone = document.querySelectorAll('.dz-clickable')[[...document.querySelectorAll('input.dz-hidden-input')].indexOf(c)];
    if (!zone) return '';
    let group = zone;  // the widest box around the area that holds no other question: "Upload Resume*" with its mark
    for (let up = zone.parentElement, i = 0; up && up !== document.body && i < 12; up = up.parentElement, i++) {
      if (up.querySelector('input:not([type=hidden]):not(.dz-hidden-input), select, textarea')) break;
      group = up;
    }
    labelEl = group;
    return (group.innerText || '').split('\n').map(s => s.trim()).find(Boolean) || '';
  };
  const out = [], done = new Set();
  all.forEach((c, n) => {
    if (done.has(c)) return;
    const t = (c.type || '').toLowerCase();
    let members = [c];
    if ((t === 'radio' || t === 'checkbox') && c.name) members = all.filter(x => x.name === c.name && x.type === c.type);
    else if (t === 'radio' || t === 'checkbox') {
      // boxes with no name in one named group are one question: Oracle's race boxes ("Select the races you identify
      // with.") were read one by one, each named by its race, and all left blank (Honeywell, 2026-11-20)
      const g = c.closest('[role=group], [role=radiogroup]');
      if (g && (g.getAttribute('aria-labelledby') || g.getAttribute('aria-label')))
        members = all.filter(x => !x.name && x.type === c.type && x.closest('[role=group], [role=radiogroup]') === g);
    }
    members.forEach(m => done.add(m));
    const box = wrapper(members);
    const role = c.getAttribute('role');
    labelEl = null;
    const named = (t === 'file' && zoneLabel(c)) || own(c, members.length > 1 || t === 'radio' ? members : null)
      || nearbyLabel(box, members);
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
    else if (role === 'list') {
      type = 'multi_select';
      options = [...c.querySelectorAll('.cx-select-pill-name')].map(r => text(r)).filter(Boolean);
    }
    else if (role === 'combobox' && !isField(c)) { type = 'select'; listbox = true; }  // its choices open on a click
    else if (t === 'radio') { type = 'radio'; options = members.map(optionLabel); }
    else if (t === 'checkbox') {
      if (members.length > 1) { type = 'multi_select'; options = members.map(optionLabel); }
      else { type = 'boolean'; options = [optionLabel(c) || label]; }
    } else if (t === 'date') type = 'date';
    // a search box that opens a list: a dropdown when its choices show without typing (discover() opens it to see),
    // a place search otherwise
    // Oracle's cx-select opens a grid (aria-haspopup="grid"; Nationality on Vertiv's form, 2026-11-19)
    else if (role === 'combobox' && /^(listbox|grid)$/.test(c.getAttribute('aria-haspopup') || '')) listbox = true;
    const required = members.some(m => m.required || m.getAttribute('aria-required') === 'true')
      || /\*|\(required\)/i.test(label) || starred(labelEl) || (box !== c && starred(box));
    const key = 'generic:' + (c.name || c.id || 'field-' + n);
    if (out.some(o => o.key === key)) return;
    box.setAttribute('data-helper-key', key);
    // a box that filters its list as one types: what it shows first may be only the start of the list
    const search = c.tagName === 'INPUT' && role === 'combobox' && /^(list|both)$/.test(c.getAttribute('aria-autocomplete') || '');
    out.push({key, label: label.replace(/^\s*\*+\s*/, '').replace(/\s*\*+\s*$/, '').replace(/\s*\(required\)\s*$/i, '').trim(),
              type, required, options, listbox, search});
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
                                      options=f["options"], section=section,
                                      searchable=bool(f.get("search")) and f["type"] == "select"))
    return questions


_GENERIC_SITE_NAME = re.compile(r"(?i)^\s*(candidate experience( site)?|careers?( site| page| portal)?|jobs?|job search|"
                                r"job board|career opportunities|recruiting\d*|talent)\s*$")  # recruiting2.ultipro.com
_COPYRIGHT_RE = re.compile(r"(?:©|\(c\)|Copyright)\s*(?:©\s*)?(?:\d{4}(?:\s*[-–]\s*\d{4})?\s*)?"
                           r"([A-Z][A-Za-z0-9&'. -]{1,60}?)(?:,?\s+(?:Inc|LLC|Ltd|Corp|Corporation|Co)\b\.?|\.|\s+All rights)",
                           re.IGNORECASE)


def site_employer(site: str) -> str:
    """The employer in a site's name: "JPMC Candidate Experience page" is Oracle's site name with JPMC in front
    (2026-11-23)."""
    name = re.sub(r"(?i)\s*[-|:]?\s*candidate experience(?: site| page)?\s*$", "", site or "").strip()
    # a site named by its own address: "Amazon.jobs" is Amazon (2026-11-24)
    return re.sub(r"(?i)^([a-z][\w&-]*)\.(?:jobs|careers|com|io|ai)$", r"\1", name)


_LEGAL_TAIL_RE = re.compile(r"(?i)[,\s]+(?:inc|llc|l\.l\.c|ltd|corp|corporation|plc|lp|l\.p)\.?\s*$")


def employer_brand(company: str, url: str, text: str) -> str:
    """The brand a company's own careers site is named for, over a payroll entity in its job data: Disney's
    join.disneycareers.com named "Woodlands Entertainment LLC PAY SVC" for a Disney Entertainment and ESPN internship
    (2026-11-25). Otherwise the name without its legal suffix ("Disney Entertainment & Sports LLC")."""
    name = (company or "").strip()
    for _ in range(2):
        name = _LEGAL_TAIL_RE.sub("", name).strip(" ,.")
    labels = (urlparse(url).hostname or "").lower().split(".")
    site = labels[-2] if len(labels) >= 2 else ""
    brand = re.sub(r"(?:careers?|jobs?)$", "", re.sub(r"^(?:careers?|jobs?|join)", "", site))
    squash = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())  # noqa: E731
    named = len(re.findall(rf"(?i)(?<![\w-]){re.escape(brand)}(?![\w-])", text or "")) if brand else 0
    if len(brand) >= 4 and squash(brand) not in squash(name) and named >= 3:
        return as_spelled(brand, text)
    return name or (company or "").strip()


def as_spelled(name: str, text: str) -> str:
    """A name taken from the site's address, spelled the way the posting spells it: careers.sap.com is "SAP", not
    "Sap" (2026-11-23). Title case when the posting never writes it."""
    found = re.findall(rf"(?i)(?<![\w-]){re.escape(name)}(?![\w-])", text or "")
    return max(sorted(set(found)), key=found.count) if found else name.title()


def ukg_employer(homepage: str, text: str) -> str:
    """UKG Pro job boards (recruiting.ultipro.com/<tenant>/...) name the employer only by their footer link to its own
    site (CAS: www.cas.org/careers; Melaleuca: melaleucajobs.com, 2026-11-20): the site's name, as the description
    spells it. "" when the description never names it."""
    host = (urlparse(homepage).hostname or "").removeprefix("www.").removeprefix("careers.").removeprefix("jobs.")
    label = re.sub(r"(?i)(jobs|careers?)$", "", host.split(".")[0]) if host.count(".") >= 1 else ""
    m = re.search(rf"(?i)(?<![\w-]){re.escape(label)}(?![\w-])", text or "") if len(label) >= 2 else None
    return m.group(0) if m else ""


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


_ORACLE_JOB_RE = re.compile(r"(?i)/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/(?:[^/]+/)*job/([A-Za-z]*\d+)")
# The site's own requisition endpoint, asked from the page (same origin, as the page's scripts do): read-only
_ORACLE_DETAILS_JS = r"""
async ([site, id]) => {
  const finder = `ById;Id="${id}",siteNumber=${site}`;
  const url = '/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder='
    + encodeURIComponent(finder);
  try {
    const r = await fetch(url, {headers: {Accept: 'application/json'}});
    if (!r.ok) return null;
    const item = ((await r.json()).items || [])[0];
    return item ? [item.ExternalDescriptionStr, item.ExternalResponsibilitiesStr, item.ExternalQualificationsStr]
      .filter(Boolean) : null;
  } catch (e) { return null; }
}
"""


def oracle_description(page: Any) -> str:
    """The whole posting on an Oracle Candidate Experience page: its description, responsibilities and
    qualifications. The page's JobPosting data carries only the description, so the qualifications were never read
    (Atlantic Health, 2026-11-19: "PharmD student" and its work authorization rule were missed)."""
    m = _ORACLE_JOB_RE.search(urlparse(page.url).path)
    if not m:
        return ""
    try:
        parts = page.evaluate(_ORACLE_DETAILS_JS, [m.group(1), m.group(2)])
    except Exception:
        return ""
    return "\n\n".join(html_to_text(p).strip() for p in parts or [] if isinstance(p, str) and p.strip()).strip()


def read_posting(page: Any) -> tuple[str, str, str]:
    """(role title, company, description) from the page itself."""
    title, company, text = _read_posting(page)
    if "oraclecloud" in (urlparse(page.url).hostname or ""):
        whole = oracle_description(page)
        if len(whole) > len(text):
            text = whole
    return title, company, text


def _read_posting(page: Any) -> tuple[str, str, str]:
    data = page.evaluate(r"""() => {
      const t = el => el ? (el.innerText || '').trim() : '';
      const meta = n => (document.querySelector(`meta[property="${n}"], meta[name="${n}"]`) || {}).content || '';
      const main = document.querySelector('main, article, [role=main], #content, .job-description, [class*=description]');
      const blocks = [...document.querySelectorAll('div, section')].map(d => [t(d).length, d]).sort((a, b) => b[0] - a[0]);
      const foot = t(document.querySelector('footer')) || (t(document.body) || '').slice(-3000);
      return {h1: t(document.querySelector('h1')), title: document.title, site: meta('og:site_name'),
              ogtitle: meta('og:title'), text: t(main) || (blocks[0] ? t(blocks[0][1]) : t(document.body)),
              footer: foot, homepage: (document.querySelector('.copyright a[href]') || {}).href || ''};
    }""")
    title = data["h1"] or data["ogtitle"] or data["title"]
    host = (urlparse(page.url).hostname or "").removeprefix("www.").removeprefix("careers.").removeprefix("jobs.")
    label = host.split(".")[0].replace("-", " ") if host else ""
    company = site_employer(data["site"] or "") or (as_spelled(label, data.get("text") or "") if label else "")
    if _GENERIC_SITE_NAME.match(company) or "oraclecloud" in host:
        # Oracle's "Candidate Experience site" is no employer (Fortinet, 2026-11-16): use the copyright line
        owner = copyright_owner(data.get("footer") or "")
        company = owner or ("" if _GENERIC_SITE_NAME.match(company) else company)
    if host.endswith("rippling.com"):  # "Rippling Recruiting" is the hiring site (Raptor Maps, apexanalytix, 2026-11-18)
        company = rippling_employer(page.url) or company
    if host.endswith(("ultipro.com", "ukg.net")):  # UKG's own host names no employer (CAS, Melaleuca, 2026-11-20)
        company = ukg_employer(data.get("homepage") or "", data["text"])
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
    return !['hidden', 'submit', 'button', 'reset', 'image', 'search', 'password', 'range', 'color'].includes(t)
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


def step_form(page: Any, timeout_ms: int = 15000) -> Optional[Any]:
    """A later page of an application helper is already in: the frame with the most real fields. Its first page's
    test (an email or resume box) doesn't hold there: Veeva's page 2 asks other things (2026-11-23)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        sizes = []
        for f in _frames(page):
            try:
                sizes.append((f.evaluate(_FORM_SIZE_JS)["count"], f))
            except Exception:
                continue
        best = max(sizes, key=lambda x: x[0], default=(0, None))
        if best[0] >= 1 or time.monotonic() >= deadline:
            return best[1] if best[0] >= 1 else None
        page.wait_for_timeout(500)


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


_WORKABLE_RE = re.compile(r"^https?://apply\.workable\.com/([^/?#]+)/j/([0-9A-Za-z]+)", re.I)


def workable_job(url: str, session: Optional[Any] = None) -> Optional[Job]:
    """A Workable posting from Workable's public posting API. Its links often open the application form (".../apply"),
    whose page has no description and whose title is "Apply" (Serko, 2026-11-24: the company read as "Apply")."""
    import requests

    m = _WORKABLE_RE.match(url or "")
    if not m:
        return None
    account, code = m.groups()
    session = session or requests.Session()
    try:
        r = session.get(f"https://apply.workable.com/api/v2/accounts/{account}/jobs/{code}", timeout=30)
        if r.status_code != 200:
            return None
        data = r.json()
        a = session.get(f"https://apply.workable.com/api/v1/accounts/{account}", timeout=30)
        company = (a.json().get("name") if a.status_code == 200 else None) or None
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("title"):
        return None
    places = [p for p in (data.get("locations") or [data.get("location") or {}]) if isinstance(p, dict)
              and not p.get("hidden")]
    location = "; ".join(", ".join(x for x in (p.get("city"), p.get("region") or p.get("country")) if x)
                         for p in places) or None
    if data.get("remote"):
        location = f"{location} (remote)" if location else "Remote"
    parts = [(h, data.get(k)) for h, k in (("", "description"), ("Requirements", "requirements"),
                                          ("Benefits", "benefits"))]
    body = "\n\n".join(f"{h}\n{html_to_text(v)}" if h else html_to_text(v) for h, v in parts if v)
    return Job(url=url, ats="workable", company=company or account, role_title=data["title"], location=location,
               description_text=body, source="workable_api", fetched_at=now_iso(),
               apply_url=f"https://apply.workable.com/{account}/j/{code}/apply")


def fetch_job(url: str, session: Optional[Any] = None, scan: Callable[[str], Scan] = scan_posting) -> Job:
    from ..ats import detect_ats
    from ..errors import HelperError, NeedsUser

    if _WORKABLE_RE.match(url or "") and (job := workable_job(url, session)) is not None:
        # the questions still come from the form itself: without them the plan was empty and the fill typed nothing
        # (Serko, 2026-11-24)
        try:
            form = scan(job.apply_url or url)
        except Exception:
            form = None
        if form is not None and form.robot_check:
            raise NeedsUser(f"Workable showed a \"check you are not a robot\" page for {url}, and helper never gets past "
                            "those (rule 6). Apply in your own browser (`helper assist <run_id>`).")
        return job.model_copy(update={"questions": form.questions if form is not None else []})
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
    company = employer_brand(result.company or "", url, result.description)
    return Job(url=url, ats=ats if ats != "unknown" else "generic", company=company or "Unknown",
               role_title=result.title or "Unknown role", location=result.location,
               description_text=html_to_text(result.description) if "<" in result.description else result.description,
               questions=result.questions, source="page", fetched_at=now_iso(), apply_url=result.apply_url or url)


_COOKIE_DECLINE_JS = r"""
() => {
  const boxes = [...document.querySelectorAll('[role=dialog], [role=alertdialog], [aria-modal=true], [id*=cookie i], ' +
                 '[class*=cookie i], [data-ui*=cookie i], [id*=consent i], [class*=consent i]')]
    .filter(b => /cookie/i.test((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '').slice(0, 600)));
  for (const b of boxes) {
    const btn = [...b.querySelectorAll('button, [role=button], a')].find(x =>
      /^(decline|reject|refuse)( all)?( optional)?( cookies)?$|^(only )?(necessary|essential|required) cookies only$|^use necessary cookies only$/i
        .test((x.innerText || x.getAttribute('aria-label') || '').trim()));
    if (btn && btn.offsetParent !== null) { btn.click(); return true; }
  }
  return false;
}
"""


def decline_cookies(page: Any) -> bool:
    """Decline a cookie banner's optional cookies, so it no longer covers the form: Workable's notice laid a backdrop
    over the Submit button (Serko, 2026-11-24). Only a Decline/Reject button inside a box about cookies is clicked."""
    try:
        clicked = bool(page.evaluate(_COOKIE_DECLINE_JS))
        if clicked:
            page.wait_for_timeout(500)
        return clicked
    except Exception:
        return False


def find_form(page: Any, timeout_ms: int = 15000) -> Optional[Any]:
    decline_cookies(page)
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
    # "-Select-" and the submit failed), so the country goes first; and on Oracle a State pick cleared the ZIP, City
    # and County picked before it (Ralliant, Honeywell, 2026-11-20), so the place goes from the widest to the narrowest
    answers = sorted(answers, key=lambda a: _GEO_ORDER.get(getattr(a, "key", None) or "", len(_GEO_ORDER)))
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
        if getattr(a, "key", None) in _GEO_ORDER and a.value and _same_place(_shown_value(block), a.value):
            # already right (a ZIP pick fills its city and county): picking it again can clear the fields tied to it
            results.append(FieldResult(a.field, a.label, True, _shown_value(block), "already set"))
            continue
        try:
            ok, shown, detail = dom._fill_one(root, block, a, resume_pdf, city, _iso_date)
        except Exception as exc:
            ok, shown, detail = False, a.value, f"{type(exc).__name__}: {str(exc)[:120]}"
        results.append(FieldResult(a.field, a.label, ok, shown, detail))
        pause()
    # a place that ended up empty after the others were picked is picked once more, narrowest last
    for a in answers:
        if getattr(a, "key", None) not in ("address_zip", "address_city", "address_county") or not a.value \
                or a.kind in ("skip", "unknown"):
            continue
        block = root.locator(f"[data-helper-key={json.dumps(a.field)}]")
        if not block.count() or _shown_value(block):
            continue
        try:
            ok, shown, detail = dom._fill_one(root, block, a, resume_pdf, city, _iso_date)
        except Exception as exc:
            ok, shown, detail = False, a.value, f"{type(exc).__name__}: {str(exc)[:120]}"
        results = [r for r in results if r.field != a.field] + [FieldResult(a.field, a.label, ok, shown, detail)]
    try:
        root.evaluate(SUBMIT_WATCH_JS)
    except Exception:
        pass
    return results


_GEO_ORDER = {"address_country": 0, "address_state": 1, "address_zip": 2, "address_city": 3, "address_county": 4}


_FILLED_JS = r"""
b => {
  const els = b.matches('input, textarea, select') ? [b] : [...b.querySelectorAll('input:not([type=hidden]), textarea, select')];
  const has = e => (e.type === 'radio' || e.type === 'checkbox') ? e.checked
    : e.type === 'file' ? !!(e.files && e.files.length)
    : e.tagName === 'SELECT' ? e.selectedIndex > 0 || (e.value || '').trim() !== ''
    : (e.value || '').trim() !== '';
  if (els.some(has)) return true;
  if (b.querySelector('[aria-checked=true], [aria-selected=true][role=option]')) return true;
  if (/\.(pdf|docx?|rtf|odt)\b/i.test(b.innerText || '')) return true;  // an upload shows its file's name
  const combo = b.getAttribute('role') === 'combobox' ? b : b.querySelector('[role=combobox]');
  const shown = combo && !combo.matches('input') ? (combo.innerText || '').trim() : '';
  return !!shown && !/^(select|choose|--|search)/i.test(shown);
}
"""


def empty_required(root: Any) -> list[str]:
    """The labels of the form's own required fields that still show nothing. The plan alone can't say: Serko's
    Workable form was read as having no questions, so helper filled nothing and found nothing missing (2026-11-24)."""
    try:
        fields = root.evaluate(DISCOVER_JS)
    except Exception:
        return []
    empty = []
    for f in fields:
        if not f.get("required"):
            continue
        block = root.locator(f"[data-helper-key={json.dumps(f['key'])}]")
        try:
            if block.count() and not block.first.is_visible():
                continue
            if not block.count() or not block.first.evaluate(_FILLED_JS):
                empty.append(f["label"])
        except Exception:
            continue
    return list(dict.fromkeys(empty))


def _shown_value(block: Any) -> str:
    """What a place box shows now: its text box's value, or a dropdown's chosen option."""
    try:
        box = block.locator("input:not([type=hidden]):not([type=checkbox]):not([type=radio])")
        if box.count():
            return (box.first.input_value() or "").strip()
        pick = block.locator("select")
        if pick.count():
            return (pick.first.evaluate("s => s.selectedIndex > 0 ? s.options[s.selectedIndex].text : ''") or "").strip()
    except Exception:
        pass
    return ""


def _same_place(shown: str, wanted: str) -> bool:
    """'Seattle, King, WA' shown for 'Seattle, King, WA' or 'Seattle': the same place."""
    a, b = " ".join(shown.lower().split()), " ".join(wanted.lower().split())
    return bool(a) and (a == b or a.split(",")[0].strip() == b.split(",")[0].strip())


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


# A step button on a form that spans pages ("Page 1 of 3", Veeva, 2026-11-23): the whole label, so "Continue with
# LinkedIn" (a sign-in) never counts, and never a final Submit
_NEXT_TEXT = re.compile(r"(?i)^\s*(next( step| page)?|continue|save (and|&) (continue|next)|proceed)\s*[>›→]?\s*$")


def next_button(form: Any) -> Optional[Any]:
    """The form's one visible, enabled Next button, when it has no final Submit button yet."""
    if submit_buttons(form):
        return None
    found = []
    buttons = form.locator("button, input[type=submit], input[type=button], [role=button], a[role=button]")
    for i in range(min(buttons.count(), 200)):
        b = buttons.nth(i)
        try:
            if not (b.is_visible() and b.is_enabled()):
                continue
            text = (b.inner_text(timeout=500) or b.get_attribute("value") or "").strip()
        except Exception:
            continue
        if _NEXT_TEXT.match(text):
            found.append(b)
    return found[0] if len(found) == 1 else None


_SIGNATURE_JS = r"""
() => location.href + '#' + [...document.querySelectorAll('input, select, textarea')]
  .filter(e => e.type !== 'hidden' && (e.offsetParent !== null || e.type === 'file'))
  .map(e => e.name || e.id || e.type).join('|')
"""


def form_signature(page: Any) -> str:
    """What the page's form is now: its address and its fields. A different signature after Next is the next step."""
    parts = []
    for frame in _frames(page):
        try:
            parts.append(frame.evaluate(_SIGNATURE_JS))
        except Exception:
            continue
    return "||".join(parts)


def submit(form: Any) -> bool:
    """Auto-submit (Vatsal, 2026-11-16): click the final Submit once, only when exactly one such button is there."""
    found = submit_buttons(form)
    if len(found) != 1:
        return False
    try:
        # trial first: a banner over the button (Workable's cookie notice, Serko, 2026-11-24) made the click time out
        # and the fill stop; the trial click never presses the button, so nothing can have been sent
        found[0].click(trial=True, timeout=10000)
    except Exception:
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
