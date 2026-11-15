"""Render a resume through templates/resume.tex.j2 (spec 6).

The template is Vatsal's own resume layout and is never changed here. This module only supplies the
Jinja environment, the three escaping filters, and the content the template prints.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jinja2
from pydantic import model_validator

from .dates import display_month_year
from .errors import HelperError
from .models import Header, MasterData, ResumeMetaFile, StrictModel, TailorOutput, find_gpa_keys


class RenderError(HelperError):
    """Text the template can't print as intended (an em dash, an unknown symbol, unbalanced bold)."""


# --------------------------------------------------------------------------- escaping

_SPECIAL = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "^": r"\textasciicircum{}",
    # A bare ~ is a non-breaking space in LaTeX. Spec 6 asked for \textasciitilde{}, which prints a raised
    # tilde in this font encoding; Vatsal chose a tilde centered on the line instead (2026-11-14).
    "~": r"$\sim$",
    "<": r"\textless{}",  # the template's default font encoding prints < > | wrongly
    ">": r"\textgreater{}",
    "|": r"\textbar{}",
}

# Private-use placeholders, so markup added before escaping is not escaped itself.
_OPEN_DQ, _CLOSE_DQ, _OPEN_SQ, _SUP2 = "\ue000", "\ue001", "\ue002", "\ue003"

_SYMBOLS = {
    _OPEN_DQ: "``",
    _CLOSE_DQ: "''",
    _OPEN_SQ: "`",
    _SUP2: r"\textsuperscript{2}",
    '"': "''",  # an unpaired straight double quote
    "\u201c": "``",
    "\u201d": "''",
    "\u2018": "`",
    "\u2019": "'",
    "\u2026": r"\ldots{}",
    "\u00b2": r"\textsuperscript{2}",
    "\u00b3": r"\textsuperscript{3}",
    "\u00d7": "x",  # multiplication sign: "20x", "10 jurisdictions x 36 sectors"
    "\u2212": "-",  # minus sign
    "\u00a0": "~",  # no-break space
    "\u2013": "--",  # en dash (only outside bullets; see _check_dashes)
}

# Latin letters with accents print fine through LaTeX's default UTF-8 input ("Résumé").
_LATIN_OK = {chr(c) for c in range(0x00C0, 0x0100)} - {"\u00d7", "\u00f7"}

_WORD_SUBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?<![A-Za-z])R(?:[ -][Ss]quared|\^2|\u00b2)(?![A-Za-z0-9])"), "R" + _SUP2),
    (re.compile(r"\s*[\u2192\u21d2\u2794\u279c]\s*"), " to "),  # arrows: "45s→0.8s" -> "45s to 0.8s"
    (re.compile(r"\s*\u2605"), " stars"),  # "3k★" -> "3k stars"
    (re.compile(r"\u2265\s*"), "at least "),
    (re.compile(r"\u2264\s*"), "at most "),
    (re.compile(r"\u2705\s*"), ""),  # check-mark status icons
    (re.compile(r'"([^"\n]*)"'), _OPEN_DQ + r"\1" + _CLOSE_DQ),  # paired straight double quotes
    (re.compile(r"(?<!\w)'(?=\w)"), _OPEN_SQ),  # opening single quote; apostrophes stay
]

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _check_dashes(text: str, in_bullet: bool) -> None:
    if "---" in text or "\u2014" in text or "\u2015" in text:
        raise RenderError(f"em dash in {text!r} (rule 4: no em dashes). Use a comma or a period.")
    if in_bullet and ("--" in text or "\u2013" in text):
        raise RenderError(f"dash in bullet {text!r}. Bullets use commas or periods, not -- or an en dash.")


def _escape(text: Any, in_bullet: bool = False) -> str:
    if text is None:
        raise RenderError("a value the template prints is missing (None)")
    text = str(text)
    _check_dashes(text, in_bullet)
    for rx, replacement in _WORD_SUBS:
        text = rx.sub(replacement, text)
    out = []
    for ch in text:
        if ch in _SPECIAL:
            out.append(_SPECIAL[ch])
        elif ch in _SYMBOLS:
            out.append(_SYMBOLS[ch])
        elif ord(ch) < 128 or ch in _LATIN_OK:
            out.append(ch)
        else:
            raise RenderError(f"can't print {ch!r} (U+{ord(ch):04X}) in {text!r}. Reword it in plain text.")
    return "".join(out)


def tex(text: Any) -> str:
    """Escape plain text for LaTeX."""
    return _escape(text)


def _bold(match: re.Match[str]) -> str:
    # Math symbols ignore \textbf; \boldmath makes the centered tilde bold like the digits next to it.
    return r"\textbf{" + match.group(1).replace(r"$\sim$", r"{\boldmath$\sim$}") + "}"


def bullet(text: Any) -> str:
    """Escape a bullet, then turn **phrase** into \\textbf{phrase}."""
    escaped = _BOLD_RE.sub(_bold, _escape(text, in_bullet=True))
    if "**" in escaped:
        raise RenderError(f"unbalanced ** in bullet {text!r}")
    return escaped


def url(text: Any) -> str:
    """Escape only % and # (inside \\href)."""
    if text is None:
        raise RenderError("a link the template prints is missing (None)")
    return str(text).replace("%", r"\%").replace("#", r"\#")


# --------------------------------------------------------------------------- content the template prints


class SkillsLineContent(StrictModel):
    label: str
    items: list[str]


class ExperienceContent(StrictModel):
    title: str
    dates: str
    org: str
    location: str
    bullets: list[str]


class ProjectContent(StrictModel):
    title: str  # "Name: Subtitle"
    url: str
    tech_tags: list[str]
    dates: str
    bullets: list[str]


class ResumeContent(StrictModel):
    """Exactly what the template prints, already in display form."""

    header: Header  # phone as printed, e.g. "(206) 280--0258"
    grad_date_display: str  # "May 2027" or "Dec. 2027"
    skills_lines: list[SkillsLineContent]
    experience: list[ExperienceContent]
    projects: list[ProjectContent]
    coursework: list[str]

    @model_validator(mode="before")
    @classmethod
    def _no_gpa(cls, data: Any) -> Any:
        hits = find_gpa_keys(data)
        if hits:
            raise ValueError(f"GPA fields are not allowed (rule 3): {', '.join(hits)}")
        return data


def display_phone(phone: str) -> str:
    """The reference resume prints the phone's hyphen as an en dash: (206) 280--0258."""
    return re.sub(r"(?<=\d)-(?=\d)", "--", phone)


def display_header(header: Header) -> Header:
    return header.model_copy(update={"phone": display_phone(header.phone)})


def build_content(tailor: TailorOutput, master: MasterData, meta: ResumeMetaFile, header: Header) -> ResumeContent:
    """Combine tailor output (wording) with master.yaml (dates, links, organizations) and resume_meta.yaml."""
    experience = []
    for e in tailor.experience:
        item = _master_item(master, e.entry_id, "experience")
        entry_meta = meta.experience.get(e.entry_id)
        location = (entry_meta.location if entry_meta else None) or item.location
        if not location:
            raise RenderError(f"{e.entry_id}: no location. Add it to data/resume_meta.yaml.")
        if not item.organization or not item.dates:
            raise RenderError(f"{e.entry_id}: the master list has no organization or dates for it")
        experience.append(ExperienceContent(
            title=e.title, dates=item.dates.display, org=item.organization, location=location, bullets=e.bullets,
        ))
    projects = []
    for p in tailor.projects:
        item = _master_item(master, p.project_id, "project")
        name = meta.project_display_names.get(p.project_id)
        if not name:
            raise RenderError(f"{p.project_id}: no display name. Add it to data/resume_meta.yaml.")
        if not item.github_url or not item.dates:
            raise RenderError(f"{p.project_id}: the master list has no link or dates for it")
        title = f"{name}: {p.subtitle}" if p.subtitle.strip() else name
        projects.append(ProjectContent(
            title=title, url=item.github_url, tech_tags=p.tech_tags, dates=item.dates.display, bullets=p.bullets,
        ))
    return ResumeContent(
        header=display_header(header),
        grad_date_display=display_month_year(tailor.grad_date),
        skills_lines=[SkillsLineContent(label=s.label, items=s.items) for s in tailor.skills_lines],
        experience=experience,
        projects=projects,
        coursework=tailor.coursework,
    )


def _master_item(master: MasterData, item_id: str, kind: str) -> Any:
    try:
        item = master.item(item_id)
    except KeyError:
        raise RenderError(f"no {kind} '{item_id}' in master.yaml") from None
    if item.kind != kind:
        raise RenderError(f"'{item_id}' is in the master list's {item.kind} section, not its {kind}s")
    return item


# --------------------------------------------------------------------------- rendering


def drop_leading_comment_lines(source: str) -> tuple[str, int]:
    """The template opens with % comment lines that mention \\VAR{ ... } and \\BLOCK{ ... } literally, which
    Jinja would try to parse. They never affect the PDF, so they are dropped before Jinja reads the file.
    Returns the rest of the file unchanged and the number of lines dropped."""
    lines = source.splitlines(keepends=True)
    dropped = 0
    while dropped < len(lines) and lines[dropped].lstrip().startswith("%"):
        dropped += 1
    return "".join(lines[dropped:]), dropped


class _TemplateLoader(jinja2.BaseLoader):
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def get_source(self, environment: jinja2.Environment, template: str) -> tuple[str, str, Any]:
        path = self.folder / template
        if not path.is_file():
            raise jinja2.TemplateNotFound(template)
        mtime = path.stat().st_mtime
        source, _ = drop_leading_comment_lines(path.read_text(encoding="utf-8"))
        return source, str(path), lambda: path.is_file() and path.stat().st_mtime == mtime


def make_env(template_dir: Path) -> jinja2.Environment:
    env = jinja2.Environment(
        loader=_TemplateLoader(template_dir),
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    env.filters.update(tex=tex, bullet=bullet, url=url)
    return env


def render_tex(content: ResumeContent, template: Path) -> str:
    env = make_env(template.parent)
    # Pass the models, not dicts: the template's `line.items` would find dict.items() on a dict.
    variables = {name: getattr(content, name) for name in ResumeContent.model_fields}
    return env.get_template(template.name).render(**variables)


def strip_leading_comments(tex_source: str) -> str:
    """Drop the template's leading % comment lines (they don't change the PDF)."""
    lines = tex_source.splitlines()
    while lines and lines[0].lstrip().startswith("%"):
        lines.pop(0)
    return "\n".join(lines)


def tex_differences(rendered: str, reference: str) -> list[str]:
    """Lines that differ, ignoring the template's leading comments and whitespace (spec 6)."""
    import difflib

    def norm(text: str) -> list[str]:
        return [" ".join(line.split()) for line in strip_leading_comments(text).splitlines() if line.strip()]

    return [
        line for line in difflib.unified_diff(norm(reference), norm(rendered), "reference", "rendered", lineterm="", n=0)
        if not line.startswith("@@")
    ]


def resume_basename(name: str) -> str:
    """The resume's file name: VatsalPatel_Resume, nothing more (Vatsal, 2026-11-14; replaces spec 6's
    _<Company>_<RoleShortName> suffix). Each run's copy lives in its own folder, resumes/<run_id>/."""
    camel = "".join(p[:1].upper() + p[1:] for p in re.findall(r"[A-Za-z0-9]+", name))
    if not camel:
        raise RenderError(f"can't build a file name from name={name!r}")
    return f"{camel}_Resume"


def load_content(path: Path) -> ResumeContent:
    import yaml

    return ResumeContent.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def write_tex(tex_source: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tex_source, encoding="utf-8")
    return path

