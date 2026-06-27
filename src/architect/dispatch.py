#!/usr/bin/env python3
"""dispatch.py — the dispatch program. One invocation emits a finished work order.

Runs the full ARCHITECT pipeline that PLAY.md dramatizes, end to end:

    INTAKE (data sourcing)            src/intake/intake.py
      -> RANK (independence/blocking) src/intake/ranker.py
      -> CLASSIFY (triage)            src/classifier/classify.py
      -> APPROVE (work approval)      src/architect/approval.py   [Act II]
      -> ISSUE (work order)           src/architect/workorder.py  [Act III]
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

An [Epic] is a container, not a unit of work: it is never selected as the
primary or by `--all`. Pass `--issue <epic>` to decompose one into a work order
per constituent child (children declare membership via `Parent epic: #N`).

Output
------
  default            the primary work order (plain text) to stdout
  --all              every eligible work order, ranked, divider-separated
  --issue N          the work order for issue N — or, if N is an [Epic], one per
                     constituent child issue (decomposition)
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
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                              # approval, workorder
sys.path.insert(0, os.path.join(_HERE, "../intake"))  # intake, ranker
sys.path.insert(0, os.path.dirname(_HERE))            # src (tuning)
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))  # repo root for the engine package

import intake as _intake          # noqa: E402
import ranker as _ranker          # noqa: E402
from engine import structures as _dag    # noqa: E402
import approval as _approval      # noqa: E402
import resources as _resources    # noqa: E402
import decompose as _decompose    # noqa: E402
import workorder as _workorder    # noqa: E402
import characteristics as _characteristics  # noqa: E402
import research as _research      # noqa: E402
import verify as _verify          # noqa: E402
import tuning                     # noqa: E402

_CLASSIFIER = os.path.join(_HERE, "../classifier/classify.py")
_DIVIDER = "\n\n" + ("─" * 72) + "\n\n"
# Per-run cache so the verify-gate probe hits each target repo at most once.
_VERIFY_CACHE: Dict[str, str] = {}

# --- Per-stage artifact dump (E2-1) ----------------------------------------
# When DISPATCH_ARTIFACTS_DIR is set, a tick writes its intermediate state
# (rendered work order, Job Request) under <DISPATCH_ARTIFACTS_DIR>/<tick-id>/
# so the operator has a complete, inspectable record and the stage-gating
# (E2-2) / replay (E2-3) work has a substrate to read. Dumping is a pure
# side-effect: gated on DISPATCH_ARTIFACTS_DIR, it never changes stdout or
# GitHub state, and runs under PIPELINE_DRY_RUN=1 too (observability, not a
# mutation). The DAG dump (--dag) keeps writing at the artifacts-dir top level.
# Seam contract version (schemas/work-order.v1.json, job-request.v1.json,
# invoice.v1.json). Every artifact the Architect emits across the architect↔worker
# seam carries `schema_version` so the harness + worker units can build/test against
# the frozen contract while the Architect internals evolve behind WorkOrder v1.
SCHEMA_VERSION = 1
_JOB_FIELDS = ("job_id", "issue", "repo", "title", "body", "route", "scope", "confidence")


def _tick_id() -> str:
    """Stable id for this tick: a pipeline-threaded DISPATCH_TICK_ID if present,
    else a fresh tick-<UTC> stamp."""
    tid = os.environ.get("DISPATCH_TICK_ID")
    if tid:
        return tid
    import datetime
    return "tick-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _tick_artifacts_dir() -> "str | None":
    """The per-tick artifact directory (created on demand). None when dumping is off."""
    base = os.environ.get("DISPATCH_ARTIFACTS_DIR")
    if not base:
        return None
    d = os.path.join(base, _tick_id())
    os.makedirs(d, exist_ok=True)
    return d


def _dump_stage_artifacts(text: str, envelope: Dict[str, Any]) -> None:
    """Write workorder.txt + job-request.json for the primary job (no-op if off).

    job-request.json carries exactly the schemas/job-request.json fields — the
    same Job Request shape the Architect hands the Engineer."""
    d = _tick_artifacts_dir()
    if d is None:
        return
    with open(os.path.join(d, "workorder.txt"), "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    # The Job Request is the strict subset/projection of the Work Order: the job
    # fields, stamped with the same schema_version (schemas/job-request.v1.json).
    job = {"schema_version": SCHEMA_VERSION, **{k: envelope[k] for k in _JOB_FIELDS}}
    with open(os.path.join(d, "job-request.json"), "w", encoding="utf-8") as fh:
        json.dump(job, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _classify(item: Dict[str, Any], python_bin: str) -> Dict[str, Any]:
    """Run the deterministic classifier as a subprocess (quarantine reader)."""
    result = subprocess.run(
        [python_bin, _CLASSIFIER, "--title", item.get("title", ""),
         "--body", item.get("body") or ""],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def _fetch(args: argparse.Namespace, gh_bin: str) -> List[Dict[str, Any]]:
    # All selectors obtain issue information from the single provider
    # (intake.provide_items): a locally saved snapshot when present, one live
    # fetch otherwise. This selector never calls gh directly. The local DAG is
    # then built from this saved information downstream (_graph_and_urls).
    repo_slug = (args.repo or os.environ.get("INTAKE_REPO")
                 or os.environ.get("PIPELINE_REPO") or "ReclaimByDesign/dispatch")
    convo_fx = os.environ.get("INTAKE_FIXTURE_CONVERSATION", "")  # #132 offline seam
    if args.project:
        org, _, num = args.project.partition("/")
        if not org or not num:
            raise ValueError(f"--project must be ORG/NUM, got {args.project!r}")
    # dispatch.py's --fixture is its single offline seam. It must reach the
    # fixture argument that matches the chosen source: the project-item seam in
    # --project mode, the repo-issue seam otherwise. Wiring it only to
    # fixture_repo would make an offline --fixture silently dead under --project
    # (repo is "") and fall through to a live gh fetch on a cold cache.
    items = _intake.provide_items(
        project=args.project or "",
        repo="" if args.project else repo_slug,
        label_filter=args.label,
        limit=args.limit,
        gh_bin=gh_bin,
        refresh=getattr(args, "refresh", False),
        fixture_project=args.fixture or "" if args.project else "",
        fixture_repo="" if args.project else (args.fixture or ""),
        # Ground the work order with each issue's comment thread + relevant
        # history (#132). A live gh fetch is only ever attempted for a LIVE source
        # (--repo/--project) or when an INTAKE_FIXTURE_CONVERSATION fixture is
        # supplied; an offline --fixture queue with no conversation fixture stays
        # offline (empty thread) so the smoke + demo harnesses make no live gh
        # call — byte-identical to pre-#132 orders.
        with_conversation=bool(args.repo or args.project) or bool(convo_fx),
        fixture_conversation=convo_fx,
    )
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
    p.add_argument("--refresh", action="store_true",
                   help="force a live fetch + re-save the issue snapshot instead of "
                        "reading the locally saved one (the one web-fetch path)")
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
    p.add_argument("--dag", action="store_true",
                   help="write a static issue DAG (.claude/artifacts/issue-dag.{json,md}) for "
                        "browsing/selection instead of emitting a work order. Artifact dir "
                        "overridable via DISPATCH_ARTIFACTS_DIR")
    p.add_argument("--issue", type=int, metavar="N",
                   help="emit the work order for issue N chosen from the DAG (operator override: "
                        "bypasses the eligibility filter; notes unmet dependencies). If N is an "
                        "[Epic], emit a work order for each of its constituent child issues.")
    p.add_argument("--json", action="store_true",
                   help="emit a machine envelope instead of plain work-order text")
    p.add_argument("--verify-cmd", default=None, metavar="CMD",
                   help="override the gate the Engineer must make green. Default: auto-detect "
                        "from the TARGET repo (scripts/smoke.sh, then npm/make), else a generic "
                        "instruction. The DISPATCH_VERIFY_CMD env var also overrides.")
    return p


def _graph_and_urls(ranked: List[Dict[str, Any]]):
    """Build the queue DAG once and the number->url map. Shared by every caller
    so the graph is built once per invocation, not once per issue (#35). The DAG
    is built offline/deterministically — same call as --dag (D3, no web)."""
    graph = _dag.build(ranked, fwd=tuning.DEP_FWD, rev=tuning.DEP_REV)
    url_by_num = {int(it["number"]): (it.get("source_url") or "") for it in ranked}
    return graph, url_by_num


def _dep_info(graph, n: int, url_by_num: Dict[int, str]) -> Dict[str, Any]:
    """This issue's dependency edges as (number, url) pairs (sorted, deterministic):
    `blocked_by` = issues it depends on (graph.deps[n]); `blocks` = the reverse
    edges (issues that depend on it)."""
    blocked_by = [(d, url_by_num.get(d, "")) for d in graph.deps.get(n, [])]
    blocks = [(m, url_by_num.get(m, "")) for m in graph.numbers if n in graph.deps.get(m, [])]
    return {"blocked_by": blocked_by, "blocks": blocks}


def _workorder_for(item: Dict[str, Any], triage: Dict[str, Any],
                   args: argparse.Namespace, *, dry_run: bool, repo_root: str,
                   graph=None, url_by_num: Optional[Dict[int, str]] = None):
    """Approve + gather resources + decompose + render one job's work order.

    Returns (text, envelope). Shared by the default/--all dispatch path and the
    --issue operator-selection path so both emit byte-identical work orders.

    When `graph`/`url_by_num` are provided, a DEPENDENCIES section + the issue URL
    are surfaced (#35); with `graph=None` the work order renders exactly as before.
    """
    n = item["number"]
    repo_slug = item.get("repository", "")
    # The verify gate is the TARGET repo's, not the dispatch repo's. Live sources
    # (--repo/--project) are probed via gh; the offline fixture/self path reads
    # the local tree. An explicit --verify-cmd / DISPATCH_VERIFY_CMD wins.
    verify_cmd = _verify.resolve_verify_cmd(
        repo_slug=repo_slug, repo_root=repo_root,
        override=args.verify_cmd or os.environ.get("DISPATCH_VERIFY_CMD"),
        live=bool(args.repo or args.project),
        gh_bin=os.environ.get("GH_BIN", "gh"), cache=_VERIFY_CACHE)
    job = {
        "job_id": f"dispatch-issue-{n}", "issue": n, "repo": repo_slug,
        "title": item.get("title", ""), "body": item.get("body", ""),
        "route": triage["route"], "scope": triage["scope"], "confidence": triage["confidence"],
    }
    # E6-3 (#56): the committed research artifact for this job's topic, if any.
    # When present it is embedded as the leading resource AND fills the research
    # gap (the issue dispatches as implementation, not another research order).
    research_rel = "docs/research/%s.md" % _research.topic_for({**job, "number": n})
    research_present = _resources.has_file(repo_root, research_rel)
    res = _resources.gather(job, repo_root,
                            research_rel=research_rel if research_present else None,
                            conversation=item.get("conversation") or [],
                            history=item.get("history") or [])
    wplan = _decompose.plan(job, res["discovered"], verify_cmd=verify_cmd)
    # Research-mode (E6-1 verdict → E6-2 order). detect_gap needs the issue labels
    # (the job dict carries none) and the classifier confidence (already in triage).
    # Actuation is gated by generation.research.dispatch_enabled (default off): when
    # off, every order is implementation — byte-identical to today — so the heuristic
    # never leaks into the existing dispatch path. When on, a gapped issue renders a
    # research order on the cheap research route.
    gap = _research.detect_gap(
        {**job, "number": n, "labels": item.get("labels", [])}, res["discovered"],
        research_present=research_present)
    mode = ("research" if (tuning.RESEARCH.get("dispatch_enabled", False)
                           and gap["needs_research"]) else "implementation")
    if mode == "research":
        research_route = tuning.RESEARCH.get("route", "gen-default")
        triage = {**triage, "route": research_route}
        job = {**job, "route": research_route}
    auth = _approval.approve(job, triage, dry_run=dry_run)
    issue_url = item.get("source_url") or ""
    dag = (_dep_info(graph, int(n), url_by_num or {})
           if graph is not None else None)
    # The three work-plan characteristics (strategy/purpose/issues_affected),
    # derived from the plan + issue labels + DAG. Rules/content are data-driven
    # (app/config/workplan-rules.yml). `resolves` defaults to the single primary
    # issue; an aggregating caller can fold sibling ids here.
    _labels = [(l.get("name") if isinstance(l, dict) else l)
               for l in (item.get("labels") or [])]
    chars = _characteristics.build(job, triage, wplan, labels=_labels, dag=dag)
    text = _workorder.render(job, triage, auth, resources=res, plan=wplan,
                             llm=args.llm, verify_cmd=verify_cmd,
                             dag=dag, issue_url=issue_url,
                             mode=mode, topic=gap["topic"],
                             characteristics=chars)
    envelope = {"schema_version": SCHEMA_VERSION, **job, "issue_url": issue_url,
                "authorization": auth.to_dict(),
                "units": wplan["units"], "staffing": wplan["staffing"],
                "characteristics": chars,
                "mode": mode, "work_order": text}
    if dag is not None:
        # Machine view of the same edges: [{number, url}, ...], sorted.
        envelope["blocked_by"] = [{"number": d, "url": u} for d, u in dag["blocked_by"]]
        envelope["blocks"] = [{"number": d, "url": u} for d, u in dag["blocks"]]
    return text, envelope


def _source_label(args: argparse.Namespace) -> str:
    """Deterministic descriptor of where the queue came from (no absolute paths)."""
    if args.project:
        return f"project:{args.project}"
    if args.repo:
        return f"repo:{args.repo}"
    if args.fixture:
        return f"fixture:{os.path.basename(args.fixture)}"
    return os.environ.get("INTAKE_REPO") or os.environ.get("PIPELINE_REPO") or "queue"


def _eligibility_map(ranked: List[Dict[str, Any]], python_bin: str,
                     confidence: float) -> Dict[int, str]:
    """Classify each item; return {number: short eligibility note} for the DAG view."""
    out: Dict[int, str] = {}
    for item in ranked:
        n = int(item["number"])
        try:
            triage = _classify(item, python_bin)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            out[n] = "classify-error"
            continue
        action, conf = triage.get("action"), triage.get("confidence", 0.0)
        if action == "implement" and conf >= confidence:
            out[n] = "eligible"
        elif action == "decompose":
            out[n] = "decompose (epic)"
        elif action != "implement":
            out[n] = f"skip ({action})"
        else:
            out[n] = f"defer (conf {conf} < {confidence})"
    return out


def _run_dag(args: argparse.Namespace, ranked: List[Dict[str, Any]],
             python_bin: str) -> int:
    """Build the issue DAG and persist it as a static, browsable artifact."""
    graph = _dag.build(ranked, fwd=tuning.DEP_FWD, rev=tuning.DEP_REV)
    source = _source_label(args)
    envelope = _dag.to_json(graph, source=source)
    elig = _eligibility_map(ranked, python_bin, args.confidence)

    md = "\n".join([
        f"# Issue DAG — {source}",
        "",
        "Static dependency view of the queue. Pick a **root** (no unmet "
        "dependencies) and dispatch it directly with `./dispatch --issue <N>` — "
        "no need to resolve the ordering in conversation.",
        "",
        "- Roots (ready to start): " + (", ".join(f"#{n}" for n in graph.roots) or "—"),
        f"- Issues: {len(graph.numbers)} · edges: {len(envelope['edges'])}"
        + (" · cycles: " + ", ".join(f"#{n}" for n in graph.cycles) if graph.cycles else ""),
        "",
        "## Dependency graph",
        "",
        _dag.render_mermaid(graph),
        "",
        "## Issues",
        "",
        _dag.render_markdown_table(graph, eligibility=elig),
        "",
    ])

    artifacts_dir = (os.environ.get("DISPATCH_ARTIFACTS_DIR")
                     or os.path.join(os.environ.get("PIPELINE_ROOT") or os.getcwd(),
                                     ".claude", "artifacts"))
    os.makedirs(artifacts_dir, exist_ok=True)
    json_path = os.path.join(artifacts_dir, "issue-dag.json")
    md_path = os.path.join(artifacts_dir, "issue-dag.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)

    print(f"── ADMIN: issue DAG — {len(graph.numbers)} issue(s), roots "
          f"{', '.join('#'+str(n) for n in graph.roots) or '—'} ──", file=sys.stderr)
    print(json_path)
    print(md_path)
    return 0


def _decompose_epic(args: argparse.Namespace, ranked: List[Dict[str, Any]],
                    epic: Dict[str, Any], python_bin: str) -> int:
    """Decompose an [Epic] into its constituents: one work order per implementable
    child. Children declare membership with `Parent epic: #N`, which the dependency
    graph resolves as deps[epic]; they are emitted in ranked (foundational) order."""
    target = int(epic["number"])
    graph, url_by_num = _graph_and_urls(ranked)
    child_nums = set(graph.deps.get(target, []))
    children = [it for it in ranked if int(it["number"]) in child_nums]
    print(f"── ADMIN: decompose epic #{target} → {len(children)} constituent "
          f"issue(s): {', '.join('#'+str(int(c['number'])) for c in children) or '—'} ──",
          file=sys.stderr)
    if not children:
        print(f"dispatch: epic #{target} has no constituent issues in the queue "
              f"(a child declares membership with 'Parent epic: #{target}' in its body); "
              f"nothing to decompose", file=sys.stderr)
        return 3

    dry_run = os.environ.get("PIPELINE_DRY_RUN", "1") != "0"
    repo_root = os.environ.get("PIPELINE_ROOT") or os.getcwd()
    texts, envelopes = [], []
    for child in children:
        c = int(child["number"])
        try:
            ctri = _classify(child, python_bin)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            print(f"   #{c}: classify failed ({exc}) — skipping", file=sys.stderr)
            continue
        if ctri.get("action") != "implement":
            print(f"   #{c}: {ctri.get('action')} — not implementable, skipping",
                  file=sys.stderr)
            continue
        text, envelope = _workorder_for(child, ctri, args, dry_run=dry_run,
                                        repo_root=repo_root,
                                        graph=graph, url_by_num=url_by_num)
        texts.append(text)
        envelopes.append(envelope)

    if not texts:
        print(f"dispatch: epic #{target} has no implementable constituent issues",
              file=sys.stderr)
        return 3

    _dump_stage_artifacts(texts[0], envelopes[0])
    print(f"── ARCHITECT: {len(texts)} work order(s) from epic #{target} ──",
          file=sys.stderr)
    if args.json:
        print(json.dumps(envelopes, ensure_ascii=False, indent=2))
    else:
        print(_DIVIDER.join(texts))
    return 0


def _run_issue(args: argparse.Namespace, ranked: List[Dict[str, Any]],
               python_bin: str) -> int:
    """Emit the work order for an operator-selected issue (eligibility overridden).

    If the selection is an [Epic], decompose it into one work order per child."""
    target = int(args.issue)
    item = next((it for it in ranked if int(it["number"]) == target), None)
    if item is None:
        print(f"dispatch: issue #{target} is not in the queue", file=sys.stderr)
        return 3
    try:
        triage = _classify(item, python_bin)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"dispatch: classify failed for #{target} ({exc})", file=sys.stderr)
        return 1

    if triage.get("action") == "decompose":
        return _decompose_epic(args, ranked, item, python_bin)

    action, conf = triage.get("action"), triage.get("confidence", 0.0)
    note = ("eligible" if action == "implement" and conf >= args.confidence
            else f"OPERATOR OVERRIDE ({action})")
    print(f"── ADMIN: dispatching #{target} by operator selection "
          f"[{triage.get('scope')}/{triage.get('route')} conf {conf}] {action} — {note} ──",
          file=sys.stderr)
    graph, url_by_num = _graph_and_urls(ranked)
    deps = graph.deps.get(target, [])
    if deps:
        print(f"   note: #{target} depends on " + ", ".join(f"#{d}" for d in deps)
              + " — ensure those are done first", file=sys.stderr)

    dry_run = os.environ.get("PIPELINE_DRY_RUN", "1") != "0"
    repo_root = os.environ.get("PIPELINE_ROOT") or os.getcwd()
    text, envelope = _workorder_for(item, triage, args, dry_run=dry_run, repo_root=repo_root,
                                    graph=graph, url_by_num=url_by_num)
    _dump_stage_artifacts(text, envelope)
    if args.json:
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


def _run_dispatch(args: argparse.Namespace, ranked: List[Dict[str, Any]],
                  python_bin: str) -> int:
    """Default path: select eligible job(s) and emit the primary (or --all) work order."""
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
        if ok:
            reason = "eligible"
        elif action == "decompose":
            reason = f"decompose (epic — emit children via --issue {n} or --all)"
        elif action != "implement":
            reason = f"skip ({action})"
        else:
            reason = f"skip (conf {conf} < {args.confidence})"
        print(f"  {pos}. #{n} [{triage.get('scope')}/{triage.get('route')} "
              f"conf {conf}] {action} — {reason}: {item.get('title','')}", file=sys.stderr)
        if ok:
            eligible.append({"item": item, "triage": triage})

    if not eligible:
        print("dispatch: no eligible jobs (need action=implement and confidence >= "
              f"{args.confidence})", file=sys.stderr)
        return 3

    chosen = eligible if args.all else eligible[:1]

    # APPROVE + ISSUE (with embedded resources + decomposition/staffing)
    repo_root = os.environ.get("PIPELINE_ROOT") or os.getcwd()
    graph, url_by_num = _graph_and_urls(ranked)
    envelopes, texts = [], []
    for e in chosen:
        text, envelope = _workorder_for(e["item"], e["triage"], args,
                                        dry_run=dry_run, repo_root=repo_root,
                                        graph=graph, url_by_num=url_by_num)
        texts.append(text)
        envelopes.append(envelope)

    _dump_stage_artifacts(texts[0], envelopes[0])
    primary = chosen[0]["item"]["number"]
    print(f"── ARCHITECT: primary #{primary} "
          f"(scope {chosen[0]['triage']['scope']}, route {chosen[0]['triage']['route']}, "
          f"budget {envelopes[0]['authorization']['budget_tokens']:,} tok) ──", file=sys.stderr)

    if args.json:
        print(json.dumps(envelopes if args.all else envelopes[0], ensure_ascii=False, indent=2))
    else:
        print(_DIVIDER.join(texts) if args.all else texts[0])
    return 0


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

    # 3. Branch by mode: --dag writes the static artifact; --issue N dispatches an
    #    operator-chosen issue; default/--all emits the primary/every work order.
    if args.dag:
        return _run_dag(args, ranked, python_bin)
    if args.issue is not None:
        return _run_issue(args, ranked, python_bin)
    return _run_dispatch(args, ranked, python_bin)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
