#!/usr/bin/env python3
"""bindings/admin — administrator tokens (the admin: namespace).

Env prep, adversarial tests, doc update, persistence, and the engineer-invoice
intake (the first GitHub-mutating action in the workflow engine). Composes the
``prep`` subsystem; the rest are deterministic assembly. ``store`` and
``intake_invoice`` need the run Context (``store`` for the trace length;
``intake_invoice`` for ``ctx.dry_run``, which gates every gh mutation).
"""
from __future__ import annotations

import json
from typing import Any, Dict

import prep  # src/architect/prep.py
import rescaffold  # src/architect/rescaffold.py — the shared #137 directive

# common carries the dry-run-aware gh wrapper + run-ledger (orchestration policy).
# bindings/__init__ puts src/orchestration on sys.path, so this is a flat import.
import common  # src/orchestration/common.py


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


# --------------------------------------------------------------------------- #
# M8 — intake the engineer invoice and drive the GitHub label state machine.    #
# Ported from the DEPRECATED src/orchestration/visitors.py::visit_intake_invoice #
# (the `completed`/`partial`/`failed`/`needs-human` transitions). This is the    #
# FIRST gh-mutating action in the workflow engine: every mutation is gated on    #
# ``ctx.dry_run`` (no network on the dry-run path), and the manifest declares the #
# gh-write capabilities it uses (see app/config/actions/admin/intake_invoice.yml).#
# --------------------------------------------------------------------------- #

# The engineering Program returns ``engineering_result = {ok, value, meta}`` (see
# bindings/engineer.py). visitors.py worked off a 4-way Invoice ``status``; the
# workflow does not yet carry one, so we DERIVE it:
#   * ``meta.status`` — if the engineer reported one of the canonical four,
#     honor it verbatim (forward-compatible: a richer Engineer can set it);
#   * else  ok && value      -> "completed"
#           ok && not value  -> "partial"
#           not ok           -> "failed".
# ``needs-human`` is only reachable via an explicit ``meta.status`` today — the
# bare {ok, value} shape cannot express operator-escalation. (Reported as a known
# limitation: the engineering_result needs to carry an explicit status to drive
# the needs-human transition from a derived mapping.)
_CANONICAL_STATUSES = ("completed", "partial", "failed", "needs-human")


def _invoice_status(engineering_result: Dict[str, Any]) -> str:
    er = engineering_result or {}
    meta = er.get("meta") or {}
    explicit = meta.get("status")
    if isinstance(explicit, str) and explicit in _CANONICAL_STATUSES:
        return explicit
    if not er.get("ok", False):
        return "failed"
    return "completed" if er.get("value") else "partial"


def intake_invoice(inputs: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """M8 — read the engineer's result, advance the GitHub label state machine, and
    record the transition. Mutations run ONLY when not ``ctx.dry_run``; under
    dry-run the action is log-only and returns the transition it *would* perform,
    so the dry-run path makes no network call (the workflow-engine equivalent of
    common.py's DRY-RUN discipline). Faithful port of visitors.py."""
    engineering_result = inputs.get("engineering_result") or {}
    job = inputs.get("job") or {}
    issue = job.get("issue")
    issue = "" if issue is None else str(issue)
    pr_number = job.get("pr_number")
    pr_number = "" if pr_number is None else str(pr_number)
    route_used = job.get("route") or "gen-local"
    status = _invoice_status(engineering_result)
    summary = (engineering_result.get("meta") or {}).get("summary") or "(no summary)"

    dry = bool(getattr(ctx, "dry_run", True))
    actions: list = []  # the gh mutations performed (or, under dry-run, intended)

    def _mutate(*args: str) -> None:
        """Perform a gh mutation, or record-only under dry-run. Either way the
        intended call is appended to the transition record (durable on the
        deliverables shelf)."""
        actions.append([str(a) for a in args])
        if not dry:
            common.gh_mutate(*args)

    common.log(f"intake-invoice: issue=#{issue} status={status} pr={pr_number or 'none'} dry_run={dry}")

    if status == "completed":
        common.log(f"#{issue}: completed — arming auto-merge")
        if pr_number:
            _mutate("pr", "comment", pr_number, "--body",
                    common.format_invoice_comment("completed", summary, route_used=route_used))
            # Arm auto-merge; branch protection still requires the human tap.
            _mutate("pr", "merge", pr_number, "--auto", "--squash",
                    "--subject", f"Closes #{issue}")
        _mutate("issue", "edit", issue,
                "--remove-label", "claimed", "--add-label", "done-pending-merge")
        common.ledger_emit("closure", issue, json.dumps(
            {"label_before": "claimed", "label_after": "done-pending-merge"},
            ensure_ascii=False,
        ))
    elif status in ("partial", "failed"):
        common.log(f"#{issue}: {status} — labeling fix-attempt-1; posting rescaffold directive (#137)")
        _mutate("issue", "edit", issue, "--add-label", "fix-attempt-1")
        if pr_number:
            _mutate("pr", "comment", pr_number, "--body",
                    common.format_invoice_comment(status, summary, route_used=route_used))
        # #137 — on a failed/partial engineer result, feed the failure back as a
        # diagnose-then-replan directive (the SAME shared text the CI-failure
        # ladder posts) rather than a bare tier bump, and ledger it. attempt 1 /
        # tier gen-local matches the fix-attempt-1 label just applied.
        directive = rescaffold.rescaffold_directive("1", "gen-local", status, summary)
        target, target_n = ("pr", pr_number) if pr_number else ("issue", issue)
        _mutate(target, "comment", target_n, "--body", directive)
        common.ledger_emit("fix-rescaffold", issue, json.dumps(
            {"issue": issue, "pr": pr_number, "attempt": "1", "tier": "gen-local",
             "conclusion": status, "rescaffold": True},
            ensure_ascii=False,
        ))
    elif status == "needs-human":
        common.log(f"#{issue}: needs-human — escalating to operator")
        _mutate("issue", "edit", issue,
                "--remove-label", "claimed", "--add-label", "needs-human")
        _mutate("issue", "comment", issue, "--body",
                common.format_invoice_comment("needs-human", summary, route_used=route_used))
    else:  # pragma: no cover — _invoice_status only emits the canonical four
        raise RuntimeError(f"intake-invoice: unknown invoice status {status!r}")

    common.log(f"intake-invoice: done (issue=#{issue} status={status})")
    return {"intake": {
        "issue": issue,
        "pr_number": pr_number,
        "status": status,
        "summary": summary,
        "route_used": route_used,
        "dry_run": dry,
        "mutations": actions,
    }}


def register(reg: Any) -> None:
    reg.register_action("prepare_env", prepare_env)
    reg.register_action("write_adversarial", write_adversarial)
    reg.register_action("update_docs", update_docs)
    reg.register_action("store", store, needs_ctx=True)
    reg.register_action("intake_invoice", intake_invoice, needs_ctx=True)
