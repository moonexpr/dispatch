#!/usr/bin/env python3
"""approval.py — ARCHITECT work-approval (PLAY.md Act II; issue #7 "work approval").

Pure and deterministic. Turns a classified job into an *Authorization*: the
budget, route, branch, session id, scope constraint, stop condition, and
per-phase token allocations the work order will carry. ADMIN holds the purse;
this module is the arithmetic of the authorization handshake.

No network, no model, no filesystem, no wall-clock — identical input always
yields identical output (the same determinism contract as classify.py).
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tuning  # noqa: E402

# scope -> total token budget ADMIN authorizes for the job.
# Tunable via services/tuning.json (generation.approval.scope_budget).
_SCOPE_BUDGET = tuning.SCOPE_BUDGET

# Fraction of the budget allocated per phase. The remainder (~0.20) is held as
# contingency reserve — PLAY.md Act II: "hold the reserve, do not pre-spend it."
# Tunable via services/tuning.json (generation.approval.phase_split).
_PHASE_SPLIT = tuning.PHASE_SPLIT


@dataclass(frozen=True)
class Authorization:
    session_id: str
    branch: str
    route: str
    scope: str
    dry_run: bool
    budget_tokens: int
    reserve_tokens: int
    phase_budgets: Dict[str, int]
    constraint: str
    stop_condition: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _constraint_for(triage: Dict[str, Any]) -> str:
    base = ("The job is bounded by the issue's stated scope. Adjacent work is "
            "not authorized — note it in the PR thread and stop. One issue, one "
            "branch, one PR.")
    action = triage.get("action", "implement")
    if action != "implement":
        return f"Classified action is '{action}', not 'implement'. " + base
    return base


def approve(
    job: Dict[str, Any],
    triage: Dict[str, Any],
    *,
    dry_run: bool = True,
    session_id: str = "",
) -> Authorization:
    """Authorize a classified job. Deterministic; no side effects."""
    issue = int(job["issue"])
    scope = triage.get("scope", job.get("scope", "m"))
    route = triage.get("route", job.get("route", "gen-default"))
    total = _SCOPE_BUDGET.get(scope, _SCOPE_BUDGET["m"])

    phase_budgets = {name: int(total * frac) for name, frac in _PHASE_SPLIT}
    reserve = total - sum(phase_budgets.values())

    return Authorization(
        session_id=session_id or f"dispatch-issue-{issue}",
        branch=f"pipeline/issue-{issue}",
        route=route,
        scope=scope,
        dry_run=dry_run,
        budget_tokens=total,
        reserve_tokens=reserve,
        phase_budgets=phase_budgets,
        constraint=_constraint_for(triage),
        stop_condition="PR open · do not push to main · do not merge",
    )
