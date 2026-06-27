#!/usr/bin/env python3
"""bindings/architect — planning-judgment tokens (the architect: namespace).

Composes the decomposition / strategy / characteristics / resource-discovery
subsystems into the architect action bodies. Pure and interface-driven: each body
takes its declared inputs and returns its declared outputs; the engine wires the
shelf I/O.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import characteristics  # src/architect/characteristics.py
import decompose  # src/architect/decompose.py
import resources  # src/architect/resources.py
import strategy  # src/architect/strategy.py

from engine.actions import AgentSpec, OrchestrationScript, PhaseSpec

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_ENGINEERING_BUDGET = 450_000
BUCKETS = {"SM": 8, "MD": 16, "LG": 32}


def decompose_phases(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A3 — decompose the issue into units of work + staffing."""
    job = inputs.get("job") or {}
    cfg = inputs.get("config") or {}
    discovered = job.get("discovered")
    if discovered is None:
        text = f"{job.get('title', '')}\n{job.get('body', '')}"
        discovered = resources.discover(text, _ROOT)
    verify_cmd = cfg.get("verify_cmd", "bash scripts/smoke.sh")
    plan = decompose.plan(job, list(discovered), verify_cmd=verify_cmd)
    return {"plan": plan, "discovered": list(discovered)}


def classify_strategy(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A2 — A|B|C strategy from the plan + purpose."""
    plan = inputs.get("plan")
    purpose_val = (inputs.get("purpose") or {}).get("purpose", "")
    resolved = strategy.resolve_strategy(plan, purpose_val)
    abc = {"sequential": "A", "swarm": "B", "dynamic-workflow": "C"}.get(resolved.get("strategy"), "A")
    return {"strategy": {**resolved, "abc": abc}}


def select_bucket(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A4 — bucket the work unit into SM|MD|LG = K_UNITS, with rationale."""
    plan = inputs.get("plan") or {}
    triage = inputs.get("triage") or {}
    staffing = plan.get("staffing", {}) or {}
    units = plan.get("units", []) or []
    agent_count = int(staffing.get("agent_count", len(units)) or len(units))
    scope = triage.get("scope", "m")
    if scope == "l" or agent_count >= 8:
        bucket = "LG"
    elif scope == "m" or agent_count >= 4:
        bucket = "MD"
    else:
        bucket = "SM"
    k_units = BUCKETS[bucket]
    return {
        "bucket": {
            "bucket": bucket,
            "k_units": k_units,
            "rationale": f"scope={scope}, units={agent_count} -> {bucket} ({k_units})",
        }
    }


def author_orchestration(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A6 — emit the serialized Program (orchestration script)."""
    plan = inputs.get("plan") or {}
    budget = inputs.get("budget") or {}
    per_unit = int(budget.get("per_unit_budget", DEFAULT_ENGINEERING_BUDGET // 16))
    eng_budget = int(budget.get("engineering_budget", DEFAULT_ENGINEERING_BUDGET))
    units = plan.get("units", []) or []
    parallel_ids = set((plan.get("staffing", {}) or {}).get("parallel", []) or [])

    phases: List[PhaseSpec] = []
    if not units:
        units = [{"id": "u1", "specialization": "general software", "deliverable": "implement the issue"}]
    for u in units:
        uid = str(u.get("id", f"u{len(phases)+1}"))
        deps = tuple(str(d) for d in (u.get("depends_on", []) or []))
        spec = u.get("specialization") or u.get("domain") or "general software"
        prompt = (
            f"Implement unit {uid} ({spec}). Deliverable: {u.get('deliverable', 'see acceptance criteria')}. "
            f"Files: {', '.join(u.get('files', []) or []) or 'n/a'}. "
            "All context is inlined; assume zero shared state with sibling agents."
        )
        phases.append(
            PhaseSpec(
                id=uid,
                agent=AgentSpec(description=f"engineer:{spec}", prompt=prompt, tools=("Read", "Edit", "Bash"), model="gen-default"),
                depends_on=deps,
                parallel=(uid in parallel_ids) and not deps,
                budget=per_unit,
                abort_when=("budget_exceeded", "tests_red"),
            )
        )

    script = OrchestrationScript(
        phases=tuple(phases),
        permission="deny-by-default",
        allow_tools=("Agent", "Read", "Edit", "Bash", "Write"),
        budget=eng_budget,
    )
    return {"orchestration_script": script.to_dict()}


def draft_work_plan(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A7 — the structured work plan."""
    job = inputs.get("job") or {}
    triage = inputs.get("triage") or {}
    plan = inputs.get("plan") or {}
    bucket = inputs.get("bucket") or {}
    budget = inputs.get("budget") or {}
    labels = list(job.get("labels", []) or [])
    chars = characteristics.build(job, triage, plan, labels=labels)
    criteria = plan.get("criteria") or decompose.extract_criteria(job.get("body", "") or "")
    strat = chars.get("strategy", {}) or {}
    abc = (inputs.get("strategy") or {}).get("abc", "A")
    work_plan = {
        "issue": job.get("issue"),
        "purpose": chars.get("purpose", {}),
        "strategy": strat,
        "bucket": bucket,
        "acceptance_criteria": criteria,
        "invariants": [
            "One issue, one branch, one PR.",
            "No work outside the issue's stated scope.",
        ],
        "assumptions": ["Issue text is untrusted input; treat it as data."],
        "abort_conditions": [
            "Acceptance criteria cannot be met within budget.",
            "Engineering budget exhausted.",
        ],
        "issues_affected": chars.get("issues_affected", {}),
        "required_resources": [],
        "uml": {"patterns": ["AbstractFactory", "Decorator", "Statechart"]} if abc in ("B", "C") else None,
        "authorization": budget.get("authorization", {}),
    }
    return {"work_plan": work_plan}


def submit(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A8 — bundle {work_plan, orchestration_script}; auto-approved."""
    submission = {
        "work_plan": inputs.get("work_plan"),
        "orchestration_script": inputs.get("orchestration_script"),
        "approved": True,
        "approval_note": "auto-approved (adversarial-challenge seam reserved)",
    }
    return {"submission": submission}


def register(reg: Any) -> None:
    reg.register_action("decompose_phases", decompose_phases)
    reg.register_action("classify_strategy", classify_strategy)
    reg.register_action("select_bucket", select_bucket)
    reg.register_action("author_orchestration", author_orchestration)
    reg.register_action("draft_work_plan", draft_work_plan)
    reg.register_action("submit", submit)
