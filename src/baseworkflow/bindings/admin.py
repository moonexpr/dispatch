#!/usr/bin/env python3
"""bindings/admin — administrator tokens (the admin: namespace).

Env prep, adversarial tests, doc update, and persistence. Composes the ``prep``
subsystem; the rest are deterministic assembly. ``store`` needs the run Context
(trace length for analytics).
"""
from __future__ import annotations

from typing import Any, Dict

import prep  # src/architect/prep.py


def prepare_env(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """M1 — prepare the work environment (worktree/harness plan)."""
    job = inputs.get("job") or {}
    plan = inputs.get("plan")
    labels = list(job.get("labels", []) or [])
    env = prep.build(job, labels, plan)
    return {"env": env, "prep": env}


def write_adversarial(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """M2 — author adversarial tests against the work plan's acceptance criteria."""
    work_plan = inputs.get("work_plan") or {}
    criteria = work_plan.get("acceptance_criteria") or ["the issue's acceptance criteria are met"]
    tests = [{"id": f"adv{i+1}", "asserts": c, "kind": "adversarial"} for i, c in enumerate(criteria)]
    return {"adversarial_tests": tests}


def update_docs(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """M6 — update online documentation on completion."""
    work_plan = inputs.get("work_plan") or {}
    docs = {
        "issue": work_plan.get("issue"),
        "summary": "work unit delivered; docs updated",
        "sections": ["work plan", "orchestration script", "acceptance criteria"],
    }
    return {"docs": docs}


def store(inputs: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """M7 — persist work plan, orchestration script, engineering result, analytics."""
    stored = {
        "work_plan": inputs.get("work_plan"),
        "orchestration_script": inputs.get("orchestration_script"),
        "engineering_result": inputs.get("engineering_result"),
        "analytics": {
            "budget": inputs.get("budget"),
            "trace_len": len(getattr(ctx, "trace", []) or []),
        },
    }
    return {"stored": stored}


def register(reg: Any) -> None:
    reg.register_action("prepare_env", prepare_env)
    reg.register_action("write_adversarial", write_adversarial)
    reg.register_action("update_docs", update_docs)
    reg.register_action("store", store, needs_ctx=True)
