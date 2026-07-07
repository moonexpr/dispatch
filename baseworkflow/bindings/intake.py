#!/usr/bin/env python3
"""bindings/intake — the ``intake:`` namespace: the intake dossier.

The intake phase turns the accepted request (seed's work item + the classified
purpose) into a durable **intake dossier** (``deliverables.intake``) — the
single document the Administrator questions, the research loop enriches, and
the Architect specs against. Two bodies:

  * ``intake:compose_dossier`` — consolidate job / triage / request / purpose
    into the dossier (runs once, in the ``intake`` phase);
  * ``intake:refine`` — the intake↔research "bounce": fold a research round's
    synthesized answers back into the dossier (runs inside the research loop).

Pure and interface-driven like every bindings module: the engine wires the
declared shelf I/O; ``compose_dossier`` additionally reads the seed variant's
dynamic ``work_item`` via ctx (an optional, runtime-only enrichment — the seed
variants are supersede targets, so their outputs are not part of the static
phase data-flow).
"""
from __future__ import annotations

from typing import Any, Dict, List


def compose_dossier(inputs: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """Consolidate the accepted request into the intake dossier."""
    job = inputs.get("job") or {}
    triage = inputs.get("triage") or {}
    request = inputs.get("request") or {}
    purpose = inputs.get("purpose") or {}
    work_unit = inputs.get("work_unit") or {}
    # The seed variant's normalized work item (dynamic supersede output) — an
    # enrichment when present, never a static dependency.
    work_item = ctx.shelves.shared.get("work_item") or {}
    intake = {
        "issue": job.get("issue"),
        "title": work_item.get("title") or job.get("title", ""),
        "goal": work_item.get("goal") or job.get("body", ""),
        "acceptance": work_item.get("acceptance", ""),
        "labels": list(work_item.get("labels") or job.get("labels") or ()),
        "source": request.get("source") or work_item.get("source") or "",
        # The TARGET repo (owner/repo) — the tree the FileResearcher grounds in.
        "repo": request.get("repo") or job.get("repo") or "",
        "purpose": purpose,
        "work_unit": work_unit,
        "triage": triage,
        "open_needs": list(work_item.get("open_needs") or ()),
        # Filled by the intake↔research bounce (intake:refine), round by round.
        "research_context": {},
        "rounds": [],
    }
    return {"intake": intake}


def refine(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """The bounce: fold the research round's synthesized answers back into the
    dossier, appending a per-round summary so the enrichment is auditable."""
    intake = dict(inputs.get("intake") or {})
    research = inputs.get("research") or {}
    answers = dict(research.get("answers") or {})
    merged = dict(intake.get("research_context") or {})
    new_keys = [k for k in answers if answers.get(k) and not merged.get(k)]
    merged.update({k: v for k, v in answers.items() if v})
    rounds: List[Dict[str, Any]] = list(intake.get("rounds") or ())
    rounds.append({
        "round": int(research.get("rounds") or len(rounds) + 1),
        "answered": new_keys,
        "findings": len(research.get("findings") or ()),
    })
    intake["research_context"] = merged
    intake["rounds"] = rounds
    return {"intake": intake}


def register(reg: Any) -> None:
    reg.register_action("intake_compose_dossier", compose_dossier, needs_ctx=True)
    reg.register_action("intake_refine", refine)
