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
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tuning  # noqa: E402

# Hard cap on agents (= number of units). Tunable via services/tuning.json
# (generation.decompose.swarm_max).
_SWARM_MAX = tuning.SWARM_MAX


def _spec_for(path: str):
    """Map a file/area to a (function, domain) specialization.

    The ordered match rules live in services/tuning.json
    (generation.decompose.specialization_rules) and are evaluated by
    tuning.spec_for — edit the config to retune staffing labels.
    """
    return tuning.spec_for(path)


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
