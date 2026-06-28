#!/usr/bin/env python3
"""test_scaffold_foundation.py — WebsiteWF use-case overlay tests for #167
"scaffold a website foundation (Next.js or Laravel)".

Self-asserting module (exit 0 = pass), matching the repo convention — no pytest.
Run from the repo root so the app-dir-relative workflow path resolves:
  python3 src/websitewf/test_scaffold_foundation.py

Proves, for the scaffold-foundation overlay (disjoint from the proof vertical):
  * the overlay loads with the right header;
  * the four-verb diff folds onto baseworkflow as declared (extend / add / proxy);
  * the merged overlay validates clean against this use case's registry;
  * the registry inherits the base binds and adds this use case's web:* binds;
  * a full mock run produces the foundation_spec deliverable + the proxy mirror.

The mock-run is driven directly through a BaseWorkflow subclass bound to THIS
overlay + registry, so it does not depend on the (Integrate-phase-owned) selector
in src/websitewf/websitewf.py. A forward-compatible selector check is included and
asserted only once the selector path is wired (skipped-with-note until then).
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SRC)
for _p in (_ROOT, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_PASS = 0
_FAIL = 0

_SLUG = "scaffold-foundation"
_WF = f"workflows/websitewf-{_SLUG}.yml"


def _ok(cond: bool, label: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


_JOB = {
    "issue": 167,
    "repo": "ReclaimByDesign/dispatch-testrepo-a",
    "title": "Scaffold a Next.js website foundation",
    "framework": "next",
}
_TRIAGE = {"action": "implement", "scope": "l", "route": "gen-default", "confidence": 0.9}


def _phase_tokens(phase):
    from engine.workflow.nodes import ActionRefNode, LoopNode

    def toks(n):
        if isinstance(n, ActionRefNode):
            return [n.token]
        if isinstance(n, LoopNode):
            return toks(n.body)
        return [t for s in getattr(n, "steps", ()) for t in toks(s)]

    return [t for s in phase.steps for t in toks(s)]


def _kind_of(phase, token):
    from engine.workflow.nodes import ActionRefNode, LoopNode

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
    """The overlay loads with the expected header."""
    from engine.workflow import load_workflow

    doc = load_workflow(_WF)
    _ok(doc.name == f"websitewf-{_SLUG}", f"overlay header: name is 'websitewf-{_SLUG}'")


def test_overlay_merge() -> None:
    """The three overlay verbs (extend / add / proxy) fold onto the base tree."""
    from engine.workflow import load_workflow

    doc = load_workflow(_WF)
    spec = _phase_tokens(doc.phases[0])
    # extend after: the addendum follows architect:classify_strategy in the spec phase.
    _ok(
        "web:classify_foundation_addendum" in spec
        and spec.index("web:classify_foundation_addendum")
        == spec.index("architect:classify_strategy") + 1,
        "extend after: web:classify_foundation_addendum follows architect:classify_strategy",
    )
    work = doc.phases[1]
    work_tokens = _phase_tokens(work)
    # add: the scaffold task is spliced into the work phase.
    _ok("web:scaffold_foundation" in work_tokens, "add: web:scaffold_foundation spliced into the work phase")
    # proxy: engineer:execute_orchestration becomes a kind: proxy wrapper.
    pm = _kind_of(work, "engineer:execute_orchestration")
    _ok(pm is not None and pm.kind == "proxy", "proxy: engineer:execute_orchestration is kind 'proxy'")
    _ok(
        pm is not None and pm.proxy_target is not None and pm.proxy_target.bind == "execute_orchestration",
        "proxy: wraps the original target's bind",
    )


def test_validate_clean() -> None:
    """The merged overlay validates against this use case's (base + web) registry."""
    from engine.workflow import load_workflow, validate
    from src.websitewf.usecases.scaffold_foundation import build_registry

    errs = validate(load_workflow(_WF), build_registry())
    _ok(not errs, f"websitewf-{_SLUG} overlay validates clean ({len(errs)} error(s))")


def test_registry_tokens() -> None:
    """The registry inherits the base binds and adds this use case's web:* binds."""
    from src.websitewf.usecases.scaffold_foundation import build_registry

    reg = build_registry()
    _ok(reg.has_action("generate_work_units"), "registry inherits a base bind (generate_work_units)")
    _ok(reg.has_action("execute_orchestration"), "registry inherits a base bind (execute_orchestration)")
    for b in ("scaffold_foundation", "classify_foundation_addendum"):
        _ok(reg.has_action(b), f"registry adds web bind ({b})")


def _run_overlay_mock(job, triage):
    """Mock-run THIS overlay + registry through a BaseWorkflow subclass bound to it,
    independent of the Integrate-phase selector in websitewf.py."""
    from engine.actions import MockActionFactory
    from engine.workflow import load_workflow
    from src.baseworkflow.baseworkflow import BaseWorkflow
    from src.websitewf.usecases.scaffold_foundation import build_registry

    doc = load_workflow(_WF)
    budget = int(doc.budgets["total"])

    class _ScaffoldFoundationWF(BaseWorkflow):
        def _load_doc(self):
            return doc

        def _build_registry(self):
            return build_registry()

        def _total_budget(self):
            return budget

    wf = _ScaffoldFoundationWF(MockActionFactory(), job=job, triage=triage)
    ctx = wf.context(dry_run=True)
    result = wf.run(ctx=ctx)
    return {"result": result, "deliverables": wf.shelves.deliverables.snapshot()}


def test_mock_run_produces_spec() -> None:
    """A full mock run produces the foundation_spec deliverable + the proxy mirror."""
    out = _run_overlay_mock(_JOB, _TRIAGE)
    _ok(out["result"].ok, "scaffold-foundation run_mock completes ok")
    d = out["deliverables"]
    fs = d.get("foundation_spec") or {}
    _ok(fs.get("framework") == "next", "add: foundation_spec authored (framework=next)")
    _ok(isinstance(fs.get("dir_layout"), dict) and bool(fs["dir_layout"]),
        "foundation_spec carries a dir_layout")
    _ok(isinstance(fs.get("key_files"), list) and bool(fs["key_files"]),
        "foundation_spec carries key_files")
    _ok((d.get("strategy") or {}).get("framework") == "next",
        "extend: strategy augmented with framework")
    _ok("engineering_result" in d and "web_engineering_result" in d,
        "proxy: engineering_result mirrored to web_engineering_result")
    _ok(d.get("web_engineering_result") == d.get("engineering_result"),
        "proxy: the out-rewire copy equals the target's output")


def test_mock_run_laravel() -> None:
    """The framework choice is data-driven: a laravel job yields a laravel spec."""
    job = dict(_JOB, framework="laravel", title="Scaffold a Laravel foundation")
    out = _run_overlay_mock(job, _TRIAGE)
    fs = out["deliverables"].get("foundation_spec") or {}
    _ok(fs.get("framework") == "laravel", "framework picked from job: laravel")
    _ok(any("composer" in f for f in fs.get("key_files", [])),
        "laravel key_files include composer.json")


def test_selector_path_forward_compat() -> None:
    """Forward-compatible: once the Integrate phase wires the WEBSITEWF_USECASE
    selector into websitewf.py, run_mock under the env var must produce the
    foundation_spec. Asserted only when the selector is wired; skipped-with-note
    until then (the proof vertical is the current default)."""
    from src.websitewf.websitewf import run_mock

    prev = os.environ.get("WEBSITEWF_USECASE")
    os.environ["WEBSITEWF_USECASE"] = _SLUG
    try:
        d = run_mock(_JOB, _TRIAGE, dry_run=True)["deliverables"]
    finally:
        if prev is None:
            os.environ.pop("WEBSITEWF_USECASE", None)
        else:
            os.environ["WEBSITEWF_USECASE"] = prev

    if "foundation_spec" in d:
        _ok((d.get("foundation_spec") or {}).get("framework") == "next",
            "selector: WEBSITEWF_USECASE routes run_mock to this overlay")
    else:
        print("  SKIP selector path not yet wired (Integrate phase owns websitewf.py)")


def main() -> int:
    print("== test_scaffold_foundation ==")
    test_overlay_loads()
    test_overlay_merge()
    test_validate_clean()
    test_registry_tokens()
    test_mock_run_produces_spec()
    test_mock_run_laravel()
    test_selector_path_forward_compat()
    print(f"-- scaffold-foundation tests: PASS={_PASS} FAIL={_FAIL} --")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
