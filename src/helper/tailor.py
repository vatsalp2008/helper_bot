"""tailor_input.json: everything Claude needs to write tailor_output.json for one posting (spec 7, 9 step 5)."""

from __future__ import annotations

from typing import Any

from .keywords import Keywords
from .models import Job, MasterItem
from .validate import Context, RunOptions

OUTPUT_SCHEMA: dict[str, Any] = {
    "role_type": "a key of role_types",
    "grad_date": "exactly run.grad_date",
    "experience": [{"entry_id": "ta", "title": "exact title", "bullets": ["...", "..."]}],
    "projects": [{
        "project_id": "id",
        "subtitle": "reworded to fit the posting; facts unchanged",
        "tech_tags": ["a", "b", "c"],
        "bullets": ["...", "..."],
        "reason": "optional: one line on why this project fits",
    }],
    "skills_lines": [{"label": "role-appropriate label", "items": ["terms the master list supports"]}],
    "coursework": ["five entries from courses"],
    "keyword_audit": {"matched": ["posting terms on the resume verbatim"], "skills_only": ["true, only in skills lines"],
                      "gaps": ["posting terms the master list doesn't have"]},
    "flags": ["qualitative: <item_id> bullet <n>: <why>", "anything Vatsal should look at"],
    "fit_paragraph": "optional: one paragraph on fit, shown before filling the form",
}


def rules(run: RunOptions) -> list[str]:
    return [
        "Only facts from the items below (the master list). No new tools, numbers, employers, courses or "
        "responsibilities. Facts never change; wording may.",
        "Numbers: each one must be in that item's allowed_numbers, with its precision marker (~, +, <, >) and unit. "
        "Label simulated data as simulated, PulseTrader data as trade events, borderline results as borderline; never "
        "join separately measured numbers into one claim.",
        "Technologies in a bullet or tag must be in that item's allowed_tech (framing additions and variants included). "
        "In projects only, an approved family swap may name another member in place of the one the project used; it "
        "replaces, never adds. Experience entries keep their own tools.",
        "Prefer the posting's own tool names in project bullets and tags: name the posting's tool when the project "
        "used it, or swap it in through an approved family (keywords.tech lists the swaps).",
        "Follow every item's wording_rules (some cover the whole entry, like naming one database of a framed pair).",
        f"Experience: Graduate Teaching Assistant always, with its exact title; exactly one Kalini internship by track "
        f"(Software Development Intern for SWE, infra, full-stack, mobile, automation; Machine Learning Intern for ML, "
        f"data science, data, AI). Titles exactly as given. Off-by-default entries only if requested: "
        f"{', '.join(run.include_experience) or 'none requested'}.",
        f"Projects: exactly {run.projects_per_resume}, by verbatim keyword and domain overlap; prefer the project that "
        "genuinely used a named tool."
        + (f" The posting values {', '.join(run.open_source_signals)}, so include chef or spiceai." if run.require_open_source
           else ""),
        f"Exactly {run.bullets_per_item} bullets per item, each 1 to 2 rendered lines. XYZ formula, strong past-tense verb "
        "first (present tense is fine for in-progress work). Bullet 1: what was built and the headline metric. Bullet 2: "
        "reliability, speed, testing, or the second-strongest result. TA bullets read as teaching.",
        "Bold (**phrase**) only metrics and core technologies or posting keywords, at most 4 per bullet.",
        "No em dashes, no -- and no en dashes in bullets. No GPA. No summary section.",
        "A bullet with no number gets the flag 'qualitative: <item_id> bullet <n>: <why>'.",
        "Exactly 3 skills lines with labels fitted to the role. Terms from the master Skills section, any item's text, "
        "or an approved skill synonym. When the posting names a skill the master list has under another name, use the "
        "posting's name. A new name with clear master-list evidence goes into data/skill_synonyms.yaml (evidence, "
        "approved: true) and into flags as 'new synonym: <term>'.",
        "Exactly 5 courses, from courses.",
        f"grad_date is {run.grad_date}.",
        "Keyword audit: use the posting's exact wording where true. matched = on the resume verbatim; skills_only = true "
        "but only in skills lines; gaps = posting terms the master list doesn't have, never forced in. Sort every "
        "technology in keywords.tech.",
    ]


def _item(item: MasterItem, ctx: Context, scores: dict[str, tuple[int, list[str]]]) -> dict[str, Any]:
    score, hits = scores.get(item.id, (0, []))
    entry: dict[str, Any] = {"id": item.id, "kind": item.kind}
    if item.kind == "project":
        entry["display_name"] = ctx.meta.project_display_names.get(item.id)
    else:
        meta = ctx.meta.experience.get(item.id)
        entry["title"] = (meta.resume_title if meta and meta.resume_title else item.name)
        entry["organization"] = item.organization
        entry["location"] = (meta.location if meta and meta.location else item.location)
    entry.update({
        "heading": item.heading,
        "status": item.status,
        "dates": item.dates.display if item.dates else None,
        "keyword_score": score,
        "keyword_hits": hits,
        "allowed_tech": item.allowed_tech,
        "framing_additions": item.framing_additions,
        "implied_tech": item.implied_tech,
        "variants": item.variants,
        "allowed_numbers": item.allowed_numbers,
        "allowed_tokens": item.allowed_tokens,
        "wording_rules": [f"{r.id} ({r.scope}): {r.description}" for r in item.wording_rules],
        "text": {"fields": item.fields, "sections": item.sections, "metrics": item.metrics, "notes": item.notes},
    })
    return entry


def build_tailor_input(run_id: str, job: Job, keywords: Keywords, ctx: Context, output_file: str) -> dict[str, Any]:
    scores = {s.id: (s.score, s.tech) for s in keywords.item_scores}
    projects = sorted(ctx.master.projects, key=lambda p: (-scores.get(p.id, (0, []))[0], p.number or 0))
    return {
        "run_id": run_id,
        "write_to": output_file,
        "then_run": f"helper continue {run_id}",
        "rules": rules(ctx.run),
        "run": ctx.run.model_dump(),
        "job": {
            "company": job.company,
            "role_title": job.role_title,
            "url": job.url,
            "location": job.location,
            "description_text": job.description_text,
        },
        "keywords": keywords.model_dump(),
        "role_types": {rid: rt.model_dump() for rid, rt in ctx.project_sets.role_types.items()},
        "experience": [_item(i, ctx, scores) for i in ctx.master.experience],
        "projects": [_item(i, ctx, scores) for i in projects],
        "skills": [
            {"group": g.name, "items": [i.name for i in g.all_items()], "notes": g.notes} for g in ctx.master.skills
        ],
        "approved_skill_synonyms": ctx.synonyms.approved_terms(),
        "approved_tech_families": [f.model_dump() for f in ctx.families.approved()],
        "courses": ctx.courses,
        "output_schema": OUTPUT_SCHEMA,
    }
