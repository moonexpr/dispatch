#!/usr/bin/env python3
"""test_supersede.py — superseedable HFSM states (ADR-003 / #190).

Covers the preemption contract: a ``ctx.supersede`` request preempts the active
plan at a microstep boundary at the nearest frame whose priority it beats;
``abandon`` hands the superstate over to the superseder, ``suspend`` resumes the
preempted plan afterwards; a refused request bubbles and an unaccepted one is
dropped with an audit entry; the grafted state serializes with the chart.

No pytest in this repo — a runnable, self-asserting module (exit 0 = pass).
Run: ``python3 foundation/actions/test_supersede.py``.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from foundation.actions import (  # noqa: E402
    BudgetMeter,
    Context,
    Interpreter,
    MockActionFactory,
    Procedure,
    SUPERSEDE_SUSPEND,
    Sequence,
    Statechart,
)

CHECKS = {"n": 0}


def ok(label: str) -> None:
    CHECKS["n"] += 1
    print(f"  ok  {label}")


def fresh_ctx() -> Context:
    return Context(shelves=MockActionFactory().shelves(), meter=BudgetMeter(10**9, label="test"))


def step(name: str):
    return Procedure(name, lambda payload, ctx, _n=name: f"ran:{_n}")


def raiser(name: str, handler, **kw):
    def fn(payload, ctx):
        ctx.supersede(handler, name="handler", **kw)
        return f"ran:{name}"

    return Procedure(name, fn)


def run(seq: Sequence, ctx: Context, *, root_priority: int = 0):
    root = seq.compile(seq.name)
    root.priority = root_priority
    interp = Interpreter(Statechart(root=root), ctx)
    return interp, interp.run("payload")


def ran(ctx: Context, name: str) -> bool:
    return any(t["name"] == name for t in ctx.trace)


def audited(interp: Interpreter, event: str) -> bool:
    return any(e.get("event") == event for e in interp.events)


def test_abandon_takes_over() -> None:
    ctx = fresh_ctx()
    seq = Sequence([step("a"), raiser("route", step("handler")), step("c")], name="plan")
    interp, result = run(seq, ctx)
    assert result.ok and result.value == "ran:handler", result
    assert ran(ctx, "a") and ran(ctx, "route") and ran(ctx, "handler")
    assert not ran(ctx, "c"), "abandon must skip the remaining plan"
    assert audited(interp, "supersede.accepted") and audited(interp, "supersede.completed")
    ok("abandon: superseder's result completes the superstate; remaining plan skipped")


def test_suspend_resumes_plan() -> None:
    ctx = fresh_ctx()
    seq = Sequence(
        [step("a"), raiser("route", step("handler"), policy=SUPERSEDE_SUSPEND), step("c")],
        name="plan",
    )
    interp, result = run(seq, ctx)
    assert result.ok and result.value == "ran:c", result
    assert ran(ctx, "handler") and ran(ctx, "c")
    assert audited(interp, "supersede.completed")
    ok("suspend: interruption runs, then the preempted plan resumes and completes")


def test_suspend_failure_fails_plan() -> None:
    ctx = fresh_ctx()

    def boom(payload, ctx):
        raise RuntimeError("handler exploded")

    seq = Sequence(
        [step("a"), raiser("route", Procedure("handler", boom), policy=SUPERSEDE_SUSPEND), step("c")],
        name="plan",
    )
    interp, result = run(seq, ctx)
    assert not result.ok, "a failed interruption must fail the suspended plan"
    assert not ran(ctx, "c")
    assert audited(interp, "supersede.failed")
    ok("suspend: a failed interruption routes through the error edge")


def test_priority_gate_refuses_and_drops() -> None:
    ctx = fresh_ctx()
    seq = Sequence([step("a"), raiser("route", step("handler"), priority=1), step("c")], name="plan")
    interp, result = run(seq, ctx, root_priority=5)
    assert result.ok and result.value == "ran:c", result
    assert not ran(ctx, "handler"), "a refused request must not run"
    assert audited(interp, "supersede.refused") and audited(interp, "supersede.dropped")
    refusals = [e for e in interp.events if e.get("event") == "supersede.refused"]
    assert len(refusals) == 1, f"one refusal audit per frame, got {len(refusals)}"
    ok("priority gate: low-priority request refused once, dropped at root with audit")


def test_nested_frame_accepts_innermost() -> None:
    ctx = fresh_ctx()
    inner = Sequence([raiser("route", step("handler")), step("inner_tail")], name="inner")
    outer = Sequence([step("a"), inner, step("c")], name="outer")
    interp, result = run(outer, ctx)
    assert result.ok and result.value == "ran:c", result
    assert ran(ctx, "handler") and not ran(ctx, "inner_tail"), "inner plan abandoned"
    assert ran(ctx, "c"), "outer plan unaffected — preemption is frame-scoped"
    ok("nesting: the innermost frame accepts; abandon is scoped to that superstate")


def test_grafted_state_serializes() -> None:
    ctx = fresh_ctx()
    seq = Sequence([raiser("route", step("handler"))], name="plan")
    root = seq.compile("plan")
    interp = Interpreter(Statechart(root=root), ctx)
    result = interp.run("payload")
    assert result.ok
    d = interp.chart.to_dict()
    grafted = [c for c in d["root"].get("children", ()) if "handler" in c["id"]]
    assert grafted and grafted[0].get("priority") == 1, d["root"].get("children")
    ok("graft: the superseding state appears in the serialized chart with its priority")


def main() -> int:
    test_abandon_takes_over()
    test_suspend_resumes_plan()
    test_suspend_failure_fails_plan()
    test_priority_gate_refuses_and_drops()
    test_nested_frame_accepts_innermost()
    test_grafted_state_serializes()
    print(f"\nsupersede: {CHECKS['n']} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
