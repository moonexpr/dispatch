#!/usr/bin/env python3
"""bindings/budget — budget-arithmetic tokens (the budget: namespace).

Composes the ``approval`` subsystem (the authorization handshake) into the
``budget:compute`` body, and owns ALL budget allocation — including stamping budgets
onto the architect's orchestration script (``budget:scope_orchestration``). This is the
budget-scoping seam: ``architect:author_orchestration`` emits agent DEFINITIONS with no
budgets; this namespace allocates them, so agent definition and budget allocation never
touch the same function. Needs the run Context for the dry-run flag (compute only).
"""
from __future__ import annotations

from typing import Any, Dict

import approval  # src/baseworkflow/subsystems/approval.py

DEFAULT_ENGINEERING_BUDGET = 450_000


def compute_budget(inputs: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """A5 — per-unit budget = ENGINEERING_BUDGET / K_UNITS, plus the Authorization."""
    job = inputs.get("job") or {}
    triage = inputs.get("triage") or {}
    cfg = inputs.get("config") or {}
    bucket = inputs.get("bucket") or {}
    k_units = int(bucket.get("k_units", 16)) or 16
    auth = approval.approve(job, triage, dry_run=ctx.dry_run, session_id=str(job.get("issue", "")))
    eng = int(cfg.get("engineering_budget", DEFAULT_ENGINEERING_BUDGET))
    return {
        "budget": {
            "authorization": auth.to_dict(),
            "k_units": k_units,
            "engineering_budget": eng,
            "per_unit_budget": eng // k_units,
        }
    }


def scope_orchestration(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A6.5 — the budget-scoping seam: stamp the engineering bucket onto the orchestration
    script and the per-unit budget onto every phase. ``architect:author_orchestration``
    emits the script with ``budget=0`` everywhere (agent definitions only); this reads
    the ``budget`` deliverable (from ``budget:compute``) and fills the allocations in,
    keeping agent definition and budget allocation in separate functions/namespaces."""
    script = dict(inputs.get("orchestration_script") or {})
    budget = inputs.get("budget") or {}
    eng = int(budget.get("engineering_budget", DEFAULT_ENGINEERING_BUDGET))
    per_unit = int(budget.get("per_unit_budget", eng // 16))
    script["budget"] = eng
    script["phases"] = [{**p, "budget": per_unit} for p in (script.get("phases") or [])]
    return {"orchestration_script": script}


def register(reg: Any) -> None:
    reg.register_action("compute_budget", compute_budget, needs_ctx=True)
    reg.register_action("scope_orchestration", scope_orchestration)
