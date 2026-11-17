"""`helper assist <run_id>`: Vatsal applies, helper prepares (Vatsal, 2026-11-16).

For postings helper can't fill (SmartRecruiters turns automated browsers away; helper never disguises itself), or that
Vatsal would rather fill himself. It opens the posting in his normal browser (not helper's automated window), shows
the run's resume and cover letter in Finder, and prints a paste sheet of his details and the run's answers. Nothing is
typed anywhere; after he submits, `helper mark <id> applied` logs it.
"""

from __future__ import annotations

import platform
import subprocess
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import Paths, load_yaml
from .errors import HelperError
from .models import Job, Profile
from .runs import load_state, read_json, rel, run_dir

SHEET = "paste_sheet.txt"


@dataclass
class Assist:
    url: str
    resume: Optional[Path]
    letter: Optional[Path]
    letter_text: Optional[Path]
    rows: list[tuple[str, str]]
    application_id: Optional[int]


def _value(item: Any) -> str:
    if item is None:
        return ""
    value = getattr(item, "value", item)
    return "" if value is None else str(value)


def paste_rows(profile: Profile, grad_date: Optional[str], answers: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Field, answer: his details, then the run's own answers (helper's rules and Claude's drafts)."""
    t1, t2 = profile.tier1, profile.tier2
    name = " ".join(x for x in (t1.first_name, t1.last_name) if x) or (t1.full_name or "")
    place = ", ".join(x for x in (t1.address.city, t1.address.state) if x)
    rows = [("First name / Last name", f"{t1.first_name or ''} / {t1.last_name or ''}" if t1.first_name else name),
            ("Email", t1.email or ""), ("Phone", t1.phone or ""), ("Location", place),
            ("LinkedIn", t1.linkedin_url or ""), ("GitHub", t1.github_url or ""), ("Website", t1.portfolio_url or ""),
            ("School", t1.current_school or ""),
            ("Degree", " in ".join(x for x in (t1.degree, t1.major) if x)),
            ("Expected graduation", grad_date or "")]
    authorized = _value(t2.work_authorization)
    if authorized:
        rows.append(("Authorized to work in the US?", authorized))
    sponsorship = _value(t2.sponsorship)
    if sponsorship:
        rows.append(("Need sponsorship now or later?", f"{sponsorship} (Yes when the question says sponsorship includes "
                                                         "CPT/OPT)"))
    if t1.how_did_you_hear:
        rows.append(("How did you hear about us?", t1.how_did_you_hear))
    seen = {label for label, _ in rows}
    for a in answers:  # the run's planned answers, when it has any (a fill or `helper continue` wrote them)
        label, value = (a.get("label") or "").strip(), a.get("value")
        if not label or value in (None, "") or a.get("type") == "file" or label in seen:
            continue
        seen.add(label)
        rows.append((label if len(label) <= 90 else label[:87] + "...", str(value)))
    return [(k, v) for k, v in rows if v.strip(" /")]


def prepare(paths: Paths, run_id: str) -> Assist:
    folder = run_dir(paths, run_id)
    state = load_state(paths, run_id)
    result = state.options.get("result") or {}
    if not result.get("pdf"):
        raise HelperError(f"run {run_id} has no resume yet; finish it with `helper continue {run_id}` first.")
    job = Job.model_validate(read_json(folder / "job.json"))
    profile = Profile.model_validate(load_yaml(paths.profile))
    letter = state.options.get("cover_letter") or {}
    plan = read_json(folder / "answers_plan.json") if (folder / "answers_plan.json").is_file() else {}
    grad = (state.options.get("run") or {}).get("grad_date")

    def existing(p: Optional[str]) -> Optional[Path]:
        return (paths.root / p) if p and (paths.root / p).is_file() else None

    return Assist(url=job.apply_url or job.url or "", resume=existing(result.get("pdf")),
                  letter=existing(letter.get("pdf")), letter_text=existing(letter.get("text")),
                  rows=paste_rows(profile, grad, (plan or {}).get("answers") or []),
                  application_id=state.application_id)


def sheet_text(paths: Paths, a: Assist) -> str:
    width = max((len(k) for k, _ in a.rows), default=10)
    lines = [f"  {k.ljust(width)}  {v}" for k, v in a.rows]
    files = [f"  Resume:        {rel(paths, a.resume)}" if a.resume else "",
             f"  Cover letter:  {rel(paths, a.letter)}" if a.letter else "",
             f"  Letter text:   {rel(paths, a.letter_text)} (for a text box)" if a.letter_text else ""]
    return "\n".join(["Paste sheet", *lines, "", "Files", *[f for f in files if f]])


def _reveal(path: Path) -> None:
    if platform.system() == "Darwin":
        subprocess.run(["open", "-R", str(path)], check=False)


def assist(paths: Paths, run_id: str, *, open_browser: bool = True) -> str:
    a = prepare(paths, run_id)
    text = sheet_text(paths, a)
    (run_dir(paths, run_id) / SHEET).write_text(text + "\n", encoding="utf-8")
    opened = []
    if open_browser and a.url:
        webbrowser.open(a.url)  # the normal browser: nothing automated, nothing typed
        opened.append("Opened the posting in your browser.")
    if open_browser and a.resume:
        _reveal(a.resume)
        opened.append("Finder shows the resume and cover letter.")
    log = (f"When you've submitted, run `helper mark {a.application_id} applied` (or tell Claude)."
           if a.application_id else "When you've submitted, tell Claude so it's logged.")
    return "\n".join([*opened, f"Link: {a.url}" if a.url else "", "", text, "",
                      f"Saved as {rel(paths, run_dir(paths, run_id) / SHEET)}.", log]).strip()

