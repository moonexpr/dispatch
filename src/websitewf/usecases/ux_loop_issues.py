#!/usr/bin/env python3
"""ux_loop_issues.py — WebsiteWF use-case bindings for "create new GitHub issues
from UX loops / website use cases" (#166).

Disjoint from the proof vertical's src/websitewf/bindings.py. build_registry()
inherits baseworkflow's fully-populated TokenRegistry and registers only THIS use
case's web:* bodies on top. Bodies are pure fn(inputs)->{out_alias: value},
deterministic (no model, no network) so they run identically under mock and real.

The deliverable is DATA only: a set of proposed GitHub issue specs (work-order
shape title/body/acceptance/labels). These bodies NEVER call `gh` — they only write
to a shelf; the admin phase (downstream) is what would file anything.

Validate:
  python3 -m engine.workflow app/workflows/websitewf-ux-loop-issues.yml \
    --registry src.websitewf.usecases.ux_loop_issues:build_registry
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

# Repo root + src/ on sys.path so src.* and engine.* resolve regardless of import path.
_HERE = os.path.dirname(os.path.abspath(__file__))            # .../src/websitewf/usecases
_WEBSITEWF = os.path.dirname(_HERE)                           # .../src/websitewf
_SRC = os.path.dirname(_WEBSITEWF)                            # .../src
_ROOT = os.path.dirname(_SRC)                                 # repo root
for _p in (_ROOT, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine.workflow import TokenRegistry  # noqa: E402


def _field(job: Any, key: str, default: Any = "") -> Any:
    return job.get(key, default) if isinstance(job, dict) else default


def _propose_issues(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Derive well-formed proposed GitHub issues from the job's UX-loop / use-case
    inputs, deterministically. Each issue carries the work-order shape:
    {title, body, acceptance, labels}. DATA only — no `gh` call."""
    # UX-loop inputs may arrive as a list of use cases / loop steps under one of
    # several keys; fall back to the single job title as one use case.
    use_cases = (
        _field(job, "use_cases", None)
        or _field(job, "ux_loops", None)
        or _field(job, "loops", None)
    )
    if not isinstance(use_cases, list) or not use_cases:
        use_cases = [{"name": _field(job, "title", "Website use case")}]

    base_labels = list(_field(job, "labels", []) or [])
    issues: List[Dict[str, Any]] = []
    for uc in use_cases:
        if isinstance(uc, dict):
            name = uc.get("name") or uc.get("title") or "Website use case"
            goal = uc.get("goal") or uc.get("outcome") or name
            steps = uc.get("steps") or []
        else:
            name = str(uc)
            goal = name
            steps = []
        title = f"Implement use case: {name}"
        body_lines = [f"Derived from UX loop / website use case: {name}.", "", f"Goal: {goal}."]
        if steps:
            body_lines.append("")
            body_lines.append("UX-loop steps:")
            body_lines.extend(f"- {s}" for s in steps)
        issues.append({
            "title": title,
            "body": "\n".join(body_lines),
            "acceptance": [
                f"The use case '{name}' is reachable in the website.",
                f"The flow satisfies the goal: {goal}.",
            ],
            "labels": sorted(set(base_labels + ["enhancement", "ux-loop"])),
        })
    return issues


# -- web action bodies (pure: inputs -> {out_alias: value}) ------------------

def generate_issue_work_units(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A1 (web): the base work-unit classification *plus* a set of authored, proposed
    GitHub ISSUES (work-order shape) emitted as DATA.

    Composes baseworkflow's ``generate_work_units`` body (so ``purpose`` /
    ``work_unit`` keep the exact shape downstream base actions expect) and adds
    ``proposed_issues``. DATA only — never calls `gh`."""
    from src.baseworkflow.bindings.github import generate_work_units as _base

    out = dict(_base(inputs))
    job = inputs.get("job") or {}
    out["proposed_issues"] = _propose_issues(job)
    return out


def classify_uxloop_addendum(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A2 (web addendum): recognize UX-loop / use-case inputs and tag the signal onto
    the strategy the base classifier produced (augment, preserving the base output)."""
    strategy = inputs.get("strategy") or {}
    proposed = inputs.get("proposed_issues") or []
    out = dict(strategy) if isinstance(strategy, dict) else {"value": strategy}
    out["input_kind"] = "ux-loop" if proposed else out.get("input_kind", "generic")
    out["proposed_issue_count"] = len(proposed) if isinstance(proposed, list) else 0
    return {"strategy": out}


def register(reg: TokenRegistry) -> None:
    """Register THIS use case's web:* bodies onto an existing registry.
    The bind name (1st arg) MUST equal the manifest's `bind:` field."""
    reg.register_action("generate_issue_work_units", generate_issue_work_units)
    reg.register_action("classify_uxloop_addendum", classify_uxloop_addendum)


def build_registry() -> TokenRegistry:
    """Inherit every baseworkflow bind, plus THIS use case's web:* bodies."""
    from src.baseworkflow.bindings import build_registry as _base_build_registry

    reg = _base_build_registry()
    register(reg)
    return reg
