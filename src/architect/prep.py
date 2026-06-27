#!/usr/bin/env python3
"""prep.py — ARCHITECT harness-prep planner (issue #102; the `prep` stage).

The `prep` stage sits between work-order emission and engineer dispatch
(``intake → workorder → [prep] → engineer → …``). Its job is to PROVISION the
*declared* harness a work plan needs BEFORE the job is issued, so a half-prepped
harness never reaches the Engineer. This module is the deterministic PLANNER for
that stage: given a job (and optional labels / decomposition plan) it derives the
complete PREP PLAN — what harness to install, how to orchestrate, and which
branch/worktree to ready — as one JSON object the shell stage
(``scripts/lib/common.sh::prep_stage``) records, dumps, and acts on.

It mirrors purpose.py / strategy.py / characteristics.py: pure orchestration over
the data-driven rules in ``workplan-rules.yml`` (purpose→harness, strategy→layout,
plus a ``prep`` section for the provisioner allowlist + branch/worktree
conventions). It adds NO editorial content and executes NOTHING — the planner is
always side-effect-free, which is what makes the stage's dry-run path trivially
safe and its output reproducible.

Three things the plan answers, one per #102 case family:
  * HARNESS    — purpose → harness + its data-driven ``setup`` (the commands the
                 Engineer runs to engage it). A no-op harness (empty setup) needs
                 no provisioning; a real one (e.g. ponytail) carries its steps.
  * ORCHESTRATION — strategy → swarm (N-agent coordinator) / dynamic-workflow
                 (probe→replan) / sequential (no-op single agent).
  * ISSUES AFFECTED — the branch + worktree to ready, for a single issue or a
                 multi-issue aggregate (administrative; same branch resolves all).

FAIL-SAFE (#102): a harness whose provisioner is not in the data-driven
``prep.known_provisioners`` allowlist is marked ``provisionable: false`` with a
``block_reason``; the shell stage refuses to issue the job (it never half-preps).

Pure and deterministic: same (job, labels, plan) in → same plan out. No network,
no model, no wall-clock.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import characteristics as _characteristics  # noqa: E402
import workplan_config                      # noqa: E402

# Defaults if the YAML omits a `prep` section (kept here as structural fallback
# only — the real values live in workplan-rules.yml, like every other rule).
_DEFAULT_KNOWN_PROVISIONERS = ["none", "marketplace", "skill"]
_DEFAULT_BRANCH_PREFIX = "pipeline/issue-"
_DEFAULT_WORKTREE_ROOT = ".worktrees"


def _setup_steps(setup: str) -> List[str]:
    """Split a harness ``setup`` string into ordered, individually-runnable steps.
    Steps are joined with ``&&`` in the YAML (shell-style); whitespace-only parts
    are dropped. An empty/absent setup yields no steps (a no-op harness)."""
    return [s.strip() for s in (setup or "").split("&&") if s.strip()]


def _provisioner_for(harness_entry: Dict[str, Any], has_setup: bool) -> str:
    """The provisioner kind for a harness: explicit ``provision:`` on the harness
    entry wins; otherwise inferred FROM SETUP CONTENT — a setup that drives a
    ``/plugin`` install/marketplace command is a ``marketplace`` provision; a
    harness with no setup is a ``none`` (no-op). A non-empty setup with no
    recognizable provisioner signal returns ``"unknown"`` so it FAILS SAFE at the
    allowlist gate rather than being mis-tagged ``marketplace`` and issued."""
    explicit = str(harness_entry.get("provision") or "").strip().lower()
    if explicit:
        return explicit
    if not has_setup:
        return "none"
    setup = str(harness_entry.get("setup") or "")
    if re.search(r"/plugin\s+(?:install|marketplace)\b", setup):
        return "marketplace"
    return "unknown"


def _orchestration(strategy_rec: Dict[str, Any],
                   plan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map the resolved strategy to a concrete orchestration directive.

      * swarm           → provision an N-agent coordinator (N from STAFFING).
      * dynamic-workflow→ arm the probe → replan loop.
      * sequential      → no-op (a single agent, no fan-out).
    """
    strategy = strategy_rec.get("strategy", "sequential")
    summary = str(strategy_rec.get("summary", ""))
    staffing = ((plan or {}).get("staffing") or {})
    # Prefer the decomposition's explicit agent_count; fall back to the count of
    # independent (parallel) units the strategy resolver saw, then to 1.
    n_agents = staffing.get("agent_count")
    try:
        n_agents = int(n_agents)
    except (TypeError, ValueError):
        n_agents = int(strategy_rec.get("n_parallel") or 0)
    if n_agents < 1:
        n_agents = 1   # a swarm always has >=1 agent; floor a degenerate/zero count
    parallel_units = list(strategy_rec.get("parallel_units")
                          or staffing.get("parallel") or [])

    if strategy == "swarm":
        return {
            "kind": "swarm",
            "n_agents": n_agents,
            "coordinator": f"{n_agents}-agent swarm coordinator",
            "parallel_units": parallel_units,
            "summary": summary,
        }
    if strategy == "dynamic-workflow":
        return {
            "kind": "dynamic-workflow",
            "probe": True,
            "replan": True,
            "summary": summary,
        }
    return {"kind": "none", "n_agents": 1, "summary": summary}


def build(job: Dict[str, Any],
          labels: Optional[List[str]] = None,
          plan: Optional[Dict[str, Any]] = None, *,
          resolves: Optional[List[int]] = None,
          dag: Optional[Dict[str, Any]] = None,
          branch_prefix: Optional[str] = None,
          worktree_root: Optional[str] = None) -> Dict[str, Any]:
    """Return the deterministic PREP PLAN for one job.

    The plan is data only — it provisions nothing. ``provisionable`` is the
    fail-safe gate: when false (an unknown provisioner), the shell stage blocks
    issuance and ``block_reason`` says why.
    """
    rules = workplan_config.load()
    prep_cfg = (rules.get("prep") or {})
    known = [str(p).strip().lower()
             for p in (prep_cfg.get("known_provisioners") or _DEFAULT_KNOWN_PROVISIONERS)]
    branch_prefix = (branch_prefix if branch_prefix is not None
                     else str(prep_cfg.get("branch_prefix") or _DEFAULT_BRANCH_PREFIX))
    worktree_root = (worktree_root if worktree_root is not None
                     else str(prep_cfg.get("worktree_root") or _DEFAULT_WORKTREE_ROOT))

    # The three load-bearing characteristics (purpose→harness, strategy→layout,
    # issues_affected) — computed exactly as the work order surfaces them.
    ch = _characteristics.build(job, {}, plan, labels=labels, dag=dag, resolves=resolves)
    pur = ch.get("purpose", {}) or {}
    strat = ch.get("strategy", {}) or {}
    iss = ch.get("issues_affected", {}) or {}

    purpose_name = str(pur.get("purpose", "new-feature"))
    harness = str(pur.get("harness", "standard"))
    setup = str(pur.get("harness_setup", "") or "")
    steps_setup = _setup_steps(setup)

    # Provisionability is data-driven: look the purpose's harness entry up in the
    # rules to honour an explicit `provision:`, else infer it, then gate on the
    # `prep.known_provisioners` allowlist. An unknown provisioner FAILS SAFE.
    harness_entry = (((rules.get("purpose") or {}).get("harness") or {})
                     .get(purpose_name) or {})
    provisioner = _provisioner_for(harness_entry, bool(steps_setup))
    provisionable = provisioner in known
    block_reason = ("" if provisionable else
                    f"harness '{harness}' uses an unknown provisioner "
                    f"'{provisioner}' (known: {', '.join(known)})")

    # A `none` provisioner is a no-op: prep provisions nothing for it, even if the
    # harness happens to carry a stray `setup` string (that is the Engineer's to
    # run, surfaced in the work order — not prep's to provision). Keep the raw
    # harness_setup for reference, but the steps prep WOULD provision are empty.
    is_noop = (provisioner == "none")
    provision_steps = [] if is_noop else steps_setup

    orchestration = _orchestration(strat, plan)

    # Branch / worktree to ready. A multi-issue aggregate resolves several issues
    # on ONE branch (the primary's) — issues_affected is administrative only.
    primary = job.get("issue")
    try:
        primary = int(primary)
    except (TypeError, ValueError):
        primary = None
    resolved = iss.get("resolves") or ([primary] if primary is not None else [])
    if primary is not None:
        branch = f"{branch_prefix}{primary}"
        worktree = f"{worktree_root}/issue-{primary}"
    else:
        branch = ""
        worktree = ""

    # Intended steps, human-readable — what the stage WOULD do, printed verbatim
    # on the dry-run path (side-effect-free) and logged on the live path.
    steps: List[str] = []
    if branch:
        if iss.get("multi_issue"):
            agg = ", ".join(f"#{n}" for n in resolved)
            steps.append(f"ready branch {branch} + worktree {worktree} "
                         f"for the aggregate resolving {agg}")
        else:
            steps.append(f"ready branch {branch} + worktree {worktree}")
    if not provisionable:
        steps.append(f"BLOCK issuance — {block_reason}")
    elif provision_steps:
        steps.append(f"provision harness '{harness}' ({provisioner}): "
                     + " && ".join(provision_steps))
    else:
        steps.append(f"harness '{harness}' is a no-op (nothing to provision)")
    if orchestration["kind"] == "swarm":
        steps.append(f"arm swarm orchestration: {orchestration['coordinator']}")
    elif orchestration["kind"] == "dynamic-workflow":
        steps.append("arm dynamic-workflow orchestration: probe → replan loop")
    else:
        steps.append("orchestration: sequential (single agent, no fan-out)")

    return {
        "issue": primary,
        "purpose": purpose_name,
        "purpose_source": str(pur.get("source", "default")),
        "harness": harness,
        "harness_ref": str(pur.get("harness_ref", "")),
        "harness_setup": setup,
        "harness_steps": provision_steps,
        "provisioner": provisioner,
        "provisionable": provisionable,
        "block_reason": block_reason,
        "strategy": str(strat.get("strategy", "sequential")),
        "orchestration": orchestration,
        "issues_affected": iss,
        "branch": branch,
        "worktree": worktree,
        "steps": steps,
    }


def main(argv: List[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Plan the harness-prep stage for one job.")
    p.add_argument("--job-json", required=True,
                   help="job dict JSON (issue/title/body/scope/route) — the Job Request")
    p.add_argument("--plan-json", default=None, help="decompose.plan() output JSON (optional)")
    p.add_argument("--labels", default="", help="comma-separated issue labels")
    p.add_argument("--resolves", default="", help="comma-separated sibling issue ids (aggregate)")
    p.add_argument("--branch-prefix", default=None, help="override branch prefix")
    p.add_argument("--worktree-root", default=None, help="override worktree root")
    args = p.parse_args(argv)
    with open(args.job_json, encoding="utf-8") as fh:
        job = json.load(fh)
    plan = None
    if args.plan_json:
        with open(args.plan_json, encoding="utf-8") as fh:
            plan = json.load(fh)
    labels = [s for s in args.labels.split(",") if s]
    resolves = []
    for _tok in args.resolves.split(","):
        _tok = _tok.strip()
        if not _tok:
            continue
        try:
            resolves.append(int(_tok))
        except ValueError:
            pass  # degrade like the internal dedupe — ignore non-integer tokens
    out = build(job, labels, plan, resolves=resolves,
                branch_prefix=args.branch_prefix, worktree_root=args.worktree_root)
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
