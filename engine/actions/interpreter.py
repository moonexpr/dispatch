#!/usr/bin/env python3
"""interpreter.py — the single run-to-completion statechart interpreter.

One interpreter drives any ``Statechart``. It owns the agenda; states never call
each other directly. It consults the chart's first-class ``Transition`` objects to
decide where to go next — it never hard-codes "then run step 2" — so the same
generic engine runs any compiled controller.

Semantics (pinned): **asynchronous / UML-style RTC**. A completing leaf raises a
``done``/``error`` event onto the *internal* queue; the interpreter drains the
internal queue to a stable configuration (one region's microstep sequence) before
it would dequeue an *external* event. The external queue is the broadcast / cross-
region seam — reserved and empty today (single region), wired when controllers run
as peers.

How the addendum's constructs execute here:
  * Result is the trigger — ``output -> done`` edge, ``error -> error`` edge.
  * a Governor's denial is just an ``Error`` Result, so it routes through the
    ``error`` transition like any other failure (Governor outcomes go through the
    transition layer, not an ad-hoc branch).
  * a Loop is a superstate whose self-transition carries the iteration guard; the
    interpreter exposes the live iteration count on ``ctx.counters[loop_id]`` so
    the guard can read it.
  * a Program leaf runs a *nested* interpreter (the depth operator) via
    ``controller.run(payload, ctx.descend())``.

Leaf module: stdlib + sibling engine leaves.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

from .result import Output, Result
from .statechart import (
    COMPOUND,
    EV_DONE,
    EV_ERROR,
    FINAL,
    LEAF,
    LOOP,
    PARALLEL,
    Configuration,
    State,
    Statechart,
    T_DONE,
    T_ERROR,
    Transition,
)


@dataclass
class Event:
    """A completion event placed on the agenda by a finishing leaf."""

    source: str
    name: str  # EV_DONE | EV_ERROR
    result: Result
    external: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "event": self.name, "ok": self.result.ok}


@dataclass
class Interpreter:
    """Drives one chart to completion and returns the Result that reached the top.
    ``events`` is the transition-level audit; ``config`` + ``history`` are the
    serializable resume seam."""

    chart: Statechart
    ctx: Any
    semantics: str = "async"  # async (UML) | sync (STATEMATE) — sync is a future seam
    config: Configuration = field(default_factory=Configuration)
    events: List[Dict[str, Any]] = field(default_factory=list)
    history: Dict[str, str] = field(default_factory=dict)
    _internal: Deque[Event] = field(default_factory=deque)
    _external: Deque[Event] = field(default_factory=deque)  # broadcast seam (unused)

    def run(self, payload: Any = None) -> Result:
        return self._run_state(self.chart.root, payload)

    # -- dispatch -----------------------------------------------------------
    def _run_state(self, state: State, payload: Any) -> Result:
        if state.kind == LEAF:
            return self._run_leaf(state, payload)
        if state.kind in (COMPOUND, LOOP):
            return self._run_super(state, payload)
        if state.kind == PARALLEL:
            return self._run_parallel(state, payload)
        if state.kind == FINAL:
            return Output(payload)
        raise ValueError(f"unknown state kind {state.kind!r} for {state.id!r}")

    def _run_leaf(self, state: State, payload: Any) -> Result:
        self.config.enter(state.id)
        try:
            if state.activity is None:
                return Output(payload)
            # A Program activity enters a nested chart here — the depth operator.
            return state.activity.run(payload, self.ctx)
        finally:
            self.config.leave(state.id)

    def _run_super(self, state: State, payload: Any) -> Result:
        """Compound (OR-superstate) and Loop share one driver: run the active
        child, raise its completion event, then SELECT the next transition from
        the chart. A Loop differs only in that a transition may target the body
        again (the guarded self-transition); the live iteration count is published
        on ``ctx.counters[state.id]`` for its guard to read."""
        self.config.enter(state.id)
        try:
            cur_id = state.initial or (state.children[0].id if state.children else "")
            feed = payload
            result: Result = Output(payload)
            iteration = 0
            while cur_id:
                child = self.chart.index[cur_id]
                self.history[state.id] = cur_id  # resume seam: deepest active child
                if state.kind == LOOP:
                    iteration += 1
                    self.ctx.counters[state.id] = iteration
                result = self._run_state(child, feed)
                event = EV_DONE if result.ok else EV_ERROR
                self._emit(Event(source=cur_id, name=event, result=result))
                nxt = self._select(state, cur_id, event, result)
                if nxt is None:
                    break  # implicit completion with the child's result
                if nxt.action is not None:
                    nxt.action(result, self.ctx)
                if nxt.target in (T_DONE, T_ERROR):
                    return result
                cur_id = nxt.target
                feed = result.value
            return result
        finally:
            self.config.leave(state.id)

    def _run_parallel(self, state: State, payload: Any) -> Result:
        """Orthogonal regions. Single-region stub: regions run sequentially and
        join on all-success; the first failing region short-circuits. Concurrency
        and cross-region events are the reserved seam."""
        self.config.enter(state.id)
        try:
            outputs: List[Any] = []
            for region in state.children:
                r = self._run_state(region, payload)
                self._emit(Event(source=region.id, name=EV_DONE if r.ok else EV_ERROR, result=r))
                if not r.ok:
                    return r
                outputs.append(r.value)
            return Output(outputs)
        finally:
            self.config.leave(state.id)

    # -- agenda + selection -------------------------------------------------
    def _emit(self, event: Event) -> None:
        """Place a completion event on the internal queue and drain it. Draining
        is the RTC discipline; with one region it is synchronous, but the queue is
        the seam where external/broadcast events will interleave between macrosteps
        under the async pin."""
        self._internal.append(event)
        while self._internal:
            ev = self._internal.popleft()
            self.events.append(ev.to_dict())

    def _select(self, state: State, source: str, event: str, result: Result) -> Optional[Transition]:
        """Consult the chart's first-class transitions: the first one whose source
        and event match and whose guard admits the Result wins. No hard-coded
        next-step; control flow lives in the transition table."""
        for t in state.transitions:
            if t.source == source and t.event == event:
                if t.guard is None or t.guard(result, self.ctx):
                    return t
        return None

    # -- resume seam --------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "history": dict(self.history),
            "events": list(self.events),
        }


def interpret(chart: Statechart, ctx: Any, payload: Any = None, *, semantics: str = "async") -> Result:
    """Run a chart to completion and return its Result. Convenience wrapper used
    by ``Controller.run``."""
    return Interpreter(chart, ctx, semantics=semantics).run(payload)
