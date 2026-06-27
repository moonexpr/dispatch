"""visitors.py — operations over pipeline stages (the visitor side).

:class:`StageVisitor` declares one method per stage; :class:`ExecutionVisitor`
is the production implementation that performs each stage's real (dry-run-gated)
work. State that flows between stages rides on :class:`TickContext`.

The behaviour is ported 1:1 from the former shell, split along the boundaries
the ``pipeline.sh -h`` text already advertised:

  intake          claim the issue off GitHub (queued -> claimed, route marker,
                  worktree)                              [was dispatch.sh claim_issue head]
  workorder       build the Job Request, ledger it, dump --until artifacts
  prep            provision the declared harness (fail-safe)   [was prep_stage]
  engineer        hand the Job Request to ENGINEER_BIN          [engineer_dispatch]
  intake-invoice  handle the returned Invoice                   [architect-intake.sh]
  closure         merge/close transitions                       [closure.sh]

Every emitted string (log lines, DRY-RUN tokens, comment bodies, ledger fields)
is identical to the shell — smoke.sh greps them verbatim.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from engine import proc

from . import common


def _jc(obj) -> str:
    """Compact JSON (jq -c style: no inter-token spaces)."""
    return json.dumps(obj, separators=(",", ":"))


@dataclass
class TickContext:
    """State threaded through one issue's (or one replay's) stage walk."""

    repo: str = ""
    # classified issue fields (intake/workorder)
    num: str = ""
    title: str = ""
    body: str = ""
    labels: str = ""
    route: str = ""
    scope: str = ""
    confidence: str = ""
    result_json: str = ""
    # produced / replayed artifacts
    job_request: str = ""
    invoice_json: str = ""
    # closure payload fields
    pr: str = ""
    conclusion: str = "failure"
    review: str = "none"
    merged: str = "false"

    def artifacts_td(self):
        """The per-tick artifact dir, or None when DISPATCH_ARTIFACTS_DIR unset."""
        ad = os.environ.get("DISPATCH_ARTIFACTS_DIR")
        if not ad:
            return None
        return Path(ad) / os.environ.get("DISPATCH_TICK_ID", "tick-unknown")


class StageVisitor(ABC):
    @abstractmethod
    def visit_intake(self, stage, ctx): ...

    @abstractmethod
    def visit_workorder(self, stage, ctx): ...

    @abstractmethod
    def visit_prep(self, stage, ctx): ...

    @abstractmethod
    def visit_engineer(self, stage, ctx): ...

    @abstractmethod
    def visit_intake_invoice(self, stage, ctx): ...

    @abstractmethod
    def visit_closure(self, stage, ctx): ...


def render_workorder(num, title, route, scope, confidence, body) -> str:
    """The human-readable work order dumped under --until (was render_workorder)."""
    return (
        f"WORK PLAN — issue #{num}\n"
        f"Title: {title}\n"
        f"Route: {route}    Scope: {scope}    Confidence: {confidence}\n"
        f"\n"
        f"{body}\n"
    )


class ExecutionVisitor(StageVisitor):
    """Performs each stage's real work, honouring PIPELINE_DRY_RUN throughout."""

    # ---------------------------------------------------------------- intake
    def visit_intake(self, stage, ctx) -> None:
        """Claim the issue: route marker, queued -> claimed, worktree (was claim_issue head)."""
        num = ctx.num
        common.log(f"#{num} -> claimed (route={ctx.route})")
        # Heartbeat seam (E1-2): record which issue this tick claimed.
        common.tick_record_claim(num)
        # Run-ledger (E4/#36): the queued->claimed transition.
        common.ledger_emit(
            "claimed", num, _jc({"label_before": "queued", "label_after": "claimed"})
        )
        # Durable routing decision (machine-findable marker) as an issue comment.
        common.gh_mutate(
            "issue", "comment", num, "--body",
            common.format_route_comment(ctx.result_json),
        )
        common.gh_mutate(
            "issue", "edit", num, "--remove-label", "queued", "--add-label", "claimed"
        )
        # Worktree isolation for the worker session (§5.4 step 3).
        common.run(
            "git", "-C", str(common.PIPELINE_ROOT), "worktree", "add",
            f"{os.environ['PIPELINE_WORKTREE_ROOT']}/issue-{num}",
            "-b", f"pipeline/issue-{num}",
        )

    # ------------------------------------------------------------- workorder
    def visit_workorder(self, stage, ctx) -> None:
        """Build the Job Request, ledger it, dump --until artifacts."""
        num = ctx.num
        try:
            conf = json.loads(ctx.confidence)
        except (ValueError, TypeError):
            conf = ctx.confidence
        ctx.job_request = _jc(
            {
                "job_id": f"issue-{num}",
                "issue": int(num),
                "repo": ctx.repo,
                "title": ctx.title,
                "body": ctx.body,
                "route": ctx.route,
                "scope": ctx.scope,
                "confidence": conf,
            }
        )
        # Authoring-engine seam (Phase 3, DEFAULT ON). DISPATCH_ENGINE selects the
        # workorder authoring engine: `baseworkflow` (the DEFAULT) runs a
        # BaseWorkflow over the Job Request and folds its authored
        # orchestration_script / work_plan back in; `visitor` is the explicit
        # fallback that leaves the Job Request byte-for-byte unchanged. Fail-safe:
        # on any error the request is returned unchanged. The fallback literal here
        # mirrors common.py's _default("DISPATCH_ENGINE", "baseworkflow").
        if common.env("DISPATCH_ENGINE", "baseworkflow") == "baseworkflow":
            from .baseworkflow_bridge import author_via_baseworkflow

            ctx.job_request = author_via_baseworkflow(ctx.job_request)
        else:
            common.warn(
                "DISPATCH_ENGINE=visitor selects the deprecated visitors authoring "
                "engine. It is kept only as a fallback and will not be supported in "
                "the future; migrate to the default baseworkflow engine."
            )

        # Run-ledger: the architect emitted the work order for this issue.
        common.ledger_emit("work-order", num, "{}")
        # Stage gate (E2-2/#31): dump work order + job request under --until.
        if os.environ.get("DISPATCH_UNTIL_STAGE") and os.environ.get("DISPATCH_ARTIFACTS_DIR"):
            td = ctx.artifacts_td()
            td.mkdir(parents=True, exist_ok=True)
            (td / "job-request.json").write_text(ctx.job_request + "\n")
            (td / "workorder.txt").write_text(
                render_workorder(num, ctx.title, ctx.route, ctx.scope, ctx.confidence, ctx.body)
            )

    # ------------------------------------------------------------------ prep
    def visit_prep(self, stage, ctx) -> bool:
        """Provision the work plan's declared harness (was prep_stage).

        Returns True to PROCEED (issue the job), False to BLOCK. Idempotent and
        side-effect-free except for local-file ledger/artifact writes.
        """
        args = ctx.job_request
        labels = ctx.labels
        try:
            num = json.loads(args).get("issue", "?")
        except (ValueError, TypeError):
            num = "?"

        # 1. Deterministic prep plan from the Job Request.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as jt:
            jt.write(args)
            jt_path = jt.name
        pa = ["--job-json", jt_path]
        if labels:
            pa += ["--labels", labels]
        result = proc.run([os.environ["PYTHON_BIN"], common.PREP_PY, *pa])
        os.unlink(jt_path)
        prep_json = result.stdout if result.returncode == 0 else ""
        if result.returncode != 0 or not prep_json.strip():
            common.err(
                f"#{num} prep: planner failed (rc={result.returncode}) — blocking issuance (fail-safe)"
            )
            common.ledger_emit(
                "prep", num, _jc({"provisionable": False, "reason": "planner-error"})
            )
            return False

        plan = json.loads(prep_json)
        harness = plan.get("harness") or "standard"
        strategy = plan.get("strategy") or "sequential"
        provisioner = plan.get("provisioner") or "none"
        provisionable = bool(plan.get("provisionable", False))
        reason = plan.get("block_reason") or ""
        steps = plan.get("steps") or []

        # 2. Run-ledger: record the prep stage transition.
        common.ledger_emit(
            "prep", num,
            _jc({"harness": harness, "strategy": strategy, "provisionable": provisionable}),
        )

        # 3. Dump the prep artifact under --until.
        if os.environ.get("DISPATCH_UNTIL_STAGE") and os.environ.get("DISPATCH_ARTIFACTS_DIR"):
            td = ctx.artifacts_td()
            td.mkdir(parents=True, exist_ok=True)
            (td / "prep.json").write_text(prep_json.rstrip("\n") + "\n")

        # 4. Fail-safe: an un-provisionable harness blocks issuance.
        if not provisionable:
            common.err(
                f"#{num} prep: harness '{harness}' is not provisionable — "
                f"{reason or 'unknown reason'}; blocking issuance"
            )
            return False

        # 5. Dry-run prints intended steps; live logs readiness.
        if common.is_dry_run():
            common.log(
                f"#{num} prep DRY-RUN: would provision harness '{harness}' "
                f"(strategy {strategy}); provisions nothing:"
            )
            for step in steps:
                if step:
                    common.log(f"#{num} prep   • {step}")
        elif provisioner == "none" and strategy == "sequential":
            common.log(
                f"#{num} prep: harness '{harness}' is a no-op; sequential "
                f"orchestration — nothing to provision"
            )
        else:
            common.log(
                f"#{num} prep: harness '{harness}' ready ({provisioner}); "
                f"'{strategy}' orchestration armed"
            )
        return True

    # -------------------------------------------------------------- engineer
    def visit_engineer(self, stage, ctx) -> int:
        return common.engineer_dispatch(ctx.job_request)

    # -------------------------------------------------------- intake-invoice
    def visit_intake_invoice(self, stage, ctx) -> None:
        """Handle the Invoice returned by the Engineer (was architect-intake.sh)."""
        invoice_json = ctx.invoice_json
        try:
            inv = json.loads(invoice_json)
        except (ValueError, TypeError):
            inv = {}

        status = inv.get("status") or ""
        issue = inv.get("issue")
        issue = "" if issue is None else str(issue)
        pr_number = inv.get("pr_number")
        pr_number = "" if pr_number is None else str(pr_number)
        summary = inv.get("summary") or ""
        route_used = inv.get("route_used") or "gen-local"

        if not status or not issue:
            print(
                "architect-intake: Invoice missing required fields (status, issue)",
                file=sys.stderr,
            )
            raise common.PipelineExit(1)

        common.log(f"architect-intake: issue=#{issue} status={status} pr={pr_number or 'none'}")

        cost = inv.get("cost") or {}
        cost_fields = _jc(
            {
                "tokens_in": cost.get("tokens_in"),
                "tokens_out": cost.get("tokens_out"),
                "duration_seconds": cost.get("duration_seconds"),
                "model": cost.get("model"),
            }
        )
        common.ledger_emit("engineer-dispatch", issue, cost_fields)
        common.ledger_emit("invoice", issue, cost_fields)

        if status == "completed":
            common.log(f"#{issue}: completed — arming auto-merge")
            if pr_number:
                common.gh_mutate(
                    "pr", "comment", pr_number, "--body",
                    f"**Engineer invoice** (route: `{route_used}`): {summary}",
                )
                # Arm auto-merge; branch protection still requires the human tap.
                common.gh_mutate(
                    "pr", "merge", pr_number, "--auto", "--squash",
                    "--subject", f"Closes #{issue}",
                )
            common.gh_mutate(
                "issue", "edit", issue,
                "--remove-label", "claimed", "--add-label", "done-pending-merge",
            )
            closure_fields = json.loads(cost_fields)
            closure_fields["label_before"] = "claimed"
            closure_fields["label_after"] = "done-pending-merge"
            common.ledger_emit("closure", issue, _jc(closure_fields))
        elif status in ("partial", "failed"):
            common.log(
                f"#{issue}: {status} — labeling fix-attempt-1; "
                f"fix-dispatch.sh handles progression"
            )
            common.gh_mutate("issue", "edit", issue, "--add-label", "fix-attempt-1")
            if pr_number:
                common.gh_mutate(
                    "pr", "comment", pr_number, "--body",
                    common.format_invoice_comment(status, summary),
                )
        elif status == "needs-human":
            common.log(f"#{issue}: needs-human — escalating to operator")
            common.gh_mutate(
                "issue", "edit", issue,
                "--remove-label", "claimed", "--add-label", "needs-human",
            )
            common.gh_mutate(
                "issue", "comment", issue, "--body",
                common.format_invoice_comment("needs-human", summary),
            )
        else:
            print(f"architect-intake: unknown Invoice status '{status}'", file=sys.stderr)
            raise common.PipelineExit(1)

        common.log(f"architect-intake: done (issue=#{issue} status={status})")

    # --------------------------------------------------------------- closure
    def visit_closure(self, stage, ctx) -> None:
        """Merge/close transitions (was closure.sh phase_success / phase_merged)."""
        pr, issue = ctx.pr, ctx.num
        if ctx.merged == "true":
            common.log(f"closure(merged): PR #{pr} merged; finalizing issue #{issue}.")
            common.gh_mutate(
                "issue", "comment", issue, "--body",
                f"Pipeline summary: PR #{pr} merged and this issue auto-closed. "
                f"Closing out as done.",
            )
            common.gh_mutate(
                "issue", "edit", issue, "--add-label", "done",
                "--remove-label", "done-pending-merge",
            )
            common.log(f"closure(merged): issue #{issue} is DONE.")
            return

        conclusion, review = ctx.conclusion, ctx.review
        if conclusion != "success" or review != "approved":
            common.log(
                f"closure: PR #{pr} not ready (conclusion={conclusion}, "
                f"review={review}); no-op exit 0."
            )
            return
        common.log(f"closure(success): PR #{pr} (Closes #{issue}) -> auto-merge.")
        common.gh_mutate(
            "pr", "edit", pr, "--remove-label", "in-review",
            "--add-label", "done-pending-merge",
        )
        # Arm auto-merge (squash). Branch protection still holds the merge.
        common.gh_mutate("pr", "merge", pr, "--auto", "--squash")
        common.gh_mutate(
            "pr", "comment", pr, "--body",
            f"Pipeline closure summary: CI green, auto-merge armed (squash). "
            f"Awaiting the required human approval; merging will close #{issue}.",
        )
        common.log(f"closure(success): PR #{pr} armed; waiting on human approval tap.")
