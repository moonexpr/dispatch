#!/usr/bin/env python3
"""test_adopt_integration.py — WebsiteWF "adopt-integration" use-case overlay tests (#170).

Proves the overlay for adopting third-party technology (e.g. Stripe):

  * the overlay loads + has the right header;
  * the overlay's three verbs (extend/add/proxy) fold onto baseworkflow as declared;
  * the merged overlay validates clean against this use case's (base + web) registry;
  * build_registry inherits the base binds and adds this use case's web:* binds;
  * a mock run produces the integration spec deliverable (env-var NAMES only); and
  * the spec never carries a secret VALUE (security: names only).

No pytest — a runnable, self-asserting module (exit 0 = pass), matching the repo
convention. Run from the repo root: ``python3 src/websitewf/test_adopt_integration.py``.
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


def _ok(cond: bool, label: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


_WF = "workflows/websitewf-adopt-integration.yml"
_JOB = {
    "issue": 170,
    "repo": "ReclaimByDesign/dispatch-testrepo-a",
    "title": "Adopt Stripe for checkout",
    "integration": "stripe",
}
_TRIAGE = {"action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.9}


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


def test_overlay_merge() -> None:
    """The three overlay verbs fold onto the base tree as declared."""
    from engine.workflow import load_workflow

    doc = load_workflow(_WF)
    _ok(doc.name == "websitewf-adopt-integration",
        "overlay header: name is 'websitewf-adopt-integration'")

    spec = _phase_tokens(doc.phases[0])
    _ok(
        spec.index("web:classify_integration_addendum")
        == spec.index("architect:classify_strategy") + 1,
        "extend after: web:classify_integration_addendum follows architect:classify_strategy",
    )

    work = doc.phases[1]
    _ok("web:adopt_integration" in _phase_tokens(work),
        "add: web:adopt_integration spliced into the work phase")
    pm = _kind_of(work, "engineer:execute_orchestration")
    _ok(pm is not None and pm.kind == "proxy",
        "proxy: engineer:execute_orchestration is kind 'proxy'")
    _ok(pm is not None and pm.proxy_target is not None
        and pm.proxy_target.bind == "execute_orchestration",
        "proxy: wraps the original target's bind")


def test_validate_clean() -> None:
    """The merged overlay validates against this use case's (base + web) registry."""
    from engine.workflow import load_workflow, validate
    from src.websitewf.usecases.adopt_integration import build_registry

    errs = validate(load_workflow(_WF), build_registry())
    _ok(not errs, f"websitewf-adopt-integration overlay validates clean ({len(errs)} error(s))")


def test_registry_tokens() -> None:
    """build_registry inherits every base bind and adds this use case's web binds."""
    from src.websitewf.usecases.adopt_integration import build_registry

    reg = build_registry()
    _ok(reg.has_action("generate_work_units"),
        "registry inherits a base bind (generate_work_units)")
    _ok(reg.has_action("execute_orchestration"),
        "registry inherits a base bind (execute_orchestration)")
    for b in ("adopt_integration", "classify_integration_addendum"):
        _ok(reg.has_action(b), f"registry adds web bind ({b})")


def _run_mock_overlay():
    """Mock-run the overlay directly via a BaseWorkflow subclass bound to THIS use
    case's doc + registry. (The WEBSITEWF_USECASE selector is owned by the Integrate
    phase and not yet wired into websitewf.py, so we exercise the engine hooks here.)"""
    from engine.actions import MockActionFactory
    from engine.workflow import load_workflow
    from src.baseworkflow.baseworkflow import BaseWorkflow
    from src.websitewf.usecases.adopt_integration import build_registry

    doc = load_workflow(_WF)

    class _WF_AdoptIntegration(BaseWorkflow):
        def _load_doc(self):
            return doc

        def _build_registry(self):
            return build_registry()

        def _total_budget(self):
            return int(doc.budgets["total"])

    wf = _WF_AdoptIntegration(MockActionFactory(), job=_JOB, triage=_TRIAGE)
    ctx = wf.context(dry_run=True)
    result = wf.run(ctx=ctx)
    return result, wf.shelves.deliverables.snapshot()


def test_mock_run_produces_spec() -> None:
    """A mock run produces the adoption spec deliverable with the expected fields."""
    result, d = _run_mock_overlay()
    _ok(result.ok, "adopt-integration mock run completes ok")

    spec = d.get("integration_spec") or {}
    _ok(spec.get("target") == "stripe", "add/extend: integration_spec target=stripe")
    _ok(spec.get("sdk") == "stripe", "integration_spec names the SDK (stripe)")
    _ok("STRIPE_SECRET_KEY" in (spec.get("env_var_names") or []),
        "integration_spec lists env-var NAMES (STRIPE_SECRET_KEY)")
    _ok(bool(spec.get("glue_points")), "integration_spec lists glue points")

    strategy = d.get("strategy") or {}
    _ok(strategy.get("integration_target") == "stripe",
        "extend: strategy augmented with integration_target")

    _ok("engineering_result" in d and "web_engineering_result" in d,
        "proxy: engineering_result mirrored to web_engineering_result")


def test_security_names_only() -> None:
    """SECURITY: the spec emits env-var NAMES only — never a secret VALUE, and the
    body never reads a real secret out of the environment."""
    # Poison the env with a fake secret value; the spec must NOT contain it.
    sentinel = "sk_live_THIS_MUST_NEVER_LEAK_INTO_THE_SPEC"
    os.environ["STRIPE_SECRET_KEY"] = sentinel
    try:
        _result, d = _run_mock_overlay()
    finally:
        os.environ.pop("STRIPE_SECRET_KEY", None)

    import json as _json

    blob = _json.dumps(d.get("integration_spec") or {})
    _ok(sentinel not in blob, "security: no secret VALUE leaked into the integration spec")
    _ok("STRIPE_SECRET_KEY" in blob, "security: env-var NAME is present (names only)")


def main() -> int:
    print("== test_adopt_integration ==")
    test_overlay_merge()
    test_validate_clean()
    test_registry_tokens()
    test_mock_run_produces_spec()
    test_security_names_only()
    print(f"-- adopt-integration tests: PASS={_PASS} FAIL={_FAIL} --")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
