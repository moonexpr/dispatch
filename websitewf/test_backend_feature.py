#!/usr/bin/env python3
"""test_backend_feature.py — WebsiteWF "backend-feature" use-case overlay (#169).

Proves the overlay vertical end to end (disjoint from the proof vertical):

  * the overlay loads + carries the right header;
  * the two overlay verbs (add + proxy) fold onto baseworkflow as declared, and the
    budget overlay extends/overrides the base buckets;
  * the merged overlay validates clean against this use case's registry;
  * the registry inherits baseworkflow's binds and adds web:scaffold_backend;
  * a full mock run authors the backend contract spec deliverable.

No pytest, no smoke harness — a runnable, self-asserting module (exit 0 = pass),
matching the repo convention. Run from the repo root so the app-dir-relative
workflow path resolves:  ``python3 websitewf/test_backend_feature.py``.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_PASS = 0
_FAIL = 0

# slug/module per the use-case overlay assignments (also the selector key, #169).
_SLUG = "backend-feature"
_WORKFLOW = "workflows/websitewf-backend-feature.yml"

_JOB = {
    "issue": 169,
    "repo": "ReclaimByDesign/dispatch-testrepo-a",
    "title": "Add a comments backend",
    "feature": "comments",
    "resource": "comment",
    "api_route": "/api/comments",
    "framework": "nextjs",
    "fields": ["id", "author", "body", "created_at"],
}
_TRIAGE = {"action": "implement", "scope": "l", "route": "gen-default", "confidence": 0.9}


def _ok(cond: bool, label: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


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


def test_overlay_loads() -> None:
    """The overlay loads and carries the right header + budget overlay."""
    from foundation.workflow import load_workflow

    doc = load_workflow(_WORKFLOW)
    _ok(doc.name == "websitewf-backend-feature", "overlay header: name is 'websitewf-backend-feature'")
    # budget overlay: total + engineering bumped; untouched base buckets inherited.
    _ok(int(doc.budgets["total"]) == 1200000, "budget overlay: total bumped to 1,200,000")
    _ok(int(doc.budgets["engineering"]) == 650000, "budget overlay: engineering bumped to 650,000")
    _ok(int(doc.budgets["architect"]) == 450000, "budget overlay: base 'architect' bucket inherited")


def test_overlay_verbs() -> None:
    """The add + proxy verbs fold onto the base tree as declared."""
    from foundation.workflow import load_workflow

    doc = load_workflow(_WORKFLOW)
    spec = _phase_tokens(doc.phases[0])
    # add: web:scaffold_backend spliced into spec, right after author_orchestration.
    _ok("web:scaffold_backend" in spec, "add: web:scaffold_backend spliced into the spec phase")
    _ok(
        "architect:author_orchestration" in spec
        and spec.index("web:scaffold_backend") == spec.index("architect:author_orchestration") + 1,
        "add after: web:scaffold_backend follows architect:author_orchestration",
    )
    # proxy: the engineer in the work phase becomes a kind: proxy wrapper.
    work = doc.phases[1]
    pm = _kind_of(work, "engineer:execute_orchestration")
    _ok(pm is not None and pm.kind == "proxy", "proxy: engineer:execute_orchestration is kind 'proxy'")
    _ok(
        pm is not None and pm.proxy_target is not None and pm.proxy_target.bind == "execute_orchestration",
        "proxy: wraps the original target's bind",
    )


def test_validate_clean() -> None:
    from foundation.workflow import load_workflow, validate
    from websitewf.usecases.backend_feature import build_registry

    errs = validate(load_workflow(_WORKFLOW), build_registry())
    _ok(not errs, f"websitewf-backend-feature overlay validates clean ({len(errs)} error(s))")


def test_registry_tokens() -> None:
    """The registry inherits a base bind and adds the new web bind."""
    from websitewf.usecases.backend_feature import build_registry

    reg = build_registry()
    _ok(reg.has_action("generate_work_units"), "registry inherits a base bind (generate_work_units)")
    _ok(reg.has_action("execute_orchestration"), "registry inherits a base bind (execute_orchestration)")
    _ok(reg.has_action("scaffold_backend"), "registry adds web bind (scaffold_backend)")


def _run_mock_overlay():
    """Run the backend-feature overlay against the MockActionFactory. Subclasses
    BaseWorkflow with THIS overlay's doc + registry directly (disjoint — does not
    depend on the Integrate-phase selector in websitewf.py). The selector contract
    is documented for reference: WEBSITEWF_USECASE='backend-feature' picks this
    overlay inside the WebsiteWF engine once Integrate wires it."""
    from foundation.actions import MockActionFactory
    from foundation.workflow import load_workflow
    from baseworkflow.baseworkflow import BaseWorkflow
    from websitewf.usecases.backend_feature import build_registry

    doc = load_workflow(_WORKFLOW)
    reg = build_registry()

    class _BackendFeatureWF(BaseWorkflow):
        def _load_doc(self):
            return doc

        def _build_registry(self):
            return reg

        def _total_budget(self):
            return int(doc.budgets["total"])

    wf = _BackendFeatureWF(MockActionFactory(), job=_JOB, triage=_TRIAGE)
    ctx = wf.context(dry_run=True)
    result = wf.run(ctx=ctx)
    return result, wf.shelves.deliverables.snapshot()


def test_mock_run_produces_spec() -> None:
    """A full mock run completes ok and authors the backend contract spec."""
    result, d = _run_mock_overlay()
    _ok(result.ok, "backend-feature run_mock completes ok")
    spec = d.get("backend_spec") or {}
    _ok(spec.get("feature") == "comments", "add: backend_spec authored (feature=comments)")
    _ok(isinstance(spec.get("api_routes"), list) and len(spec["api_routes"]) >= 1,
        "add: backend_spec carries api_routes")
    _ok((spec.get("data_model") or {}).get("name") == "comment",
        "add: backend_spec carries the data_model")
    _ok(bool(spec.get("server_logic")), "add: backend_spec carries server_logic")
    # proxy: the engineer's result is mirrored onto the web-scoped key.
    _ok("engineering_result" in d and "web_engineering_result" in d,
        "proxy: engineering_result mirrored to web_engineering_result")
    _ok(d.get("web_engineering_result") == d.get("engineering_result"),
        "proxy: the out-rewire copy equals the target's output")


def main() -> int:
    print("== test_backend_feature ==")
    test_overlay_loads()
    test_overlay_verbs()
    test_validate_clean()
    test_registry_tokens()
    test_mock_run_produces_spec()
    print(f"-- backend-feature tests: PASS={_PASS} FAIL={_FAIL} --")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
