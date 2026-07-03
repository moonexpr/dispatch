#!/usr/bin/env python3
"""test_websitewf.py — WebsiteWF engine + overlay/proxy mechanism tests.

Proves ADR-002's first increment end to end:

  * the overlay merge (``extends:`` + the four verbs replace/extend/add/proxy)
    folds onto baseworkflow into the expected merged tree;
  * the merged overlay validates clean against the WebsiteWF registry;
  * the new ``kind: proxy`` action reroutes shelf I/O around its target;
  * a full ``WebsiteWF.run_mock`` produces each verb's deliverable; and
  * the authoring bridge selects the engine by ``DISPATCH_ENGINE``.

No pytest, no smoke harness — a runnable, self-asserting module (exit 0 = pass),
matching the repo convention. Run: ``python3 websitewf/test_websitewf.py``.
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_PASS = 0
_FAIL = 0


def _ok(cond: bool, label: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


_JOB = {
    "issue": 42,
    "repo": "ReclaimByDesign/dispatch-testrepo-a",
    "title": "Add a /pricing page",
    "route": "/pricing",
    "framework": "nextjs",
}
_TRIAGE = {"action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.9}


def _phase_tokens(phase):
    from foundation.workflow.nodes import ActionRefNode, LoopNode

    def toks(n):
        if isinstance(n, ActionRefNode):
            return [n.token]
        if isinstance(n, LoopNode):
            return toks(n.body)
        return [t for s in getattr(n, "steps", ()) for t in toks(s)]

    return [t for s in phase.steps for t in toks(s)]


def _kind_of(phase, token):
    from foundation.workflow.nodes import ActionRefNode, LoopNode

    def find(n):
        if isinstance(n, ActionRefNode):
            return n.manifest if n.token == token else None
        if isinstance(n, LoopNode):
            return find(n.body)
        for s in getattr(n, "steps", ()):
            m = find(s)
            if m is not None:
                return m
        return None

    return find(phase)


def test_overlay_merge() -> None:
    """The four overlay verbs fold onto the base tree as declared."""
    from foundation.workflow import load_workflow

    doc = load_workflow("workflows/websitewf.yml")
    _ok(doc.name == "websitewf", "overlay header: name is 'websitewf'")
    spec = _phase_tokens(doc.phases[0])
    _ok(
        "web:generate_route_work_units" in spec and "github:generate_work_units" not in spec,
        "replace: github:generate_work_units -> web:generate_route_work_units",
    )
    _ok(
        spec.index("web:classify_route_addendum") == spec.index("architect:classify_strategy") + 1,
        "extend after: web:classify_route_addendum follows architect:classify_strategy",
    )
    work = doc.phases[1]
    pm = _kind_of(work, "engineer:execute_orchestration")
    _ok(pm is not None and pm.kind == "proxy", "proxy: engineer:execute_orchestration is kind 'proxy'")
    _ok(pm is not None and pm.proxy_target is not None and pm.proxy_target.bind == "execute_orchestration",
        "proxy: wraps the original target's bind")
    _ok("web:scaffold_route" in _phase_tokens(work), "add: web:scaffold_route spliced into the work phase")


def test_validate_clean() -> None:
    """The merged overlay validates against the WebsiteWF (base + web) registry."""
    from foundation.workflow import load_workflow, validate
    from websitewf.bindings import build_registry

    errs = validate(load_workflow("workflows/websitewf.yml"), build_registry())
    _ok(not errs, f"websitewf overlay validates clean ({len(errs)} error(s))")


def test_registry_inherits_base() -> None:
    """WebsiteWF's registry inherits every base bind and adds the web:* binds."""
    from websitewf.bindings import build_registry

    reg = build_registry()
    _ok(reg.has_action("generate_work_units"), "registry inherits a base bind (generate_work_units)")
    _ok(reg.has_action("execute_orchestration"), "registry inherits a base bind (execute_orchestration)")
    for b in ("generate_route_work_units", "classify_route_addendum", "scaffold_route"):
        _ok(reg.has_action(b), f"registry adds web bind ({b})")


def test_e2e_run_mock() -> None:
    """A full WebsiteWF run produces each overlay verb's deliverable."""
    from websitewf.websitewf import run_mock

    out = run_mock(_JOB, _TRIAGE, dry_run=True)
    _ok(out["result"].ok, "websitewf run_mock completes ok")
    d = out["deliverables"]
    _ok((d.get("web_route_spec") or {}).get("path") == "/pricing",
        "replace: web_route_spec authored (path=/pricing)")
    _ok((d.get("strategy") or {}).get("route_path") == "/pricing",
        "extend: strategy augmented with route_path")
    _ok("engineering_result" in d and "web_engineering_result" in d,
        "proxy: engineering_result mirrored to web_engineering_result")
    _ok(d.get("web_engineering_result") == d.get("engineering_result"),
        "proxy: the out-rewire copy equals the target's output")
    _ok((d.get("scaffold_result") or {}).get("status") == "scaffolded",
        "add: web:scaffold_route produced scaffold_result")


def test_proxy_unit() -> None:
    """The Proxy action copies shelf entries before/after the wrapped target."""
    from foundation.actions import MemoryShelf, Shelves
    from foundation.actions.action import BudgetMeter, Context, Procedure, Proxy

    shelves = Shelves(MemoryShelf("input"), MemoryShelf("deliverables"), MemoryShelf("shared"))
    shelves.deliverables.put("src_key", "rerouted-value")
    ctx = Context(shelves=shelves, meter=BudgetMeter(1000))
    # inner reads the target's default input key and writes a default output key.
    def body(_payload, c):
        c.shelves.deliverables.put("default_out", f"ran:{c.shelves.deliverables.get('default_in')}")
        return None
    inner = Procedure("target", body)
    proxy = Proxy(
        "proxy:target", inner,
        pre=[("deliverables", "src_key", "deliverables", "default_in")],
        post=[("deliverables", "default_out", "deliverables", "new_sink")],
    )
    proxy.run(None, ctx)
    _ok(shelves.deliverables.get("default_in") == "rerouted-value", "proxy pre: source copied into target input")
    _ok(shelves.deliverables.get("default_out") == "ran:rerouted-value", "proxy: target ran on the rerouted input")
    _ok(shelves.deliverables.get("new_sink") == "ran:rerouted-value", "proxy post: target output copied to new sink")


def test_bridge_engine_selection() -> None:
    """The authoring bridge selects the workflow engine by name and enriches."""
    from visitor.orchestration import baseworkflow_bridge as bridge

    os.environ["PIPELINE_DRY_RUN"] = "1"
    req = json.dumps({
        "issue": 42, "title": "Add a /pricing page", "route": "gen-default",
        "scope": "m", "confidence": 0.9,
    }, separators=(",", ":"))
    ww = json.loads(bridge.author_via_workflow(req, "websitewf"))
    _ok(any(k in ww for k in ("orchestration_script", "work_plan", "plan")),
        "bridge engine=websitewf enriches the Job Request")
    bw = json.loads(bridge.author_via_workflow(req, "baseworkflow"))
    _ok(any(k in bw for k in ("orchestration_script", "work_plan", "plan")),
        "bridge engine=baseworkflow enriches the Job Request")


def main() -> int:
    print("== test_websitewf ==")
    test_overlay_merge()
    test_validate_clean()
    test_registry_inherits_base()
    test_e2e_run_mock()
    test_proxy_unit()
    test_bridge_engine_selection()
    print(f"-- websitewf tests: PASS={_PASS} FAIL={_FAIL} --")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
