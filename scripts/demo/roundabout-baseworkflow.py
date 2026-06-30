#!/usr/bin/env python3
"""roundabout-baseworkflow.py — run the YAML-driven BaseWorkflow over a live issue.

Drives the new ``foundation.workflow`` engine (app/workflows/baseworkflow.yml + the action
interface files) through the full spec -> work -> build lifecycle on real GitHub
issue data, deterministically (MockActionFactory: the architect/admin subsystem
bodies run as oracles — real logic, no model, no network, no side effects). Prints
a phase-by-phase trace and the produced deliverables, so a live "roundabout" of the
pipeline's three phases is observable end to end.

Usage:
    python3 scripts/demo/roundabout-baseworkflow.py OWNER/REPO ISSUE [ISSUE...]
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "baseworkflow"))

from foundation import proc  # noqa: E402  (capturing subprocess wrapper)
import baseworkflow as bw  # noqa: E402  (the YAML-driven thin loader)


def _fetch_issue(repo: str, num: int) -> dict:
    out = proc.run(
        ["gh", "issue", "view", str(num), "-R", repo, "--json", "number,title,body,labels"],
        check=True,
    ).stdout
    d = json.loads(out)
    return {
        "issue": d["number"],
        "title": d.get("title", ""),
        "body": d.get("body", "") or "",
        "labels": [l["name"] for l in (d.get("labels") or [])],
    }


def run_one(repo: str, num: int) -> dict:
    job = _fetch_issue(repo, num)
    triage = {"action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.82}
    print(f"\n===== ROUNDABOUT: {repo}#{num} — {job['title']} =====")
    summary = bw.run_mock(job, triage)
    result = summary["result"]
    deliv = summary["deliverables"]
    ctx = summary["ctx"]

    print(f"  lifecycle result: ok={result.ok}")
    print(f"  phases visited (statechart trace, {len(ctx.trace)} steps):")
    # Group the audit trace by depth to show spec/work(nested)/build progression.
    for t in ctx.trace:
        indent = "    " + "  " * int(t.get("depth", 0))
        print(f"{indent}- [{t['kind']}] {t['name']} ok={t['ok']} depth={t['depth']}")

    print("  budget meters:")
    for key, m in ctx.budgets.items():
        print(f"    {key}: spent={m.spent} / cap={m.total}")
    print(f"    global: spent={ctx.meter.spent} / total={ctx.meter.total}")

    print(f"  deliverables produced ({len(deliv)}): {', '.join(sorted(deliv))}")
    wp = deliv.get("work_plan") or {}
    print("  --- WORK PLAN (spec) ---")
    print(f"    purpose: {(wp.get('purpose') or {}).get('purpose')}")
    print(f"    strategy: {(wp.get('strategy') or {}).get('strategy')}")
    print(f"    bucket: {(deliv.get('bucket') or {}).get('bucket')} "
          f"(k_units={(deliv.get('bucket') or {}).get('k_units')})")
    print(f"    acceptance_criteria: {wp.get('acceptance_criteria')}")
    osc = deliv.get("orchestration_script") or {}
    print(f"  --- ORCHESTRATION (work) --- {len(osc.get('phases', []))} phase(s), "
          f"budget={osc.get('budget')}, allow_tools={osc.get('allow_tools')}")
    er = deliv.get("engineering_result") or {}
    print(f"    engineering_result: ok={er.get('ok')}")
    docs = deliv.get("docs") or {}
    print(f"  --- DOCS (build/admin) --- {docs.get('summary')!r} sections={docs.get('sections')}")
    stored = deliv.get("stored") or {}
    print(f"    stored analytics: {json.dumps(stored.get('analytics', {}), sort_keys=True)}")
    return {"issue": num, "ok": result.ok, "deliverables": sorted(deliv)}


def main(argv) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    repo = argv[0]
    results = [run_one(repo, int(n)) for n in argv[1:]]
    ok = all(r["ok"] for r in results)
    print(f"\n===== roundabout summary: {sum(r['ok'] for r in results)}/{len(results)} green =====")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
