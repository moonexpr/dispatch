#!/usr/bin/env python3
"""strategy.py — derive the work-plan STRATEGY (drives the plan LAYOUT).

Strategy ∈ {sequential, swarm, dynamic-workflow} answers *how the work is laid
out*. It is DERIVED, not operator-set, from the decomposition DAG (how many
units have no unmet dependency) plus the issue's purpose. Resolution order
(data-driven, ``workplan-rules.yml`` ``strategy``):

  1. purpose ∈ ``dynamic_purposes``            -> dynamic-workflow
  2. parallel-unit count >= ``swarm_min_parallel_units`` -> swarm
  3. otherwise                                 -> sequential

When the resolved strategy's layout sets ``requires_parallelization_rationale``
(swarm), a concrete justification is built from the plan's independent units —
the swarm guardrail: a fan-out must be *earned*, never assumed.

Pure and deterministic: same (plan, purpose) in -> same record out.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import workplan_config  # noqa: E402


def _parallelization_rationale(plan: Dict[str, Any], parallel_ids: List[str]) -> str:
    """Justify a swarm fan-out from the plan's independent units: which units run
    concurrently (and on what disjoint surface), and what serialises after."""
    units = {u["id"]: u for u in (plan.get("units") or [])}
    par = [units[i] for i in parallel_ids if i in units]
    tail = [u for u in (plan.get("units") or []) if u.get("depends_on")]
    if len(par) < 2:
        return ""
    def _files(u: Dict[str, Any]) -> str:
        fs = u.get("files") or []
        return ", ".join(fs) if fs else "no fixed files"
    lines = [
        f"{len(par)} units have no unmet dependency and touch disjoint surfaces, "
        "so separate agents can build them concurrently without contending:",
    ]
    for u in par:
        lines.append(f"  • Unit {u['id']} ({u['specialization']['label']}) — {_files(u)}")
    if tail:
        names = ", ".join(u["id"] for u in tail)
        lines.append(f"Then unit(s) {names} integrate the parallel results and run the gate — "
                     "the one serial point.")
    return "\n".join(lines)


def resolve_strategy(plan: Optional[Dict[str, Any]], purpose: str) -> Dict[str, Any]:
    """Return {strategy, summary, requires_parallelization_rationale,
    parallelization_rationale, parallel_units, n_parallel}."""
    cfg = (workplan_config.load().get("strategy", {}) or {})
    dynamic = [str(p).lower() for p in (cfg.get("dynamic_purposes") or [])]
    swarm_min = int(cfg.get("swarm_min_parallel_units", 2) or 2)

    staffing = ((plan or {}).get("staffing") or {})
    parallel_ids = list(staffing.get("parallel") or [])
    n_parallel = len(parallel_ids)

    if str(purpose).lower() in dynamic:
        strategy = "dynamic-workflow"
    elif n_parallel >= swarm_min:
        strategy = "swarm"
    else:
        strategy = "sequential"

    layout = ((cfg.get("layouts") or {}).get(strategy) or {})
    needs_rationale = bool(layout.get("requires_parallelization_rationale"))
    rationale = _parallelization_rationale(plan or {}, parallel_ids) if needs_rationale else ""

    return {
        "strategy": strategy,
        "summary": str(layout.get("summary", "")),
        "requires_parallelization_rationale": needs_rationale,
        "parallelization_rationale": rationale,
        "parallel_units": parallel_ids,
        "n_parallel": n_parallel,
    }


def main(argv: List[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Derive work-plan strategy from a plan + purpose.")
    p.add_argument("--plan-json", required=True, help="decompose.plan() output as JSON")
    p.add_argument("--purpose", default="new-feature")
    args = p.parse_args(argv)
    with open(args.plan_json, encoding="utf-8") as fh:
        plan = json.load(fh)
    print(json.dumps(resolve_strategy(plan, args.purpose), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
