#!/usr/bin/env python3
"""decompose.py — ARCHITECT work decomposition + staffing (deterministic).

Splits a job into UNITS OF WORK — one cohesive, independently-verifiable
deliverable per unit, owned end-to-end by a single specialist — and staffs them.
Each specialist is named as `<domain> <function>` (operator's taxonomy):
the function is the role noun (engineer, writer, designer, analyst…) and the
domain qualifies it (developer tooling, data-pipeline, QA / test automation…).

Agent count = number of units, capped at the swarm maximum. Units with no
unmet dependency run in parallel; the integration/verification unit runs last.
Offline + deterministic: identical inputs -> identical plan.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

_SWARM_MAX = 15


def _spec_for(path: str):
    """Map a file/area to a (function, domain) specialization."""
    p = path.lower()
    base = os.path.basename(p)
    if "smoke" in base or "test" in p or "/fixtures/" in p:
        return ("engineer", "QA / test automation")
    if base.endswith(".sh") or p.startswith("scripts/"):
        return ("engineer", "developer tooling (shell)")
    if p.startswith("schemas/") or (base.endswith(".json") and "schema" in p):
        return ("engineer", "data contracts / schema")
    if p.startswith(".github/") or base.startswith("ci"):
        return ("engineer", "CI/CD")
    if base.endswith(".md") or base == "claude.md":
        return ("writer", "developer documentation")
    if p.startswith("services/intake/"):
        return ("engineer", "data-pipeline (Python)")
    if p.startswith("services/classifier/"):
        return ("engineer", "ML / classification (Python)")
    if p.startswith("services/models/"):
        return ("engineer", "LLM integration (Python)")
    if p.startswith("services/architect/"):
        return ("engineer", "orchestration (Python)")
    if base.endswith(".py") or p.startswith("services/"):
        return ("engineer", "backend (Python)")
    return ("engineer", "general software")


def _spec(function: str, domain: str) -> Dict[str, str]:
    return {"function": function, "domain": domain, "label": f"{domain} {function}"}


def plan(job: Dict[str, Any], discovered: List[str],
         *, verify_cmd: str = "bash scripts/smoke.sh") -> Dict[str, Any]:
    issue = job.get("issue")
    units: List[Dict[str, Any]] = []

    # One implementation unit per referenced file: a cohesive deliverable.
    for i, path in enumerate(discovered):
        fn, domain = _spec_for(path)
        units.append({
            "id": chr(ord("A") + i),
            "deliverable": f"Implement the change in `{path}` per the issue's acceptance criteria.",
            "files": [path],
            "specialization": _spec(fn, domain),
            "depends_on": [],
            "acceptance": f"`{path}` is correct in isolation and `{verify_cmd}` stays green.",
        })

    if not units:
        units.append({
            "id": "A",
            "deliverable": f"Implement issue #{issue} per its acceptance criteria.",
            "files": [],
            "specialization": _spec("engineer", "general software"),
            "depends_on": [],
            "acceptance": f"`{verify_cmd}` stays green and the issue's criteria are met.",
        })

    # Always-present integration + verification unit; depends on every impl unit.
    impl_ids = [u["id"] for u in units]
    verify_id = chr(ord("A") + len(units))
    units.append({
        "id": verify_id,
        "deliverable": f"Integrate the units, run `{verify_cmd}`, and open the PR with `Closes #{issue}`.",
        "files": ["scripts/smoke.sh"],
        "specialization": _spec("engineer", "QA / test automation"),
        "depends_on": impl_ids,
        "acceptance": f"`{verify_cmd}` exits 0 (0 FAIL); PR opened against `main`, not merged.",
    })

    capped = len(units) > _SWARM_MAX
    if capped:
        units = units[:_SWARM_MAX]

    staffing = {
        "agent_count": len(units),
        "swarm_max": _SWARM_MAX,
        "capped": capped,
        "parallel": [u["id"] for u in units if not u["depends_on"]],
        "sequential_tail": [u["id"] for u in units if u["depends_on"]],
        "assignments": [
            {"unit": u["id"], "specialist": u["specialization"]["label"]} for u in units
        ],
    }
    return {"units": units, "staffing": staffing}
