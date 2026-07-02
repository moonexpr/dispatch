#!/usr/bin/env python3
"""test_ux_loop_issues.py — WebsiteWF use-case overlay tests for #166
("create new GitHub issues from UX loops / website use cases").

Asserts the overlay loads + validates, the verbs (replace + extend) folded onto
baseworkflow as declared, the registry inherits the base binds and adds this use
case's web:* binds, and a (deterministic) run of the bodies produces the proposed-
issues spec deliverable as DATA.

The WEBSITEWF_USECASE selector lives in websitewf/websitewf.py, owned by the
Integrate phase (not yet wired when this lands), so the spec-deliverable assertion
exercises the use-case bodies directly rather than through run_mock — the bodies are
pure + deterministic, so this is the same data the selector path will produce.

No pytest — a runnable, self-asserting module (exit 0 = pass).
Run: ``python3 websitewf/test_ux_loop_issues.py``  (from the repo root).
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


def _ok(cond: bool, label: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


_WF = "workflows/websitewf-ux-loop-issues.yml"

_JOB = {
    "issue": 166,
    "repo": "ReclaimByDesign/dispatch-testrepo-a",
    "title": "Onboarding flow",
    "use_cases": [
        {"name": "Sign up", "goal": "A new visitor can create an account",
         "steps": ["land on /", "click Sign up", "submit the form"]},
        {"name": "Reset password", "goal": "A user can recover access"},
    ],
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
    """The replace + extend verbs fold onto the base tree as declared."""
    from foundation.workflow import load_workflow

    doc = load_workflow(_WF)
    _ok(doc.name == "websitewf-ux-loop-issues",
        "overlay header: name is 'websitewf-ux-loop-issues'")
    spec = _phase_tokens(next(p for p in doc.phases if p.name == "spec"))
    _ok(
        "web:generate_issue_work_units" in spec and "github:generate_work_units" not in spec,
        "replace: github:generate_work_units -> web:generate_issue_work_units",
    )
    _ok(
        spec.index("web:classify_uxloop_addendum") == spec.index("architect:classify_strategy") + 1,
        "extend after: web:classify_uxloop_addendum follows architect:classify_strategy",
    )
    # the replacement carries a procedure manifest (deterministic spec writer).
    m = _kind_of(next(p for p in doc.phases if p.name == "spec"), "web:generate_issue_work_units")
    _ok(m is not None and m.kind == "procedure", "replacement is a kind 'procedure'")


def test_validate_clean() -> None:
    from foundation.workflow import load_workflow, validate
    from websitewf.usecases.ux_loop_issues import build_registry

    errs = validate(load_workflow(_WF), build_registry())
    _ok(not errs, f"websitewf-ux-loop-issues overlay validates clean ({len(errs)} error(s))")


def test_registry_inherits_base() -> None:
    from websitewf.usecases.ux_loop_issues import build_registry

    reg = build_registry()
    _ok(reg.has_action("generate_work_units"), "registry inherits a base bind (generate_work_units)")
    _ok(reg.has_action("classify_strategy"), "registry inherits a base bind (classify_strategy)")
    for b in ("generate_issue_work_units", "classify_uxloop_addendum"):
        _ok(reg.has_action(b), f"registry adds web bind ({b})")


def test_spec_deliverable() -> None:
    """The use-case bodies produce the proposed-issues spec deliverable as DATA,
    preserving the base purpose / work_unit contract — and never calling `gh`."""
    from websitewf.usecases.ux_loop_issues import (
        classify_uxloop_addendum,
        generate_issue_work_units,
    )

    out = generate_issue_work_units({"job": _JOB})
    _ok("purpose" in out and "work_unit" in out,
        "replace body preserves base purpose / work_unit contract")
    issues = out.get("proposed_issues")
    _ok(isinstance(issues, list) and len(issues) == 2,
        "replace body emits one proposed issue per use case (2)")
    first = (issues or [{}])[0]
    _ok(
        all(k in first for k in ("title", "body", "acceptance", "labels")),
        "each proposed issue has work-order shape (title/body/acceptance/labels)",
    )
    _ok("ux-loop" in first.get("labels", []), "proposed issue carries the ux-loop label")
    _ok(isinstance(first.get("acceptance"), list) and first["acceptance"],
        "proposed issue has acceptance criteria")

    # extend body recognizes the UX-loop input and tags it onto the strategy.
    s = classify_uxloop_addendum({"strategy": {"value": "A"}, "proposed_issues": issues})
    strat = s.get("strategy") or {}
    _ok(strat.get("input_kind") == "ux-loop", "extend tags input_kind=ux-loop onto strategy")
    _ok(strat.get("proposed_issue_count") == 2, "extend records the proposed-issue count")
    _ok(strat.get("value") == "A", "extend preserves the base strategy fields")


def test_no_gh_calls() -> None:
    """The bodies must be DATA-only: no `gh` / subprocess / network in this module."""
    import inspect

    import websitewf.usecases.ux_loop_issues as mod

    src_text = inspect.getsource(mod)
    bad = [tok for tok in ("subprocess", "gh issue", "gh pr", "requests.", "urllib")
           if tok in src_text]
    _ok(not bad, f"use-case module makes no gh/network calls (found: {bad})")


def main() -> int:
    print("== test_ux_loop_issues ==")
    test_overlay_merge()
    test_validate_clean()
    test_registry_inherits_base()
    test_spec_deliverable()
    test_no_gh_calls()
    print(f"-- ux_loop_issues tests: PASS={_PASS} FAIL={_FAIL} --")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
