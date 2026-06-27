"""dispatch.py — deterministic dispatch engine (HANDOFF §5.1).

Python port of scripts/dispatch.sh. Owns the transitions
``queued -> claimed``, ``queued -> needs-human`` and
``queued -> wontdo_parent_issue`` (an ``[Epic]``/``decompose`` container is a
tracker for its child issues, not an atomic unit of work, so it is flagged and
pointed at its children rather than escalated to a human); runs the E1-3 crash
reaper and the E5-3 soft-cap pre-flight guard. The per-issue claim walks the
intake -> workorder -> prep -> engineer stages through the shared
:class:`ExecutionVisitor`, honouring the --until gate exactly as claim_issue did.
"""

from __future__ import annotations

import json
import os
import re
import sys

from engine import proc

from . import common
from .classifier import Classifier
from .stages import (
    EngineerStage,
    IntakeStage,
    PrepStage,
    WorkorderStage,
    stage_ord,
)
from .visitors import ExecutionVisitor, TickContext

_visitor = ExecutionVisitor()
_reaped_this_tick: set[str] = set()


# --- gh JSON read helper ---------------------------------------------------
def _gh_json(*args, default=None):
    """Run a read-only ``gh ... --json`` call, returning parsed JSON (or default)."""
    common.require_tool(os.environ["GH_BIN"])
    cmd = [os.environ["GH_BIN"], *[str(a) for a in args], *common.gh_repo_args()]
    return proc.run_json(cmd, default=default)


def _label_names(issue) -> list[str]:
    out = []
    for lbl in issue.get("labels") or []:
        out.append(lbl.get("name") if isinstance(lbl, dict) else lbl)
    return [x for x in out if x is not None]


# --- issue sources (live gh vs. fixture) -----------------------------------
def _foundational_first(issues: list) -> list:
    """Order issues foundational-first (dependency depth, then creation order) via
    the ranker's offline priority, so a tick claims the foundation before late or
    leaf work — never a high-weight-labelled leaf (e.g. a 'Blocker' deploy task)
    ahead of the base it needs. Falls back to ascending issue number if the ranker
    is unavailable; ranking must never break the tick."""
    if len(issues) <= 1:
        return list(issues)
    try:
        import importlib
        _intake = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "intake"
        )
        if _intake not in sys.path:
            sys.path.insert(0, _intake)
        ranker = importlib.import_module("ranker")
        return ranker._rank_offline(list(issues))
    except Exception as exc:  # noqa: BLE001 — never let ranking break the tick
        common.log(f"dispatch: foundational ranker unavailable ({exc}); ordering by issue number")
        return sorted(issues, key=lambda i: int(i.get("number", 0) or 0))


def load_queued_issues() -> list:
    fixture = os.environ.get("PIPELINE_FIXTURE_ISSUES")
    if fixture:
        return _foundational_first(json.loads(open(fixture).read()))
    return _foundational_first(_gh_json(
        "issue", "list", "--state", "open",
        "--json", "number,title,body,labels", default=[]
    ))


def count_claimed() -> int:
    fixture = os.environ.get("PIPELINE_FIXTURE_ISSUES")
    if fixture:
        issues = json.loads(open(fixture).read())
        return sum(1 for i in issues if "claimed" in _label_names(i))
    data = _gh_json(
        "issue", "list", "--label", "claimed", "--state", "open",
        "--json", "number", "--limit", "50", default=[]
    )
    return len(data or [])


# --- crash reaper (E1-3) ---------------------------------------------------
def reaper_live_claimed_at(num) -> str:
    data = _gh_json("issue", "view", num, "--json", "comments", default={})
    marks = [
        c.get("createdAt")
        for c in (data or {}).get("comments", []) or []
        if "<!-- pipeline:route -->" in (c.get("body") or "")
    ]
    return marks[-1] if marks else ""


def reaper_live_has_pr(num) -> str:
    data = _gh_json(
        "pr", "list", "--state", "open", "--head", f"pipeline/issue-{num}",
        "--json", "number", "--limit", "1", default=[]
    )
    return "true" if len(data or []) > 0 else "false"


def reaper_claimed_candidates() -> list:
    src = os.environ.get("DISPATCH_REAPER_FIXTURE") or os.environ.get("PIPELINE_FIXTURE_ISSUES")
    if src:
        issues = json.loads(open(src).read())
        return [
            {
                "number": i.get("number"),
                "claimed_at": i.get("claimed_at"),
                "has_open_pr": i.get("has_open_pr", False),
            }
            for i in issues
            if "claimed" in _label_names(i)
        ]
    if not common.have_tool(os.environ["GH_BIN"]):
        return []
    data = _gh_json(
        "issue", "list", "--label", "claimed", "--state", "open",
        "--json", "number", "--limit", "50", default=[]
    )
    out = []
    for entry in data or []:
        num = entry.get("number")
        if num is None:
            continue
        at = reaper_live_claimed_at(num)
        out.append(
            {
                "number": num,
                "claimed_at": at if at else None,
                "has_open_pr": reaper_live_has_pr(num) == "true",
            }
        )
    return out


def reap_stuck_claims() -> None:
    if os.environ.get("DISPATCH_REAPER_ENABLED", "1") == "0":
        common.log("reaper: disabled (DISPATCH_REAPER_ENABLED=0)")
        return
    claimed = reaper_claimed_candidates()
    n = len(claimed)
    if n <= 0:
        return  # nothing claimed -> nothing to recover (silent)
    timeout_h = common.reaper_timeout_hours()
    now = common.reaper_now_epoch()
    cutoff_s = int(float(timeout_h) * 3600)
    common.log(f"reaper: inspecting {n} claimed issue(s) (timeout {timeout_h}h)")
    for entry in claimed:
        num = entry.get("number")
        claimed_at = entry.get("claimed_at")
        has_pr = entry.get("has_open_pr", False)
        if has_pr is True or has_pr == "true":
            common.log(f"reaper: #{num} has an open PR — not reaping (in-flight work)")
            continue
        if not claimed_at:
            common.log(
                f"reaper: #{num} has no resolvable claim timestamp — not reaping (conservative)"
            )
            continue
        at_epoch = common.reaper_epoch_of(str(claimed_at))
        if at_epoch is None:
            common.log(
                f"reaper: #{num} unparseable claim timestamp '{claimed_at}' — not reaping"
            )
            continue
        age_s = now - at_epoch
        age_h = f"{age_s / 3600.0:.1f}"
        if age_s >= cutoff_s:
            common.log(
                f"reaper: #{num} stuck in claimed ~{age_h}h (> {timeout_h}h) with no "
                f"open PR — re-queuing (claimed -> queued)"
            )
            common.gh_mutate(
                "issue", "edit", num, "--remove-label", "claimed", "--add-label", "queued"
            )
            tick = os.environ.get("DISPATCH_TICK_ID", "tick-unknown")
            common.gh_mutate(
                "issue", "comment", num, "--body",
                f"<!-- pipeline:reaper --> Re-queued by crash reaper: this issue was "
                f"stuck in `claimed` for ~{age_h}h (timeout {timeout_h}h) with no open "
                f"PR. tick={tick}. Recovery only (claimed -> queued, D1) — a later tick "
                f"will re-drain it.",
            )
            common.tick_record_reap(num)
            _reaped_this_tick.add(str(num))
        else:
            common.log(
                f"reaper: #{num} claimed ~{age_h}h ago (<= {timeout_h}h) — within "
                f"timeout, leaving claimed"
            )


# --- threshold comparison --------------------------------------------------
def below_threshold(confidence, threshold) -> bool:
    try:
        return float(confidence) < float(threshold)
    except (ValueError, TypeError):
        return 0.0 < float(threshold)


# --- transitions -----------------------------------------------------------
def to_needs_human(num, reason, action="") -> None:
    extra = []
    if action == "wont-do":
        extra = ["--add-label", "wont-do"]
    elif action == "duplicate?":
        extra = ["--add-label", "duplicate"]
    common.log(f"#{num} -> needs-human: {reason}")
    common.gh_mutate(
        "issue", "edit", num, "--remove-label", "queued", "--add-label", "needs-human", *extra
    )
    common.gh_mutate(
        "issue", "comment", num, "--body",
        f"Pipeline dispatch routed this to **needs-human**. Rationale: {reason}",
    )


# --- epic / parent-container routing (decompose) ---------------------------
# Issue text is UNTRUSTED data parsed for `#<number>` cross-references only
# (mirrors engine/dag.py). Markdown emphasis is stripped first so
# `**Parent epic:** #N` matches, and matching is MULTILINE/IGNORECASE.
_MD_EMPHASIS = re.compile(r"[*_`]+")
# A child task-listed in the epic body: `- [ ] #N` / `* [x] #N`.
_TASKLIST_CHILD = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*#(\d+)", re.MULTILINE)
# A child naming its parent: `Parent epic: #N`.
_PARENT_EPIC = re.compile(r"parent\s+epic:?\s*#(\d+)", re.IGNORECASE)


def discover_children(epic_num, epic_body, issues) -> list:
    """Find the constituent child issues of an epic (number, title), ascending.

    Two convention signals (the same ones the intake DAG parses):
      * children task-listed in the epic's own body (``- [ ] #N``), and
      * issues whose body names this epic (``Parent epic: #<epic_num>``).
    Titles come from the citing-issue scan; task-listed-only children carry "".
    """
    epic_n = int(epic_num)
    children: dict[int, str] = {}
    # Task-list markers (`- [ ]` / `* [x]`) are matched on the RAW body: stripping
    # markdown emphasis would eat the `*` bullet and lose `*`-bulleted children.
    for m in _TASKLIST_CHILD.finditer(epic_body or ""):
        cn = int(m.group(1))
        if cn != epic_n:
            children.setdefault(cn, "")
    # Prose `**Parent epic:** #N` needs emphasis stripped first (as in dag.py).
    for issue in issues or []:
        itext = _MD_EMPHASIS.sub("", issue.get("body") or "")
        if any(int(g) == epic_n for g in _PARENT_EPIC.findall(itext)):
            num = issue.get("number")
            if num is None:
                continue
            cn = int(num)
            if cn != epic_n:
                children[cn] = issue.get("title") or ""
    return [(n, children[n]) for n in sorted(children)]


def to_wontdo_parent(num, children) -> None:
    """``queued -> wontdo_parent_issue`` for an ``[Epic]``/``decompose`` issue.

    An epic is a container of sub-issues, not an atomic deliverable the engineer
    can implement, so dispatch declines to claim it — but it is *not* a human
    escalation (`needs-human`). It is flagged `wontdo_parent_issue` and the
    comment points at the constituent child issues so the trail lives in the
    thread (CLAUDE.md: durable knowledge goes into the issue/PR thread).
    """
    common.log(
        f"#{num} -> wontdo_parent_issue: epic/container (decompose) — "
        f"{len(children)} child issue(s) linked"
    )
    common.gh_mutate(
        "issue", "edit", num,
        "--remove-label", "queued", "--add-label", "wontdo_parent_issue",
    )
    header = (
        "Pipeline dispatch flagged this as a **parent / epic issue** "
        "(`wontdo_parent_issue`): it is a container of sub-issues, not an atomic "
        "unit of work, so it is not dispatched to the engineer directly."
    )
    if children:
        listing = "\n".join(
            f"- #{cn}" + (f" — {title}" if title else "") for cn, title in children
        )
        body = (
            f"{header}\n\nConstituent child issues:\n{listing}\n\n"
            "Each child is triaged and dispatched on its own. Close this epic once "
            "every child is delivered."
        )
    else:
        body = (
            f"{header}\n\nNo child issues were found yet. Children declare "
            f"membership with `Parent epic: #{num}` in their body, or are "
            "task-listed here as `- [ ] #N`; add them and this epic will track them."
        )
    common.gh_mutate("issue", "comment", num, "--body", body)


def claim_issue(ctx: TickContext) -> None:
    """Walk intake -> workorder -> prep -> engineer with the --until gates."""
    num = ctx.num
    until_ord = os.environ.get("DISPATCH_UNTIL_ORD")
    prep_ord = stage_ord("prep")

    # intake + workorder (claim, build job request, ledger, dump artifacts).
    IntakeStage().accept(_visitor, ctx)
    WorkorderStage().accept(_visitor, ctx)

    # Halt BEFORE prep for the intake / workorder stages.
    if until_ord and int(until_ord) < prep_ord:
        common.log(
            f"#{num} --until {os.environ['DISPATCH_UNTIL_STAGE']}: halting before prep "
            f"(work order dumped)"
        )
        return

    # Prep stage (#102): fail-safe — an un-provisionable harness blocks issuance.
    if not PrepStage().accept(_visitor, ctx):
        common.log(f"#{num} prep blocked issuance — not dispatching the engineer")
        # #127: intake already claimed the issue + created its worktree before
        # prep ran. Roll both back so a blocked issuance leaves no orphan
        # `claimed` label or `pipeline/issue-<n>` worktree (which a later
        # re-claim would collide with).
        _visitor.rollback_intake(ctx)
        return

    # Halt AFTER prep, before the engineer.
    if until_ord and int(until_ord) <= prep_ord:
        common.log(
            f"#{num} --until {os.environ['DISPATCH_UNTIL_STAGE']}: halting after prep "
            f"(before the engineer)"
        )
        return

    EngineerStage().accept(_visitor, ctx)


# --- pre-flight soft-cap guard (E5-3/#40) ----------------------------------
def budget_preflight_throttles() -> bool:
    if not os.environ.get("BUDGET_ORACLE_FIXTURE") and not common.have_tool(
        os.environ.get("CLAUDE_MONITOR_BIN", "claude-monitor")
    ):
        return False
    decision = proc.run_json(
        [os.environ["PYTHON_BIN"],
         str(common.PIPELINE_ROOT / "src" / "budget" / "guard.py"), "--record"],
        default=None,
    )
    if not decision or not decision.get("throttle"):
        return False
    fraction = decision.get("fraction")
    soft_cap = decision.get("soft_cap")
    plan = decision.get("plan")
    os.environ["PIPELINE_DRY_RUN"] = "1"
    common.log(
        f"dispatch: budget soft-cap THROTTLE (fraction={fraction} >= soft_cap="
        f"{soft_cap}, plan={plan}) — forcing PIPELINE_DRY_RUN=1, skipping claim this tick"
    )
    return True


def main(argv=None) -> int:
    # Crash reaper (E1-3) first this tick.
    reap_stuck_claims()

    # Pre-flight soft-cap guard (E5-3/#40).
    if budget_preflight_throttles():
        common.log("dispatch: throttled (soft-cap) — no issue claimed this tick.")
        return 0

    issues = load_queued_issues()
    claimed = count_claimed()
    concurrency = int(os.environ["PIPELINE_CONCURRENCY"])
    n = len(issues)
    common.log(
        f"dispatch: {n} queued issue(s); {claimed} already claimed; "
        f"concurrency={concurrency}; dry_run={os.environ['PIPELINE_DRY_RUN']}"
    )
    if n <= 0:
        common.log("dispatch: nothing queued; exiting 0.")
        return 0

    threshold = os.environ["PIPELINE_CONFIDENCE_THRESHOLD"]
    repo = os.environ.get("PIPELINE_REPO", "")
    for issue in issues:
        num = str(issue.get("number"))
        # Crash reaper (E1-3, D1): don't re-claim an issue reaped this tick.
        if num in _reaped_this_tick:
            common.log(
                f"#{num} was re-queued by the reaper this tick — leaving queued for a "
                f"later tick (D1)"
            )
            continue
        title = issue.get("title") or ""
        body = issue.get("body") or ""
        labels = ",".join(_label_names(issue))

        verdict = (
            Classifier()
            .for_issue(num)
            .title(title)
            .body(body)
            .classify()
        )
        action, scope, route, confidence = (
            verdict.action, verdict.scope, verdict.route, verdict.confidence
        )
        common.log(
            f"#{num} classified: action={action} scope={scope} route={route} "
            f"confidence={confidence}"
        )

        if action == "decompose":
            # An [Epic]/container: flag as a parent issue pointing at its
            # children, NOT a human escalation.
            to_wontdo_parent(num, discover_children(num, body, issues))
            continue
        if action != "implement":
            to_needs_human(
                num,
                f"classifier action='{action}' (not implementable autonomously)",
                action,
            )
            continue
        if below_threshold(confidence, threshold):
            to_needs_human(num, f"confidence {confidence} < threshold {threshold}")
            continue
        if claimed >= concurrency:
            common.log(
                f"#{num} eligible (route={route}) but concurrency {concurrency} "
                f"reached; leaving queued."
            )
            continue

        ctx = TickContext(
            repo=repo,
            num=num,
            title=title,
            body=body,
            labels=labels,
            route=route,
            scope=scope,
            confidence=str(confidence),
            result_json=verdict.raw,
        )
        claim_issue(ctx)
        claimed += 1

    common.log("dispatch: complete.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except common.PipelineExit as exc:
        sys.exit(exc.code)
