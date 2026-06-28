#!/usr/bin/env python3
"""bindings/budget — budget-arithmetic tokens (the budget: namespace).

Composes the ``approval`` subsystem (the authorization handshake) into the
``budget:compute`` body. Needs the run Context for the dry-run flag.
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


def register(reg: Any) -> None:
    reg.register_action("compute_budget", compute_budget, needs_ctx=True)
