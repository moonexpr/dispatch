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
from .seam import (
    SEAM_SCHEMA_VERSION,
    is_valid,
    JOB_REQUEST_SCHEMA,
    JOB_REQUEST_FIELDS as _JOB_REQUEST_FIELDS,
)


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

    def rollback_intake(self, ctx) -> None:
        """Undo visit_intake's claim when a later stage blocks issuance (#127).

        On the live path ``visit_intake`` sets ``claimed`` and creates the
        ``pipeline/issue-<n>`` worktree *before* prep runs. If prep then fails
        safe (un-provisionable harness -> blocked issuance) the engineer is
        correctly not dispatched, but without this rollback the issue is left
        ``claimed`` with an orphan worktree/branch — which a later re-claim
        collides with (``git worktree add`` on an existing path). This reverses
        exactly the two mutations ``visit_intake`` made, so a blocked issuance
        leaves no orphan label or worktree behind. Dry-run-gated like the
        forward path (the same DRY-RUN tokens are printed, not executed).
        """
        num = ctx.num
        common.log(f"#{num} rollback: claimed -> queued (issuance blocked; releasing claim)")
        # Reverse the label transition (claimed -> queued).
        common.gh_mutate(
            "issue", "edit", num, "--remove-label", "claimed", "--add-label", "queued"
        )
        # Reverse the worktree isolation: remove the orphan worktree + its branch.
        wt = f"{os.environ['PIPELINE_WORKTREE_ROOT']}/issue-{num}"
        common.run(
            "git", "-C", str(common.PIPELINE_ROOT), "worktree", "remove", "--force", wt,
        )
        common.run(
            "git", "-C", str(common.PIPELINE_ROOT), "branch", "-D", f"pipeline/issue-{num}",
        )
        # Run-ledger: record the claim release for cross-run auditability.
        common.ledger_emit(
            "rollback", num, _jc({"label_before": "claimed", "label_after": "queued"})
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
                # schema_version freezes the architect↔worker seam on the LIVE path
                # (schemas/job-request.v1.json), not just the --until/--from artifact.
                "schema_version": SEAM_SCHEMA_VERSION,
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
        # Authoring engine. `baseworkflow` is the ONLY supported workorder authoring
        # engine: it runs a BaseWorkflow over the Job Request and folds its authored
        # orchestration_script / work_plan back in (fail-safe: on any error the
        # request is returned unchanged). The historical `visitor` authoring engine —
        # which left the Job Request byte-for-byte unchanged — is DEPRECATED and
        # DISABLED for release: `DISPATCH_ENGINE=visitor` is no longer selectable. An
        # explicit opt-in is ignored (with a warning) and baseworkflow runs anyway.
        # The default literal mirrors common.py's
        # _default("DISPATCH_ENGINE", "baseworkflow").
        if common.env("DISPATCH_ENGINE", "baseworkflow") != "baseworkflow":
            common.warn(
                "DISPATCH_ENGINE=visitor selects the deprecated visitor authoring "
                "engine, which is DISABLED as of release. Ignoring it and using the "
                "supported baseworkflow engine; unset DISPATCH_ENGINE to silence this."
            )
        from .baseworkflow_bridge import author_via_baseworkflow

        ctx.job_request = author_via_baseworkflow(ctx.job_request)
        # --- DEPRECATED / DISABLED: the legacy `visitor` authoring fallback once
        # lived here as the `else` arm above — it left ctx.job_request byte-for-byte
        # unchanged instead of running BaseWorkflow authoring. It is retained in
        # history for reference only and is no longer reachable. ---

        # Freeze the seam on the LIVE path (#143): validate the Job Request's
        # seam projection against schemas/job-request.v1.json BEFORE it is handed to
        # the Engineer — not just on the --until/--from artifact bridge. The
        # baseworkflow authoring fold above may have added internal fields
        # (orchestration_script / work_plan / plan); those are additive and the
        # worker ignores them, so we validate the frozen seam *projection* (the v1
        # required fields), tolerating the enriched superset. A failure is logged,
        # never fatal — the seam observes, it does not break the tick.
        try:
            _req = json.loads(ctx.job_request)
            _proj = {k: _req[k] for k in _JOB_REQUEST_FIELDS if k in _req}
            _err = is_valid(_proj, JOB_REQUEST_SCHEMA)
            if _err:
                common.warn(f"#{num} job-request: seam validation failed ({_err})")
        except (ValueError, TypeError) as _exc:  # malformed JSON — non-fatal
            common.warn(f"#{num} job-request: seam validation skipped ({_exc})")

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

    # ------------------------------------------------ admin: PR consolidation
    def _open_consolidated_pr(self, issue: str, branch: str, summary: str) -> str:
        """Open ONE PR for the engineer's pushed branch. PR creation is the
        Administrator's job now, not the engineer's (the engineer commits + pushes a
        branch, pr_number=null), so a decomposed issue lands as a single PR. Dry-run
        records the intended ``gh pr create`` (greppable) and returns ``""`` (no PR
        to arm); live captures the new PR number. Body carries ``Closes #<n>`` for
        auto-close on (squash-)merge. Base: ``PIPELINE_DEFAULT_BRANCH`` (default
        ``main``)."""
        base = os.environ.get("PIPELINE_DEFAULT_BRANCH", "main")
        pr_title = f"Implement #{issue}"
        pr_body = (
            f"Consolidated implementation of #{issue} by the dispatch pipeline: the "
            f"engineer committed to `{branch}`; the Administrator opened this PR.\n\n"
            f"{summary}\n\nCloses #{issue}"
        )
        args = ["pr", "create", "--base", base, "--head", branch,
                "--title", pr_title, "--body", pr_body]
        if common.is_dry_run():
            common.gh_mutate(*args)  # records the intended call; no network
            return ""
        cmd = [os.environ["GH_BIN"], *args, *common.gh_repo_args()]
        res = proc.run(cmd, capture=True)
        url = (res.stdout or "").strip()
        tail = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        if res.returncode == 0 and tail.isdigit():
            common.log(f"#{issue}: admin opened consolidated PR #{tail} for {branch}")
            return tail
        common.log(f"#{issue}: admin PR creation failed for {branch} (rc={res.returncode})")
        return ""

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
            from . import tick_consolidation
            if tick_consolidation.enabled():
                # Tick-level super-PR mode (DISPATCH_TICK_PR=1): DEFER PR creation.
                # The engineer pushed its branch; record it for the after-the-loop
                # tick consolidation (ONE PR across every issue this tick) and just
                # relabel here — no per-issue PR, no per-issue merge arm.
                branch = inv.get("branch") or ""
                tick_consolidation.record_completed(issue, branch)
                common.log(
                    f"#{issue}: completed — deferring PR to the tick super-PR "
                    f"(branch {branch or 'none'})"
                )
            else:
                # Per-issue mode (default): the engineer commits + pushes a branch but
                # does NOT open a PR — admin owns PR creation (so a decomposed issue
                # consolidates into ONE PR). Open the consolidated PR for its branch
                # when the Invoice carries none, then arm auto-merge. (Fixtures that
                # already carry a pr_number skip the create and arm directly.)
                if not pr_number:
                    branch = inv.get("branch") or ""
                    if branch:
                        pr_number = self._open_consolidated_pr(issue, branch, summary)
                common.log(f"#{issue}: completed — arming auto-merge")
                if pr_number:
                    common.gh_mutate(
                        "pr", "comment", pr_number, "--body",
                        common.format_invoice_comment("completed", summary, route_used=route_used),
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
