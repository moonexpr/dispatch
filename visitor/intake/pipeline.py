#!/usr/bin/env python3
"""pipeline.py — intake → [rank] → select → classify → job request.

Fetches issues from a GitHub ProjectV2 or repo, optionally ranks them by
dependency order (--rank), applies selection criteria, classifies each issue
with the keyword classifier, and emits job request JSON conforming to
schemas/job-request.json.

Default: emit the first eligible job request as a JSON object.
Use --all to emit a JSON array of all eligible job requests.

Usage
-----
  # First Scheduled issue under 10 hours from a project board:
  python3 src/intake/pipeline.py --project ReclaimByDesign/5 \\
      --status "Scheduled" --hours-max 10

  # Dependency-ranked: pick the most foundational queued issue:
  python3 src/intake/pipeline.py --repo owner/repo --label queued --rank

  # All queued issues from a repo:
  python3 src/intake/pipeline.py --repo owner/repo --label queued --all

  # Offline test:
  INTAKE_FIXTURE_PROJECT=src/intake/fixtures/project-items-raw.json \\
    python3 src/intake/pipeline.py --project ReclaimByDesign/5 --all

Selection flags (all AND-ed; unset = pass through)
---------------------------------------------------
  --status REGEX         project_status must match (case-insensitive regex)
  --dispatch VALUE       dispatch field must equal VALUE; "*" = any non-null
  --hours-max N          hours_estimate must be <= N (null items pass through)
  --confidence N         minimum classifier confidence (default: 0.55)

Env-var equivalents: INTAKE_PROJECT, INTAKE_REPO, INTAKE_STATUS_FILTER,
  INTAKE_LABEL_FILTER, INTAKE_LIMIT, INTAKE_FIXTURE_PROJECT,
  INTAKE_FIXTURE_REPO, INTAKE_DISPATCH_FILTER, INTAKE_HOURS_MAX,
  PIPELINE_CONFIDENCE_THRESHOLD, GH_BIN, PYTHON_BIN.

Exit codes: 0 at least one job request emitted; 1 runtime error;
  2 bad args; 3 no eligible issues found.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from dataclasses import asdict
from typing import Any, Dict, List, Optional

# Make sibling-service imports resolve regardless of CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))   # src/ (tuning)
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))  # repo root for the engine package
import intake as _intake    # noqa: E402
import ranker as _ranker    # noqa: E402
from engine import proc, structures as _dag  # noqa: E402
import tuning               # noqa: E402

CLASSIFIER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "../classifier/classify.py",
)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _passes_selection(
    item: _intake.IntakeItem,
    *,
    status_filter: str,
    dispatch_filter: str,
    hours_max: Optional[int],
) -> bool:
    if status_filter:
        status = item.project_status or ""
        if not re.search(status_filter, status, re.IGNORECASE):
            return False

    if dispatch_filter:
        d = item.dispatch or ""
        if dispatch_filter == "*":
            if not d:
                return False
        else:
            if d != dispatch_filter:
                return False

    if hours_max is not None:
        h = item.hours_estimate
        if h is not None and h > hours_max:
            return False

    return True


# ---------------------------------------------------------------------------
# Dependency gate (local DAG)
# ---------------------------------------------------------------------------

def _item_dict(item: _intake.IntakeItem) -> Dict[str, Any]:
    """Minimal dict view dag.build needs (it reads only number/title/body)."""
    return {"number": item.number, "title": item.title, "body": item.body}


def _dispatchable(enabled: set, graph) -> set:
    """Deterministic greedy fixpoint over the local DAG.

    An issue is *dispatchable* iff it passed per-item selection AND every issue
    it depends on (its blockers, `graph.deps[n]`) is itself dispatchable. We
    start from the `enabled` set (issues that passed selection) and repeatedly
    drop any issue that still has a dependency outside the surviving set, in
    sorted order, until a fixpoint is reached. Iteration order is fully
    determined by the issue numbers, so the result is deterministic.

    Consequence: a dispatch-disabled blocker (not in `enabled`) is never in the
    surviving set, so every issue it blocks — transitively — is excluded. A
    dispatch-enabled issue blocked by a disabled one yields an empty queue.

    An issue that references a blocker OUTSIDE this fetch window
    (`graph.external_deps[n]`, e.g. a `depends on #N` whose #N is beyond the
    --limit, in another repo, or already closed) is treated as BLOCKED: the
    blocker is unresolved, so dispatching the dependent could bypass the very
    ordering the gate enforces. Conservative: never dispatchable while an
    unresolved external blocker stands.
    """
    external = getattr(graph, "external_deps", None) or {}
    candidate = {n for n in enabled if not external.get(n)}
    changed = True
    while changed:
        changed = False
        for n in sorted(candidate):
            if any(d not in candidate for d in graph.deps.get(n, [])):
                candidate.discard(n)
                changed = True
    return candidate


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def _classify(item: _intake.IntakeItem, python_bin: str) -> Dict[str, Any]:
    """Call classify.py as a subprocess. Returns {action, scope, route, confidence}."""
    try:
        result = proc.run(
            [python_bin, CLASSIFIER, "--title", item.title, "--body", item.body],
            check=True,
        )
        verdict = json.loads(result.stdout)
        _validate_classification_boundary(verdict, item.number)
        return verdict
    except proc.ProcError as exc:
        raise RuntimeError(
            f"classifier failed for #{item.number}: {(exc.stderr or '').strip()}"
        ) from exc


def _validate_classification_boundary(verdict: Dict[str, Any], number: Any) -> None:
    """Fail-soft classification.v1 boundary guard (#145).

    classify.py is a quarantine reader that must import nothing exec-capable, so
    the boundary is validated HERE — at the consumer-side reader — instead of in
    the producer. The raw verdict is the SAME dict minus ``schema_version``; we
    validate a stamped copy additively. Best-effort; never breaks the pipeline.
    """
    try:
        _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from src.orchestration import boundaries
    except Exception:
        return
    try:
        boundaries.warn_if_invalid(verdict, boundaries.CLASSIFICATION_SCHEMA,
                                   label=f"classification#{number}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Job request builder
# ---------------------------------------------------------------------------

def _job_id(item: _intake.IntakeItem) -> str:
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return f"issue-{item.number}-{ts}"


def _build_job_request(
    item: _intake.IntakeItem,
    triage: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "job_id":     _job_id(item),
        "issue":      item.number,
        "repo":       item.repository,
        "title":      item.title,
        "body":       item.body,
        "route":      triage["route"],
        "scope":      triage["scope"],
        "confidence": triage["confidence"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Intake → select → classify → job request(s).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Source
    src = p.add_mutually_exclusive_group()
    src.add_argument("--project", metavar="ORG/NUM",
                     help="GitHub ProjectV2 to intake from (e.g. ReclaimByDesign/5)")
    src.add_argument("--repo", metavar="OWNER/REPO",
                     help="GitHub repository to intake from")

    # Intake options
    p.add_argument("--label", metavar="LABEL",
                   default=os.environ.get("INTAKE_LABEL_FILTER", ""),
                   help="filter repo issues by label")
    p.add_argument("--limit", type=int,
                   default=int(os.environ.get("INTAKE_LIMIT", "50")),
                   help="max items to fetch (default: 50)")

    # Selection
    p.add_argument("--status", metavar="REGEX",
                   default=os.environ.get("INTAKE_STATUS_FILTER", ""),
                   help="project_status must match this regex (case-insensitive)")
    p.add_argument("--dispatch", metavar="VALUE",
                   default=os.environ.get("INTAKE_DISPATCH_FILTER", ""),
                   help="dispatch field must equal VALUE (* = any non-null)")
    p.add_argument("--hours-max", type=int,
                   default=int(os.environ["INTAKE_HOURS_MAX"]) if os.environ.get("INTAKE_HOURS_MAX") else None,
                   metavar="N",
                   help="skip issues with hours_estimate > N")
    p.add_argument("--confidence", type=float,
                   default=float(os.environ.get("PIPELINE_CONFIDENCE_THRESHOLD", "0.55")),
                   metavar="N",
                   help="minimum classifier confidence (default: 0.55)")

    # Provider
    p.add_argument("--refresh", action="store_true",
                   help="force a live fetch + re-save the issue snapshot instead of "
                        "reading the locally saved one (the one web-fetch path)")

    # Ranking
    p.add_argument("--rank", action="store_true",
                   help="rank items by dependency order before selection (uses LLM)")
    p.add_argument("--rank-model", default=os.environ.get("RANKER_MODEL", "haiku"),
                   metavar="ALIAS",
                   help="model alias for the ranker (default: haiku)")

    # Output
    p.add_argument("--all", action="store_true",
                   help="emit a JSON array of all eligible job requests (default: first only)")
    p.add_argument("--output", metavar="FILE",
                   help="write output to FILE instead of stdout")

    return p


def main(argv: List[str]) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    gh_bin     = os.environ.get("GH_BIN", "gh")
    python_bin = os.environ.get("PYTHON_BIN", sys.executable)

    # Resolve source
    project_str = args.project or os.environ.get("INTAKE_PROJECT", "")
    repo_str    = args.repo    or os.environ.get("INTAKE_REPO", "") or os.environ.get("PIPELINE_REPO", "")

    if not project_str and not repo_str:
        p.error("specify --project ORG/NUM or --repo OWNER/REPO "
                "(or set INTAKE_PROJECT / INTAKE_REPO)")

    if project_str:
        parts = project_str.split("/", 1)
        if len(parts) != 2 or not all(parts):
            p.error(f"--project must be ORG/NUM, got: {project_str!r}")

    # 1. Obtain issue information from the single provider (saved snapshot when
    #    available; one live fetch otherwise). The selector never calls gh itself.
    try:
        items = _intake.provide_items(
            project=project_str,
            repo=repo_str,
            label_filter=args.label,
            status_filter="",          # selection step handles status filtering
            limit=args.limit,
            gh_bin=gh_bin,
            refresh=args.refresh,
        )
    except RuntimeError as exc:
        print(f"pipeline: intake failed: {exc}", file=sys.stderr)
        return 1

    print(f"pipeline: fetched {len(items)} item(s)", file=sys.stderr)

    # 2. Rank (optional)
    if args.rank and items:
        try:
            items = [_intake.IntakeItem(**i) if not isinstance(i, _intake.IntakeItem) else i
                     for i in _ranker.rank(
                         [i.__dict__ if hasattr(i, '__dict__') else i for i in items],
                         model=args.rank_model,
                     )]
        except RuntimeError as exc:
            print(f"pipeline: ranker failed: {exc} — continuing unranked", file=sys.stderr)

    # 3. Select — per-item filters, then a local-DAG dependency gate.
    # First, the issues that pass per-item selection (status / dispatch / hours).
    enabled = {
        int(item.number) for item in items
        if _passes_selection(
            item,
            status_filter=args.status,
            dispatch_filter=args.dispatch,
            hours_max=args.hours_max,
        )
    }
    # Build the local dependency DAG over ALL fetched items (so a blocker is
    # visible even when it failed selection), then keep only issues whose every
    # blocker is itself dispatchable — deterministic greedy fixpoint. A
    # dispatch-disabled blocker therefore transitively excludes its dependents.
    graph = _dag.build([_item_dict(i) for i in items],
                       fwd=tuning.DEP_FWD, rev=tuning.DEP_REV)
    dispatchable = _dispatchable(enabled, graph)
    blocked = sorted(enabled - dispatchable)
    if blocked:
        print("pipeline: " + str(len(blocked)) + " enabled issue(s) excluded — "
              "blocked by a non-dispatchable dependency: "
              + ", ".join("#" + str(n) for n in blocked), file=sys.stderr)
    selected = [item for item in items if int(item.number) in dispatchable]
    # Deterministic priority order (foundational first, then the operator weights:
    # leaf-before-feat, milestone, iteration, label weight) so the emitted queue —
    # and the default first-only job request — is the highest-priority ready issue.
    selected.sort(key=lambda it: _ranker.priority_key(asdict(it), graph))
    print(f"pipeline: {len(selected)} item(s) passed selection", file=sys.stderr)

    if not selected:
        print("pipeline: no eligible issues found", file=sys.stderr)
        return 3

    # 4. Classify + build job requests
    job_requests = []
    for item in selected:
        if not item.repository:
            print(f"pipeline: skipping #{item.number} — no repository slug", file=sys.stderr)
            continue
        try:
            triage = _classify(item, python_bin)
        except RuntimeError as exc:
            print(f"pipeline: {exc} — skipping", file=sys.stderr)
            continue

        action     = triage.get("action", "")
        confidence = triage.get("confidence", 0.0)

        if action != "implement":
            print(f"pipeline: #{item.number} action={action!r} — skipping (not implementable)", file=sys.stderr)
            continue
        if confidence < args.confidence:
            print(f"pipeline: #{item.number} confidence={confidence:.2f} < {args.confidence:.2f} — skipping", file=sys.stderr)
            continue

        job_requests.append(_build_job_request(item, triage))
        print(f"pipeline: #{item.number} → route={triage['route']} scope={triage['scope']} conf={confidence:.2f}", file=sys.stderr)

    if not job_requests:
        print("pipeline: no job requests after classification", file=sys.stderr)
        return 3

    # 4. Emit
    if args.all:
        output = json.dumps(job_requests, ensure_ascii=False, indent=2)
    else:
        output = json.dumps(job_requests[0], ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output)
            fh.write("\n")
        print(f"pipeline: wrote {len(job_requests) if args.all else 1} job request(s) to {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
