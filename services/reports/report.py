#!/usr/bin/env python3
"""report.py — operator digest renderer for the run-ledger (E4-2, #37).

Reads the append-only JSONL run-ledger (E4-1, services/ledger/ledger.py) and
renders a "where is everything right now" rollup to stdout: recent ticks, the
issues each touched with their latest stage and RAW per-issue token/duration
sums (attribution only — NOT reconciled against any usage cap; that is E5), and
a count of issues by current stage. Read-only and fully offline: given a fixture
ledger it renders deterministically with no GitHub or model calls.

Ledger source (override order):
  --ledger PATH  >  $DISPATCH_LEDGER_FILE  >
  ${DISPATCH_ARTIFACTS_DIR:-./.artifacts}/run-ledger.jsonl

Flags:
  --format markdown|text   output shape (default markdown)
  --limit N                show only the N most recent ticks (by max timestamp)
  --comment                print the would-be GitHub issue-comment body under
                           dry-run and exit — a post-1.0 stub; never calls gh

Exit codes: 0 always — an empty/missing ledger renders a 'no runs recorded'
digest and still exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional


def _int(v: Any) -> int:
    if isinstance(v, bool) or v in (None, ""):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _num(v: Any):
    if isinstance(v, bool) or v in (None, ""):
        return 0
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return 0


def ledger_path(override: str = "") -> str:
    if override:
        return override
    env = os.environ.get("DISPATCH_LEDGER_FILE")
    if env:
        return env
    base = os.environ.get("DISPATCH_ARTIFACTS_DIR") or "./.artifacts"
    return os.path.join(base, "run-ledger.jsonl")


def load_ledger(path: str) -> List[Dict[str, Any]]:
    """Read JSONL ledger lines; a missing file or blank/bad lines yield []."""
    lines: List[Dict[str, Any]] = []
    if not path or not os.path.exists(path):
        return lines
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                lines.append(rec)
    return lines


def _tokens(rec: Dict[str, Any]) -> int:
    cost = rec.get("cost") or {}
    return _int(cost.get("tokens_in")) + _int(cost.get("tokens_out"))


def summarize(lines: List[Dict[str, Any]], *, limit: Optional[int] = None) -> Dict[str, Any]:
    """Build the deterministic digest model from ledger lines.

    Groups by tick_id (each tick keyed by its max line timestamp); within a tick,
    each issue carries its latest stage/label_after and summed tokens/duration.
    --limit keeps the N most recent ticks. The summary counts distinct issues by
    their CURRENT (global-latest) stage.
    """
    ordered_lines = sorted(lines, key=lambda r: (str(r.get("timestamp") or ""), _int(r.get("issue"))))

    # Tick max-timestamp, to pick the N most recent.
    tick_ts: Dict[str, str] = {}
    for r in ordered_lines:
        tid = str(r.get("tick_id") or "tick-unknown")
        ts = str(r.get("timestamp") or "")
        if ts >= tick_ts.get(tid, ""):
            tick_ts[tid] = ts
    kept = sorted(tick_ts, key=lambda t: (tick_ts[t], t))
    if limit is not None and limit >= 0:
        kept = kept[-limit:] if limit else []
    kept_set = set(kept)

    ticks: Dict[str, Dict[str, Any]] = {t: {"tick_id": t, "ts": tick_ts[t], "issues": {}} for t in kept}
    issue_current: Dict[int, Dict[str, str]] = {}
    for r in ordered_lines:
        tid = str(r.get("tick_id") or "tick-unknown")
        if tid not in kept_set:
            continue
        issue = r.get("issue")
        ts = str(r.get("timestamp") or "")
        if issue is None:
            continue
        per = ticks[tid]["issues"].setdefault(
            issue, {"issue": issue, "stage": "", "label": "", "ts": "", "tokens": 0, "duration": 0})
        per["tokens"] += _tokens(r)
        per["duration"] += _num(r.get("cost", {}).get("duration_seconds") if isinstance(r.get("cost"), dict) else None)
        if ts >= per["ts"]:
            per["ts"] = ts
            per["stage"] = str(r.get("stage") or "")
            per["label"] = str(r.get("label_after") or "")
        cur = issue_current.setdefault(issue, {"stage": "", "ts": ""})
        if ts >= cur["ts"]:
            cur["ts"] = ts
            cur["stage"] = str(r.get("stage") or "")

    stage_counts: Dict[str, int] = {}
    for cur in issue_current.values():
        stage_counts[cur["stage"]] = stage_counts.get(cur["stage"], 0) + 1

    return {
        "ticks": [ticks[t] for t in kept],
        "n_ticks": len(kept),
        "distinct_issues": len(issue_current),
        "stage_counts": stage_counts,
    }


def _stage_count_line(stage_counts: Dict[str, int]) -> str:
    if not stage_counts:
        return "(none)"
    return ", ".join(f"{s}: {n}" for s, n in sorted(stage_counts.items()))


def render(model: Dict[str, Any], fmt: str = "markdown") -> str:
    if model["n_ticks"] == 0:
        if fmt == "text":
            return "Dispatch run digest\nno runs recorded\n"
        return "# Dispatch run digest\n\n_no runs recorded_\n"

    summary = (f"{model['n_ticks']} tick(s) · {model['distinct_issues']} distinct issue(s) · "
               f"issues by current stage: {_stage_count_line(model['stage_counts'])}")

    out: List[str] = []
    if fmt == "text":
        out.append("Dispatch run digest")
        out.append(summary)
        out.append("")
        for tick in model["ticks"]:
            out.append(f"tick {tick['tick_id']} ({tick['ts']})")
            for issue in sorted(tick["issues"].values(), key=lambda i: _int(i["issue"])):
                out.append(f"  #{issue['issue']:<6} stage={issue['stage']:<16} "
                           f"tokens={issue['tokens']:<8} duration_s={issue['duration']}")
            out.append("")
        return "\n".join(out).rstrip() + "\n"

    # markdown (default)
    out.append("# Dispatch run digest")
    out.append("")
    out.append(f"**Summary:** {summary}")
    out.append("")
    for tick in model["ticks"]:
        out.append(f"## tick `{tick['tick_id']}` — {tick['ts']}")
        out.append("")
        out.append("| issue | stage | label | tokens | duration (s) |")
        out.append("|------:|-------|-------|-------:|-------------:|")
        for issue in sorted(tick["issues"].values(), key=lambda i: _int(i["issue"])):
            out.append(f"| #{issue['issue']} | {issue['stage']} | {issue['label']} "
                       f"| {issue['tokens']} | {issue['duration']} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dispatch report",
        description="Render an operator digest from the JSONL run-ledger (offline, read-only).")
    p.add_argument("--ledger", default="", metavar="PATH", help="ledger file (else env / default)")
    p.add_argument("--format", choices=("markdown", "text"), default="markdown",
                   help="output shape (default markdown)")
    p.add_argument("--limit", type=int, default=None, metavar="N",
                   help="show only the N most recent ticks")
    p.add_argument("--comment", action="store_true",
                   help="print the would-be GitHub issue-comment body (dry-run stub; never calls gh)")
    return p


def main(argv: List[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    path = ledger_path(args.ledger)
    lines = load_ledger(path)
    model = summarize(lines, limit=args.limit)
    body = render(model, fmt=args.format)

    if args.comment:
        # Post-1.0 stub: show the comment we WOULD post; never calls gh.
        sys.stdout.write("DRY-RUN: would post GitHub issue comment (digest):\n")
        sys.stdout.write(body)
        return 0

    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
