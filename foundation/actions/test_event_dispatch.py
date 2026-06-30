#!/usr/bin/env python3
"""test_event_dispatch.py — the statechart event-dispatch primitive, exercised on its
deterministic edges (no model, no I/O).

Complements test_statechart.py (which proves the headline supervised-retry path). Here
we pin the corner behaviours the per-slice supervisor relies on:

  * an unhandled named event does NOT hang or loop — the run still completes;
  * a named event takes PRIORITY over the plain done edge at the same level;
  * a named event PROPAGATES through multiple non-handling levels to the handler;
  * a transition GUARD can gate on the event payload;
  * a handled event is CONSUMED once (it does not re-fire on later completions);
  * multiple events raised by one child are all queued and matched in order.

Run: ``python3 foundation/actions/test_event_dispatch.py`` (exit 0 = pass).
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))  # <root>/foundation/actions -> <root>
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from foundation.actions.action import BudgetMeter, Context, Procedure  # noqa: E402
from foundation.actions.interpreter import Interpreter  # noqa: E402
from foundation.actions.result import Error, Output  # noqa: E402
from foundation.actions.shelf import MemoryShelf, Shelves  # noqa: E402
from foundation.actions.statechart import (  # noqa: E402
    COMPOUND, EV_DONE, EV_ERROR, LEAF, LOOP, T_DONE, T_ERROR, State, Statechart, Transition,
)

_PASSED = 0


def check(cond: bool, msg: str) -> None:
    global _PASSED
    if cond:
        _PASSED += 1
        print(f"  ok  {msg}")
    else:
        print(f"  FAIL  {msg}", file=sys.stderr)
        raise SystemExit(1)


def _ctx() -> Context:
    sh = Shelves(MemoryShelf("input"), MemoryShelf("deliverables"), MemoryShelf("shared"))
    return Context(shelves=sh, meter=BudgetMeter(10 ** 12, label="test"), dry_run=True)


def _leaf(sid: str, fn) -> State:
    return State(id=sid, kind=LEAF, activity=Procedure(sid.split("/")[-1], fn))


def test_unhandled_event_does_not_hang():
    """A leaf raises an event no transition handles. The run must still complete via the
    normal done edge — the orphan event is discarded, not retried forever."""
    runs = []

    def body(payload, ctx):
        runs.append(1)
        ctx.raise_event("ghost")          # nothing anywhere handles "ghost"
        return Output("done")

    leaf = _leaf("c/work", body)
    root = State(id="c", kind=COMPOUND, children=[leaf], initial=leaf.id,
                 transitions=[Transition(source=leaf.id, event=EV_DONE, target=T_DONE)])
    result = Interpreter(Statechart(root=root), _ctx()).run()
    check(result.ok, "run completes despite an unhandled named event")
    check(len(runs) == 1, f"leaf ran exactly once (no spin); got {len(runs)}")


def test_event_takes_priority_over_done():
    """A leaf succeeds (done) AND raises a named event; the named-event edge is taken in
    preference to the done edge at the same level."""
    seen = []

    def body(payload, ctx):
        ctx.raise_event("redirect")
        return Output("ok")               # also a 'done' — but redirect should win

    def mark(_r, _c):
        seen.append("redirect-taken")

    leaf = _leaf("c/work", body)
    root = State(id="c", kind=COMPOUND, children=[leaf], initial=leaf.id, transitions=[
        Transition(source=leaf.id, event="redirect", target=T_DONE, action=mark),
        Transition(source=leaf.id, event=EV_DONE, target=T_ERROR),  # should NOT be taken
    ])
    result = Interpreter(Statechart(root=root), _ctx()).run()
    check(seen == ["redirect-taken"], "named-event edge fired in preference to done")
    check(result.ok, "outcome followed the redirect (@done), not the done->@error edge")


def test_event_propagates_through_multiple_levels():
    """A leaf three levels deep raises an event handled ONLY by the outermost loop —
    proving it bubbles past two non-handling superstates."""
    runs = []

    def body(payload, ctx):
        runs.append(1)
        if len(runs) < 2:                 # fail once, succeed on the retry
            ctx.raise_event("escalate")
            return Error("first try")
        return Output("ok")

    leaf = _leaf("top/mid/inner/work", body)
    inner = State(id="top/mid/inner", kind=COMPOUND, children=[leaf], initial=leaf.id,
                  transitions=[Transition(source=leaf.id, event=EV_DONE, target=T_DONE),
                               Transition(source=leaf.id, event=EV_ERROR, target=T_ERROR)])
    mid = State(id="top/mid", kind=COMPOUND, children=[inner], initial=inner.id,
                transitions=[Transition(source=inner.id, event=EV_DONE, target=T_DONE),
                             Transition(source=inner.id, event=EV_ERROR, target=T_ERROR)])
    top = State(id="top", kind=LOOP, children=[mid], initial=mid.id, transitions=[
        # only the outermost level handles "escalate" — two levels above the raiser
        Transition(source=mid.id, event="escalate", target=mid.id, guard=lambda r, c: len(runs) < 2),
        Transition(source=mid.id, event=EV_DONE, target=T_DONE),
        Transition(source=mid.id, event=EV_ERROR, target=T_ERROR),
    ])
    result = Interpreter(Statechart(root=top), _ctx()).run()
    check(result.ok, "escalated event handled at the top led to eventual success")
    check(len(runs) == 2, f"retried once via top-level handler; leaf ran 2x, got {len(runs)}")


def test_guard_gates_on_payload():
    """A transition guard reads the raised event's payload (wrapped as the Result) and
    only admits the transition for the matching payload."""
    runs = []

    def body(payload, ctx):
        runs.append(1)
        ctx.raise_event("retry", {"reason": "transient"} if len(runs) < 2 else {"reason": "fatal"})
        return Error("x")

    # Retry only while the payload reason is 'transient'.
    def transient_only(result, ctx):
        return isinstance(result.value, dict) and result.value.get("reason") == "transient"

    leaf = _leaf("c/work", body)
    root = State(id="c", kind=LOOP, children=[leaf], initial=leaf.id, transitions=[
        Transition(source=leaf.id, event="retry", target=leaf.id, guard=transient_only),
        Transition(source=leaf.id, event=EV_ERROR, target=T_ERROR),
    ])
    result = Interpreter(Statechart(root=root), _ctx()).run()
    check(not result.ok, "payload-gated guard stopped retrying once reason became 'fatal'")
    check(len(runs) == 2, f"ran 2x (transient retry, then fatal -> error edge); got {len(runs)}")


def test_event_consumed_once():
    """A handled named event is consumed — it does not linger and re-fire on the next
    child completion."""
    fires = []

    def body(payload, ctx):
        n = ctx.tick("n")
        if n == 1:
            ctx.raise_event("once")
            return Error("first")
        return Output("ok")               # second pass: clean, no event

    def on_once(_r, _c):
        fires.append("once")

    leaf = _leaf("c/work", body)
    root = State(id="c", kind=LOOP, children=[leaf], initial=leaf.id, transitions=[
        Transition(source=leaf.id, event="once", target=leaf.id, action=on_once),
        Transition(source=leaf.id, event=EV_DONE, target=T_DONE),
        Transition(source=leaf.id, event=EV_ERROR, target=T_ERROR),
    ])
    result = Interpreter(Statechart(root=root), _ctx()).run()
    check(result.ok, "second pass completed cleanly")
    check(fires == ["once"], f"the event fired exactly once, no re-fire; got {fires}")


def test_multiple_events_one_child():
    """A child raises two events in one completion; both are queued and the matching
    handler is selected (the other has no handler and is discarded)."""
    fired = []

    def body(payload, ctx):
        ctx.raise_event("alpha")
        ctx.raise_event("beta")
        return Output("ok")

    leaf = _leaf("c/work", body)
    root = State(id="c", kind=COMPOUND, children=[leaf], initial=leaf.id, transitions=[
        # only beta has a handler; alpha must not break selection
        Transition(source=leaf.id, event="beta", target=T_DONE, action=lambda r, c: fired.append("beta")),
        Transition(source=leaf.id, event=EV_DONE, target=T_ERROR),
    ])
    result = Interpreter(Statechart(root=root), _ctx()).run()
    check(fired == ["beta"], f"the handled event (beta) fired; got {fired}")
    check(result.ok, "selection tolerated a second, unhandled event from the same child")


def main() -> int:
    test_unhandled_event_does_not_hang()
    test_event_takes_priority_over_done()
    test_event_propagates_through_multiple_levels()
    test_guard_gates_on_payload()
    test_event_consumed_once()
    test_multiple_events_one_child()
    print(f"\nevent_dispatch: {_PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
