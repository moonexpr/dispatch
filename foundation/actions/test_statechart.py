#!/usr/bin/env python3
"""test_statechart.py — closes the two outstanding ADR-001 action items.

  #9  Deep-history resume: kill a Controller mid-``work`` and assert the
      interpreter resumes its nested active configuration (deep history, H*)
      instead of restarting from zero — both for a flat sequence and for a
      nested superstate (the "nested active configuration" the ADR names).

  #6  Shared-shelf write contract: the factory wraps ``shared`` in a
      SerializedShelf (writes RTC-serialized) while ``input`` / ``deliverables``
      stay plain, and concurrent writers never lose or tear a write.

No pytest in this repo — a runnable, self-asserting module (exit 0 = pass).
Run: ``python3 foundation/actions/test_statechart.py``.
"""
from __future__ import annotations

import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))  # <root>/foundation/actions -> <root>
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from foundation.actions.action import BudgetMeter, Context, Procedure  # noqa: E402
from foundation.actions.control import Sequence  # noqa: E402
from foundation.actions.factory import MockActionFactory  # noqa: E402
from foundation.actions.interpreter import Interpreter, resume  # noqa: E402
from foundation.actions.shelf import MemoryShelf, SerializedShelf, Shelves  # noqa: E402
from foundation.actions.statechart import Statechart  # noqa: E402

_PASSED = 0


def _ok(msg: str) -> None:
    global _PASSED
    _PASSED += 1
    print(f"  ok  {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}", file=sys.stderr)
    raise SystemExit(1)


def check(cond: bool, msg: str) -> None:
    _ok(msg) if cond else _fail(msg)


def _ctx() -> Context:
    shelves = Shelves(MemoryShelf("input"), MemoryShelf("deliverables"), MemoryShelf("shared"))
    return Context(shelves=shelves, meter=BudgetMeter(10 ** 12, label="test"), dry_run=True)


def _leaf(name: str, runlog: list, *, fail_set: set) -> Procedure:
    """A Procedure that logs the fact it ran, and raises while its name is in
    ``fail_set`` — letting one chart model both the dying session (name fails)
    and the resumed session (name removed from the set)."""

    def fn(payload, ctx):
        runlog.append(name)
        if name in fail_set:
            raise RuntimeError(f"killed at {name}")
        return f"{name}-out"

    return Procedure(name, fn)


def test_resume_flat_sequence() -> None:
    """Sequence[A, B, C], die in B. Resume runs B then C and skips A."""
    fail_set = {"B"}
    log1: list = []
    seq = Sequence([_leaf("A", log1, fail_set=fail_set),
                    _leaf("B", log1, fail_set=fail_set),
                    _leaf("C", log1, fail_set=fail_set)], name="work")
    chart = Statechart(root=seq.compile("work"))

    interp = Interpreter(chart, _ctx())
    r1 = interp.run()
    check(not r1.ok, "session 1 fails at B (the kill)")
    check(log1 == ["A", "B"], "session 1 ran A then died in B (C never reached)")
    snap = interp.snapshot()
    check(snap["history"]["work"].endswith("B"), "history records B as the deepest active child")

    # The fix: B now succeeds. Resume from the snapshot on a fresh interpreter.
    fail_set.clear()
    log2: list = []
    seq2 = Sequence([_leaf("A", log2, fail_set=fail_set),
                     _leaf("B", log2, fail_set=fail_set),
                     _leaf("C", log2, fail_set=fail_set)], name="work")
    chart2 = Statechart(root=seq2.compile("work"))
    r2 = resume(chart2, _ctx(), snap)
    check(r2.ok, "resumed session completes successfully")
    check(log2 == ["B", "C"], "resume re-enters at B and runs forward; A is NOT replayed")


def test_resume_nested_configuration() -> None:
    """Sequence[A, Inner=Sequence[B1, B2], C], die in B2. Resume restores the
    NESTED active configuration: it re-enters Inner at B2 (skipping A and B1)."""
    fail_set = {"B2"}

    def build(log: list) -> Statechart:
        inner = Sequence([_leaf("B1", log, fail_set=fail_set),
                          _leaf("B2", log, fail_set=fail_set)], name="inner")
        outer = Sequence([_leaf("A", log, fail_set=fail_set), inner,
                          _leaf("C", log, fail_set=fail_set)], name="work")
        return Statechart(root=outer.compile("work"))

    log1: list = []
    interp = Interpreter(build(log1), _ctx())
    r1 = interp.run()
    check(not r1.ok, "session 1 fails inside the nested Inner sequence (at B2)")
    check(log1 == ["A", "B1", "B2"], "session 1 ran A, B1, then died in B2")
    snap = interp.snapshot()
    check(any(k.endswith("inner") for k in snap["history"]),
          "history records the nested superstate's active child")

    fail_set.clear()
    log2: list = []
    r2 = resume(build(log2), _ctx(), snap)
    check(r2.ok, "resumed nested session completes")
    check(log2 == ["B2", "C"],
          "deep-history resume re-enters Inner at B2 (A and B1 skipped) then runs C")


def test_shared_shelf_is_write_serialized() -> None:
    """ADR-001 #6: the factory wraps ``shared`` (and only ``shared``) so its
    writes are RTC-serialized; the wrapper is write-correct and lock-guarded."""
    shelves = MockActionFactory().shelves()
    check(isinstance(shelves.shared, SerializedShelf), "shared shelf is write-serialized")
    check(not isinstance(shelves.input, SerializedShelf), "input shelf stays plain (last-write-wins)")
    check(not isinstance(shelves.deliverables, SerializedShelf), "deliverables shelf stays plain")

    s = shelves.shared
    s.put("k", 1)
    check(s.get("k") == 1 and "k" in s, "put/get/has round-trip through the wrapper")
    s.update({"a": 1, "b": 2})
    check(s.get("a") == 1 and s.get("b") == 2, "update lands through the wrapper")
    s.drop("k")
    check(not s.has("k"), "drop removes through the wrapper")

    # Concurrency: many threads hammering put/update; every key must survive (no
    # lost or torn writes under the serialized contract).
    fresh = SerializedShelf(MemoryShelf("shared"))
    n_threads, per = 8, 50

    def writer(tid: int) -> None:
        for i in range(per):
            fresh.put(f"t{tid}-{i}", tid)
            fresh.update({f"u{tid}-{i}": tid})

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    expected = n_threads * per * 2  # one put + one update key per iteration
    check(len(fresh.keys()) == expected,
          f"all {expected} concurrent writes survived (no lost/torn writes)")


def _supervised_chart(runs, injected, *, retry_cap):
    """A leaf that raises ``slice.failed`` + errors until it has run > retry_cap
    times, nested inside an inner COMPOUND that does NOT handle that event, inside
    a supervising LOOP that DOES (guard caps retries; action records a 'history'
    injection). Exercises (a) up-propagation past the non-handling parent and
    (b) the guarded event-triggered retry — the supervisor pattern."""
    from foundation.actions.action import Procedure
    from foundation.actions.statechart import (
        COMPOUND, EV_DONE, EV_ERROR, LEAF, LOOP, T_DONE, T_ERROR, State, Statechart, Transition,
    )
    from foundation.actions.result import Error, Output

    def work(payload, ctx):
        runs.append(1)
        if len(runs) <= retry_cap:           # fail-then-raise for the first cap runs
            ctx.raise_event("slice.failed", {"attempt": len(runs)})
            return Error(f"slice failed attempt {len(runs)}")
        return Output(f"slice ok after {len(runs)}")

    leaf = State(id="sup/inner/work", kind=LEAF, activity=Procedure("work", work))
    inner = State(  # deliberately has NO slice.failed transition -> event must bubble up
        id="sup/inner", kind=COMPOUND, children=[leaf], initial=leaf.id,
        transitions=[Transition(source=leaf.id, event=EV_DONE, target=T_DONE),
                     Transition(source=leaf.id, event=EV_ERROR, target=T_ERROR)],
    )

    def retry_guard(result, ctx):
        return len(runs) <= retry_cap        # retry only while not yet succeeded

    def inject_history(result, ctx):
        injected.append(result.value)        # the 'prior attempt' the supervisor would inject

    sup = State(
        id="sup", kind=LOOP, children=[inner], initial=inner.id,
        transitions=[
            Transition(source=inner.id, event="slice.failed", target=inner.id,
                       guard=retry_guard, guard_name="retries<cap", action=inject_history),
            Transition(source=inner.id, event=EV_DONE, target=T_DONE),
            Transition(source=inner.id, event=EV_ERROR, target=T_ERROR),
        ],
    )
    return Statechart(root=sup)


def test_named_event_propagates_and_retries():
    """A leaf raises a named event; a SUPERVISING loop two levels up catches it and
    retries with a guard, while the immediate parent ignores it (up-propagation)."""
    runs, injected = [], []
    chart = _supervised_chart(runs, injected, retry_cap=2)  # fail twice, then succeed
    result = Interpreter(chart, _ctx()).run()
    check(result.ok, "supervised chart completes once the slice eventually succeeds")
    check(len(runs) == 3, f"slice ran 3x (2 supervised retries + success); got {len(runs)}")
    check(len(injected) == 2, f"supervisor action fired once per retry; got {len(injected)}")


def test_named_event_retry_exhaustion():
    """When the retry guard is exhausted, the named event is NOT consumed; the failure
    falls through to the error edge and propagates out (no infinite retry)."""
    runs, injected = [], []
    chart = _supervised_chart(runs, injected, retry_cap=99)  # always fails
    # cap retries at 1 by overriding the guard via a tighter chart:
    runs2, injected2 = [], []
    chart2 = _supervised_chart(runs2, injected2, retry_cap=99)
    # find the slice.failed transition and tighten its guard to allow exactly 1 retry
    for t in chart2.root.transitions:
        if t.event == "slice.failed":
            t.guard = lambda result, ctx: len(runs2) < 2
    result = Interpreter(chart2, _ctx()).run()
    check(not result.ok, "exhausted retries surface as a failure, not a hang")
    check(len(runs2) == 2, f"slice ran exactly 2x (initial + 1 retry) then gave up; got {len(runs2)}")


def main() -> int:
    test_resume_flat_sequence()
    test_resume_nested_configuration()
    test_shared_shelf_is_write_serialized()
    test_named_event_propagates_and_retries()
    test_named_event_retry_exhaustion()
    print(f"\nstatechart: {_PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
