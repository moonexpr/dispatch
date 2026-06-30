#!/usr/bin/env python3
"""slice_supervisor.py — run an engineer issue one *slice* (unit) at a time, under a
supervising statechart that retries a failed slice with its prior attempts in context.

Motivation: a single long-living engineer session that implements every unit in one
shot accumulates rate-limit throttling until it blows the wall-clock cap and commits
NOTHING (all-or-nothing). Slicing bounds each session to one unit, commits each as it
lands (partial progress is preserved), and isolates a failing unit from its siblings.

The supervisor is expressed in the engine's own statechart idiom (ADR-001), built on
the event-dispatch primitive (``Context.raise_event`` + the interpreter's named-event
propagation):

  * The engineer (the slice *body*) only REPORTS an outcome: on success it commits and
    returns ``Output``; on failure it records the attempt and raises ``slice.failed``.
    It never decides the retry policy — that is the supervisor's job.
  * The supervising LOOP owns the cursor (which slice), the retry cap, and the
    needs-human policy, entirely in its transition table:
      - ``slice.failed`` + ``attempts <= retry_cap``  → re-enter the SAME slice (retry).
      - ``slice.failed`` (exhausted)                  → flag needs-human, advance.
      - ``done``                                       → advance to the next slice (or finish).

Backend-agnostic and side-effect-injected: ``run_slice`` and ``commit_slice`` are
passed in, so the same supervisor drives a real EngineerAgent session in production and
a deterministic stub in tests (no live model needed to verify the control flow).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

from engine.actions.action import Context, Procedure
from engine.actions.interpreter import Interpreter
from engine.actions.result import Error, Output
from engine.actions.statechart import (
    EV_DONE,
    LEAF,
    LOOP,
    T_DONE,
    State,
    Statechart,
    Transition,
)

# run_slice(unit, attempt, prior_attempts) -> (ok, summary). attempt is 1-based; prior_
# attempts are the summaries of this slice's earlier failed tries (the history to inject).
RunSlice = Callable[[Dict[str, Any], int, List[str]], Tuple[bool, str]]
# commit_slice(unit) -> None. Called once, when a slice's session succeeds.
CommitSlice = Callable[[Dict[str, Any]], None]

EV_SLICE_FAILED = "slice.failed"

# Shelf keys (namespaced) the supervisor threads through ctx.shelves.shared.
_CURSOR = "slice.cursor"
_ATTEMPTS = "slice.{}.attempts"
_HISTORY = "slice.{}.history"


@dataclass
class SliceReport:
    """Aggregate outcome of a supervised per-slice run."""

    landed: List[str] = field(default_factory=list)        # unit ids that committed
    needs_human: List[str] = field(default_factory=list)   # unit ids exhausted after retries
    attempts: Dict[str, int] = field(default_factory=dict)  # unit id -> total sessions run

    @property
    def all_landed(self) -> bool:
        return not self.needs_human and bool(self.landed)


def _uid(unit: Dict[str, Any], index: int) -> str:
    return str(unit.get("id") or f"u{index + 1}")


def run_slices_supervised(
    units: List[Dict[str, Any]],
    *,
    run_slice: RunSlice,
    commit_slice: CommitSlice,
    ctx: Context,
    retry_cap: int = 2,
) -> SliceReport:
    """Drive ``units`` one slice at a time through the supervising statechart and return
    a :class:`SliceReport`. Each slice gets ``1 + retry_cap`` sessions at most; a failed
    slice retries with its prior-attempt summaries passed to ``run_slice`` as context.

    ``ctx`` supplies the shelves the supervisor stores cursor/attempts/history on (use a
    descended context in production so it shares the run's shared shelf)."""
    report = SliceReport()
    sh = ctx.shelves.shared
    # Reset our cursor/attempt/history keys so a re-run (e.g. the outer monitor loop
    # retrying the whole attempt) starts clean rather than resuming a stale cursor.
    sh.put(_CURSOR, 0)
    for i in range(len(units)):
        sh.put(_ATTEMPTS.format(i), 0)
        sh.put(_HISTORY.format(i), [])

    def _slice_body(payload: Any, c: Context) -> Any:
        cursor = sh.get(_CURSOR, 0)
        unit = units[cursor]
        uid = _uid(unit, cursor)
        attempt = sh.get(_ATTEMPTS.format(cursor), 0) + 1
        sh.put(_ATTEMPTS.format(cursor), attempt)
        report.attempts[uid] = attempt
        prior: List[str] = list(sh.get(_HISTORY.format(cursor), []))
        ok, summary = run_slice(unit, attempt, prior)
        if ok:
            commit_slice(unit)
            if uid not in report.landed:
                report.landed.append(uid)
            return Output({"cursor": cursor, "uid": uid, "ok": True})
        prior.append(f"attempt {attempt}: {summary}")
        sh.put(_HISTORY.format(cursor), prior)
        c.raise_event(EV_SLICE_FAILED, {"cursor": cursor, "uid": uid, "attempt": attempt})
        return Error(f"slice {uid} attempt {attempt} failed: {summary}")

    leaf_id, loop_id = "slices/slice", "slices"

    def _attempts(c: Context) -> int:
        return sh.get(_ATTEMPTS.format(sh.get(_CURSOR, 0)), 0)

    def _is_last(c: Context) -> bool:
        return sh.get(_CURSOR, 0) >= len(units) - 1

    def _advance(_result: Any, c: Context) -> None:
        sh.put(_CURSOR, sh.get(_CURSOR, 0) + 1)

    def _give_up_then_advance(_result: Any, c: Context) -> None:
        cursor = sh.get(_CURSOR, 0)
        uid = _uid(units[cursor], cursor)
        if uid not in report.needs_human:
            report.needs_human.append(uid)
        sh.put(_CURSOR, cursor + 1)

    def _give_up_last(_result: Any, c: Context) -> None:
        cursor = sh.get(_CURSOR, 0)
        uid = _uid(units[cursor], cursor)
        if uid not in report.needs_human:
            report.needs_human.append(uid)

    leaf = State(id=leaf_id, kind=LEAF, activity=Procedure("slice", _slice_body))
    # Transition order = priority (first match with a true guard wins). The supervisor's
    # entire policy lives here; the body never branches on it.
    transitions = [
        # Retry the SAME slice while the cap is not exhausted (the supervised retry).
        Transition(source=leaf_id, event=EV_SLICE_FAILED, target=leaf_id,
                   guard=lambda r, c: _attempts(c) <= retry_cap, guard_name="attempts<=cap"),
        # Exhausted on the final slice → flag needs-human and finish.
        Transition(source=leaf_id, event=EV_SLICE_FAILED, target=T_DONE,
                   guard=lambda r, c: _is_last(c), guard_name="exhausted&last",
                   action=_give_up_last),
        # Exhausted with slices remaining → flag needs-human, advance, keep going.
        Transition(source=leaf_id, event=EV_SLICE_FAILED, target=leaf_id,
                   guard=None, action=_give_up_then_advance),
        # Slice succeeded on the final slice → done.
        Transition(source=leaf_id, event=EV_DONE, target=T_DONE,
                   guard=lambda r, c: _is_last(c), guard_name="last"),
        # Slice succeeded with slices remaining → advance to the next.
        Transition(source=leaf_id, event=EV_DONE, target=leaf_id,
                   guard=None, action=_advance),
    ]
    loop = State(id=loop_id, kind=LOOP, children=[leaf], initial=leaf_id, transitions=transitions)
    Interpreter(Statechart(root=loop), ctx).run()
    return report
