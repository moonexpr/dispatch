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
import os
from typing import Any, Dict

import prep  # src/baseworkflow/subsystems/prep.py
import rescaffold  # src/baseworkflow/subsystems/rescaffold.py — the shared #137 directive

# common carries the dry-run-aware gh wrapper + run-ledger (orchestration policy).
# bindings/__init__ puts src/orchestration on sys.path, so this is a flat import.
import common  # src/baseworkflow/subsystems/common.py
from engine import proc  # engine.proc — capturing subprocess wrapper (PR-create stdout)


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
# M7.5 — consolidate the engineering result into ONE pull request.              #
#                                                                               #
# This is the squash-consolidation seam: the engineering Program (work phase)   #
# leaves the unit-agents' work as commits on ONE issue branch — the engineers   #
# COMMIT, they do not open PRs. The build phase opens a single PR for that       #
# branch; the actual squash happens at merge time, where intake_invoice arms     #
# `pr merge --auto --squash` (so the unit commits collapse to one commit on the  #
# base). Moving PR-creation here — out of the per-unit engineer — is what lets a #
# decomposed issue land as one reviewable PR instead of N.                       #
#                                                                               #
# Per-issue scope (one BaseWorkflow run = one issue, the work_plan invariant     #
# "one issue, one branch, one PR"). A cross-issue / per-tick super-PR is a       #
# deliberately deferred layer ABOVE this per-issue build phase.                  #
#                                                                               #
# GitHub-mutating, so — like intake_invoice — every write is gated on            #
# ``ctx.dry_run``; under dry-run the action records the intended `gh pr create`  #
# (greppable DRY-RUN line) and makes no network call.                            #
# --------------------------------------------------------------------------- #
def _default_branch(job: Dict[str, Any]) -> str:
    """Base branch for the PR: an explicit job hint, else PIPELINE_DEFAULT_BRANCH,
    else ``main`` (the overwhelming default for the target repos)."""
    return str(job.get("default_branch") or os.environ.get("PIPELINE_DEFAULT_BRANCH") or "main")


def _engineer_branch(engineering_result: Dict[str, Any], job: Dict[str, Any], issue: str) -> str:
    """The branch the engineering Program pushed its commits to. Prefer a branch the
    program surfaced (value/meta), then a job hint, then the canonical
    ``pipeline/issue-<n>`` the engineer names by convention."""
    for carrier in (engineering_result.get("value"), engineering_result.get("meta")):
        if isinstance(carrier, dict) and carrier.get("branch"):
            return str(carrier["branch"])
    if job.get("branch"):
        return str(job["branch"])
    return f"pipeline/issue-{issue}"


def consolidate_pr(inputs: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
    """M7.5 — open ONE consolidated PR for the engineering branch, or record a skip.

    Only a ``completed`` engineering result yields a PR — a partial/failed/
    needs-human result has nothing shippable, so we record a skip and let
    ``intake_invoice`` drive the fix-ladder / escalation. The PR body restates the
    work plan's acceptance criteria and carries ``Closes #<n>`` so GitHub auto-closes
    the issue on (squash-)merge. Writes ``consolidation`` to the deliverables shelf;
    ``intake_invoice`` reads ``consolidation.pr_number`` to arm auto-merge."""
    engineering_result = inputs.get("engineering_result") or {}
    job = inputs.get("job") or {}
    work_plan = inputs.get("work_plan") or {}
    issue = job.get("issue")
    issue = "" if issue is None else str(issue)
    title = job.get("title") or f"issue #{issue}"
    status = _invoice_status(engineering_result)
    dry = bool(getattr(ctx, "dry_run", True))

    if status != "completed":
        common.log(f"consolidate-pr: issue=#{issue} status={status} — nothing to ship, no PR (skip)")
        return {"consolidation": {
            "issue": issue, "status": status, "pr_number": None, "branch": None,
            "skipped": True, "dry_run": dry, "mutations": [],
        }}

    branch = _engineer_branch(engineering_result, job, issue)
    base = _default_branch(job)
    criteria = work_plan.get("acceptance_criteria") or []
    acc_block = "\n".join(f"- [ ] {c}" for c in criteria) or "- [ ] see issue acceptance criteria"
    summary = (engineering_result.get("meta") or {}).get("summary") or ""
    pr_title = f"Implement #{issue}: {title}"
    pr_body = (
        f"Consolidated implementation of #{issue} by the dispatch BaseWorkflow build "
        f"phase. The engineering agents committed to `{branch}`; this single PR "
        f"squash-merges them.\n\n## Acceptance\n{acc_block}\n\n{summary}\n\nCloses #{issue}"
    )
    # --repo is appended by gh_mutate / gh_repo_args from PIPELINE_REPO (parity with
    # intake_invoice), so it is NOT in the arg vector here.
    args = ["pr", "create", "--base", base, "--head", branch,
            "--title", pr_title, "--body", pr_body]
    mutations = [[str(a) for a in args]]
    common.log(f"consolidate-pr: issue=#{issue} branch={branch} base={base} dry_run={dry}")

    pr_number: Any = None
    pr_url = ""
    if dry:
        # Record-only: prints the greppable DRY-RUN line, makes no network call.
        common.gh_mutate(*args)
    else:
        cmd = [os.environ.get("GH_BIN", "gh"), *args, *common.gh_repo_args()]
        res = proc.run(cmd, capture=True)
        pr_url = (res.stdout or "").strip()
        tail = pr_url.rstrip("/").rsplit("/", 1)[-1] if pr_url else ""
        if res.returncode == 0 and tail.isdigit():
            pr_number = int(tail)
        else:
            # Branch is pushed but the PR could not be opened — partial (work done,
            # no PR to arm-merge). intake_invoice then runs the partial transition.
            status = "partial"
            common.log(f"consolidate-pr: PR creation failed for #{issue} "
                       f"(rc={res.returncode}) — partial")

    return {"consolidation": {
        "issue": issue, "status": status, "pr_number": pr_number, "pr_url": pr_url,
        "branch": branch, "base": base, "skipped": False, "dry_run": dry,
        "mutations": mutations,
    }}


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
    # The PR now comes from the build-phase consolidation (admin:consolidate_pr),
    # not from the engineer — read its pr_number and (possibly downgraded) status
    # from there, falling back to the job for callers that don't run consolidation.
    consolidation = inputs.get("consolidation") or {}
    issue = job.get("issue")
    issue = "" if issue is None else str(issue)
    pr_number = consolidation.get("pr_number")
    if pr_number is None:
        pr_number = job.get("pr_number")
    pr_number = "" if pr_number is None else str(pr_number)
    route_used = job.get("route") or "gen-local"
    # consolidate_pr may DOWNGRADE a completed result to "partial" when the PR could
    # not be opened (branch pushed, no PR to arm); honor the consolidation verdict.
    status = consolidation.get("status") or _invoice_status(engineering_result)
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
    reg.register_action("consolidate_pr", consolidate_pr, needs_ctx=True)
    reg.register_action("intake_invoice", intake_invoice, needs_ctx=True)
