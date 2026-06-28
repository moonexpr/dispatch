#!/usr/bin/env python3
"""characteristics.py — assemble the three work-plan characteristics.

A work plan must clearly identify three things; this module computes them as one
bundle the renderer (workorder.py) consumes:

  * strategy        — sequential | swarm | dynamic-workflow  (drives LAYOUT).
  * purpose         — prototype | refactor | new-feature | research | test | …
                      (drives the engineering HARNESS used in execution).
  * issues_affected — the set of issue ids this plan resolves together, plus its
                      DAG neighbours (purely ADMINISTRATIVE traceability).

The data/rules live in app/config/workplan-rules.yml (see workplan_config). This
module is pure orchestration over purpose.py + strategy.py + the DAG; it adds no
rules of its own. Deterministic: identical inputs -> identical bundle.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import purpose as _purpose      # noqa: E402
import strategy as _strategy    # noqa: E402
import workplan_config          # noqa: E402


def _dedupe_ints(seq) -> List[int]:
    seen, out = set(), []
    for v in seq or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _issues_affected(primary: int, resolves: Optional[List[int]],
                     dag: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The administrative issue-id set: which issues this plan resolves together,
    plus DAG neighbours (blocked_by / blocks) when configured."""
    cfg = (workplan_config.load().get("issues_affected", {}) or {})
    resolved = _dedupe_ints([primary] + list(resolves or []))
    if primary is not None and primary not in resolved:
        resolved = _dedupe_ints([primary]) + resolved

    blocked_by: List[int] = []
    blocks: List[int] = []
    if cfg.get("include_dag_neighbours", True) and dag:
        blocked_by = _dedupe_ints(n for n, _url in (dag.get("blocked_by") or []))
        blocks = _dedupe_ints(n for n, _url in (dag.get("blocks") or []))
    return {"resolves": resolved, "blocked_by": blocked_by, "blocks": blocks,
            "multi_issue": len(resolved) > 1}


def build(job: Dict[str, Any], triage: Dict[str, Any],
          plan: Optional[Dict[str, Any]], *,
          labels: Optional[List[str]] = None,
          dag: Optional[Dict[str, Any]] = None,
          resolves: Optional[List[int]] = None) -> Dict[str, Any]:
    """Return {purpose, strategy, issues_affected} for one work plan.

    ``resolves`` is the (optional) set of SIBLING issue ids this plan resolves
    alongside the primary — the multi-issue aggregate. Defaults to just the
    primary issue.
    """
    pur = _purpose.classify_purpose(job, labels)
    strat = _strategy.resolve_strategy(plan, pur["purpose"])
    primary = job.get("issue")
    try:
        primary = int(primary)
    except (TypeError, ValueError):
        primary = None
    issues = _issues_affected(primary, resolves, dag)
    return {"purpose": pur, "strategy": strat, "issues_affected": issues}


def main(argv: List[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Assemble work-plan characteristics.")
    p.add_argument("--job-json", required=True, help="job dict JSON (issue/title/body/scope/route)")
    p.add_argument("--plan-json", default=None, help="decompose.plan() output JSON")
    p.add_argument("--labels", default="", help="comma-separated issue labels")
    p.add_argument("--resolves", default="", help="comma-separated sibling issue ids")
    args = p.parse_args(argv)
    job = json.load(open(args.job_json, encoding="utf-8"))
    plan = json.load(open(args.plan_json, encoding="utf-8")) if args.plan_json else None
    labels = [s for s in args.labels.split(",") if s]
    resolves = [int(s) for s in args.resolves.split(",") if s.strip()]
    print(json.dumps(build(job, {}, plan, labels=labels, resolves=resolves), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
