#!/usr/bin/env python3
"""dispatch.py — the dispatch program. One invocation emits a finished work order.

Runs the full ARCHITECT pipeline that PLAY.md dramatizes, end to end:

    INTAKE (data sourcing)            services/intake/intake.py
      -> RANK (independence/blocking) services/intake/ranker.py
      -> CLASSIFY (triage)            services/classifier/classify.py
      -> APPROVE (work approval)      services/architect/approval.py   [Act II]
      -> ISSUE (work order)           services/architect/workorder.py  [Act III]
      -> stdout

Default: emit the work order for the most foundational *eligible* job — the
primary. `--all` emits one work order per eligible job, in ranked order.

Sources (choose one; fixture is the offline default)
----------------------------------------------------
  --fixture FILE      offline queue: JSON array of `gh issue list --json
                      number,title,body,labels,assignees,url` objects
  --repo OWNER/REPO   live repository issues (via gh)
  --project ORG/NUM   live ProjectV2 board (via gh)

Ranking is deterministic and offline by default (RANKER_OFFLINE). Pass
`--rank-llm` to resolve the ordering with a model instead.

Output
------
  default            the primary work order (plain text) to stdout
  --all              every eligible work order, ranked, divider-separated
  --json             machine envelope: {job fields, authorization, work_order}

Exit codes: 0 a work order emitted; 1 runtime error; 2 bad args;
            3 no eligible jobs in the queue.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                              # approval, workorder
sys.path.insert(0, os.path.join(_HERE, "../intake"))  # intake, ranker
sys.path.insert(0, os.path.join(_HERE, "../models"))  # models (ranker/--llm)

import intake as _intake          # noqa: E402
import ranker as _ranker          # noqa: E402
import approval as _approval      # noqa: E402
import resources as _resources    # noqa: E402
import decompose as _decompose    # noqa: E402
import workorder as _workorder    # noqa: E402

_CLASSIFIER = os.path.join(_HERE, "../classifier/classify.py")
_DIVIDER = "\n\n" + ("─" * 72) + "\n\n"


def _classify(item: Dict[str, Any], python_bin: str) -> Dict[str, Any]:
    """Run the deterministic classifier as a subprocess (quarantine reader)."""
    result = subprocess.run(
        [python_bin, _CLASSIFIER, "--title", item.get("title", ""),
         "--body", item.get("body") or ""],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _fetch(args: argparse.Namespace, gh_bin: str) -> List[Dict[str, Any]]:
    repo_slug = (args.repo or os.environ.get("INTAKE_REPO")
                 or os.environ.get("PIPELINE_REPO") or "ReclaimByDesign/dispatch")
    if args.project:
        org, _, num = args.project.partition("/")
        if not org or not num:
            raise ValueError(f"--project must be ORG/NUM, got {args.project!r}")
        items = _intake.intake_from_project(
            org, num, limit=args.limit,
            fixture_path=os.environ.get("INTAKE_FIXTURE_PROJECT", ""), gh_bin=gh_bin)
    else:
        items = _intake.intake_from_repo(
            repo_slug, label_filter=args.label, limit=args.limit,
            fixture_path=args.fixture or os.environ.get("INTAKE_FIXTURE_REPO", ""),
            gh_bin=gh_bin)
    return [asdict(i) for i in items]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Emit a finished work order for the queue's foundational job.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    src = p.add_mutually_exclusive_group()
    src.add_argument("--fixture", metavar="FILE", help="offline queue JSON (repo-issue array)")
    src.add_argument("--repo", metavar="OWNER/REPO", help="live repository issues")
    src.add_argument("--project", metavar="ORG/NUM", help="live ProjectV2 board")
    p.add_argument("--label", default=os.environ.get("INTAKE_LABEL_FILTER", ""),
                   help="filter repo issues by label")
    p.add_argument("--limit", type=int, default=int(os.environ.get("INTAKE_LIMIT", "50")))
    p.add_argument("--confidence", type=float,
                   default=float(os.environ.get("PIPELINE_CONFIDENCE_THRESHOLD", "0.55")),
                   metavar="N", help="minimum classifier confidence (default: 0.55)")
    p.add_argument("--rank-llm", action="store_true",
                   help="rank with a model instead of the deterministic offline parse")
    p.add_argument("--rank-model", default=os.environ.get("RANKER_MODEL", "haiku"))
    p.add_argument("--llm", action="store_true",
                   help="have ARCHITECT sharpen the work order via a model (opt-in)")
    p.add_argument("--all", action="store_true",
                   help="emit a work order for every eligible job, in ranked order")
    p.add_argument("--json", action="store_true",
                   help="emit a machine envelope instead of plain work-order text")
    p.add_argument("--verify-cmd", default=os.environ.get("DISPATCH_VERIFY_CMD",
                   "bash scripts/smoke.sh"), metavar="CMD",
                   help="the gate the Engineer must make green (default: bash scripts/smoke.sh)")
    return p


def main(argv: List[str]) -> int:
    args = _build_parser().parse_args(argv)
    gh_bin = os.environ.get("GH_BIN", "gh")
    python_bin = os.environ.get("PYTHON_BIN", sys.executable)

    if not args.rank_llm:
        os.environ["RANKER_OFFLINE"] = "1"   # deterministic by default

    if not (args.fixture or args.repo or args.project
            or os.environ.get("INTAKE_REPO") or os.environ.get("PIPELINE_REPO")):
        print("dispatch: specify --fixture FILE, --repo OWNER/REPO, or --project ORG/NUM",
              file=sys.stderr)
        return 2

    # 1. INTAKE
    try:
        items = _fetch(args, gh_bin)
    except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"dispatch: intake failed: {exc}", file=sys.stderr)
        return 1
    if not items:
        print("dispatch: queue is empty", file=sys.stderr)
        return 3
    print(f"dispatch: intake fetched {len(items)} item(s)", file=sys.stderr)

    # 2. RANK (independence / blocking)
    try:
        ranked = _ranker.rank(items, model=args.rank_model)
    except RuntimeError as exc:
        print(f"dispatch: ranker failed ({exc}); continuing unranked", file=sys.stderr)
        ranked = items

    # 3. CLASSIFY + select
    dry_run = os.environ.get("PIPELINE_DRY_RUN", "1") != "0"
    eligible: List[Dict[str, Any]] = []
    print("── ADMIN: queue ranked by independence/blocking ──", file=sys.stderr)
    for pos, item in enumerate(ranked, 1):
        n = item["number"]
        try:
            triage = _classify(item, python_bin)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            print(f"  {pos}. #{n}: classify failed ({exc}) — skipping", file=sys.stderr)
            continue
        action, conf = triage.get("action"), triage.get("confidence", 0.0)
        ok = action == "implement" and conf >= args.confidence
        reason = "eligible" if ok else (
            f"skip ({action})" if action != "implement" else f"skip (conf {conf} < {args.confidence})")
        print(f"  {pos}. #{n} [{triage.get('scope')}/{triage.get('route')} "
              f"conf {conf}] {action} — {reason}: {item.get('title','')}", file=sys.stderr)
        if ok:
            eligible.append({"item": item, "triage": triage})

    if not eligible:
        print("dispatch: no eligible jobs (need action=implement and confidence >= "
              f"{args.confidence})", file=sys.stderr)
        return 3

    chosen = eligible if args.all else eligible[:1]

    # 4. APPROVE + 5. ISSUE (with embedded resources + decomposition/staffing)
    repo_root = os.environ.get("PIPELINE_ROOT") or os.getcwd()
    envelopes, texts = [], []
    for e in chosen:
        item, triage = e["item"], e["triage"]
        n = item["number"]
        job = {
            "job_id": f"dispatch-issue-{n}", "issue": n, "repo": item.get("repository", ""),
            "title": item.get("title", ""), "body": item.get("body", ""),
            "route": triage["route"], "scope": triage["scope"], "confidence": triage["confidence"],
        }
        res = _resources.gather(job, repo_root)
        wplan = _decompose.plan(job, res["discovered"], verify_cmd=args.verify_cmd)
        auth = _approval.approve(job, triage, dry_run=dry_run)
        text = _workorder.render(job, triage, auth, resources=res, plan=wplan,
                                 llm=args.llm, verify_cmd=args.verify_cmd)
        texts.append(text)
        envelopes.append({**job, "authorization": auth.to_dict(),
                          "units": wplan["units"], "staffing": wplan["staffing"],
                          "work_order": text})

    primary = chosen[0]["item"]["number"]
    print(f"── ARCHITECT: primary #{primary} "
          f"(scope {chosen[0]['triage']['scope']}, route {chosen[0]['triage']['route']}, "
          f"budget {envelopes[0]['authorization']['budget_tokens']:,} tok) ──", file=sys.stderr)

    # 6. EMIT
    if args.json:
        print(json.dumps(envelopes if args.all else envelopes[0], ensure_ascii=False, indent=2))
    else:
        print(_DIVIDER.join(texts) if args.all else texts[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
