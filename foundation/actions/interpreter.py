#!/usr/bin/env python3
"""interpreter.py — the single run-to-completion statechart interpreter.

One interpreter drives any ``Statechart``. It owns the agenda; states never call
each other directly. It consults the chart's first-class ``Transition`` objects to
decide where to go next — it never hard-codes "then run step 2" — so the same
generic interpreter runs any compiled controller.

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

Leaf module: stdlib + sibling foundation leaves.
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


class Observer:
    """No-op base for a run observer. The Interpreter calls these on every state
    enter/leave and completion event; override the ones you care about. An observer
    is passive telemetry — it must never mutate the run or raise (the Interpreter
    swallows observer exceptions), so watching a run cannot change its outcome."""

    def on_enter(self, state_id: str, kind: str) -> None: ...
    def on_leave(self, state_id: str, ok: bool) -> None: ...
    def on_event(self, event: Dict[str, Any]) -> None: ...


@dataclass
class Event:
    """An event on the agenda. Completion events (``EV_DONE``/``EV_ERROR``) are
    raised by a finishing leaf and select among the immediate superstate's edges.
    A *named* event (``external=True``) is raised by an activity via
    ``ctx.raise_event`` and PROPAGATES up the active configuration until a
    superstate handles it — the broadcast seam (ADR-001). ``payload`` carries the
    raised value; it is wrapped as the Result handed to a named transition's
    action/guard."""

    source: str
    name: str  # EV_DONE | EV_ERROR | a named event
    result: Result
    external: bool = False
    payload: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "event": self.name, "ok": self.result.ok,
                "external": self.external}


@dataclass
class Interpreter:
    """Drives one chart to completion and returns the Result that reached the top.
    ``events`` is the transition-level audit; ``config`` + ``history`` are the
    serializable resume seam. Seed ``history`` from a prior ``snapshot()`` (via
    ``restore``) and set ``resuming`` to re-enter at the deepest active
    configuration instead of restarting — that is deep history (H*)."""

    chart: Statechart
    ctx: Any
    semantics: str = "async"  # async (UML) | sync (STATEMATE) — sync is a future seam
    config: Configuration = field(default_factory=Configuration)
    events: List[Dict[str, Any]] = field(default_factory=list)
    history: Dict[str, str] = field(default_factory=dict)
    resuming: bool = False  # when True, _initial_child re-enters from history (H*)
    observer: Any = None  # optional; falls back to ctx.observer (rides descend)
    _internal: Deque[Event] = field(default_factory=deque)
    _external: Deque[Event] = field(default_factory=deque)  # broadcast seam (unused)

    def run(self, payload: Any = None) -> Result:
        return self._run_state(self.chart.root, payload)

    # -- observer notification ---------------------------------------------
    def _notify(self, method: str, *args: Any) -> None:
        """Fire an observer hook. The observer lives on ``ctx.observer`` (so it
        rides ``ctx.descend`` into nested Programs) or on this interpreter. It is
        passive: any exception it raises is swallowed so telemetry can never break
        the run."""
        obs = self.observer or getattr(self.ctx, "observer", None)
        if obs is None:
            return
        fn = getattr(obs, method, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 — an observer must never break the run
            pass

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
        self._notify("on_enter", state.id, state.kind)
        ok = False
        try:
            if state.activity is None:
                result: Result = Output(payload)
            else:
                # A Program activity enters a nested chart here — the depth operator.
                result = state.activity.run(payload, self.ctx)
            ok = result.ok
            return result
        finally:
            self._notify("on_leave", state.id, ok)
            self.config.leave(state.id)

    def _run_super(self, state: State, payload: Any) -> Result:
        """Compound (OR-superstate) and Loop share one driver: run the active
        child, raise its completion event, then SELECT the next transition from
        the chart. A Loop differs only in that a transition may target the body
        again (the guarded self-transition); the live iteration count is published
        on ``ctx.counters[state.id]`` for its guard to read."""
        self.config.enter(state.id)
        self._notify("on_enter", state.id, state.kind)
        result: Result = Output(payload)
        child_ids = {c.id for c in state.children}
        try:
            cur_id = self._initial_child(state)
            feed = payload
            iteration = 0
            while cur_id:
                child = self.chart.index[cur_id]
                self.history[state.id] = cur_id  # resume seam: deepest active child
                if state.kind == LOOP:
                    iteration += 1
                    self.ctx.counters[state.id] = iteration
                result = self._run_state(child, feed)
                # Collect any named events the child subtree raised onto the external
                # queue, then give a SUPERVISING transition at this level first refusal:
                # a named-event edge (e.g. slice.failed -> retry) is preferred over the
                # plain done/error completion edge. Unhandled named events stay queued
                # and bubble to the parent frame (source rewritten on exit).
                self._collect_raised(cur_id)
                sup, sup_event = self._select_external(state, cur_id)
                if sup is not None:
                    self._emit(sup_event)
                    nxt, edge_result = sup, sup_event.result
                else:
                    event = EV_DONE if result.ok else EV_ERROR
                    self._emit(Event(source=cur_id, name=event, result=result))
                    nxt, edge_result = self._select(state, cur_id, event, result), result
                if nxt is None:
                    break  # implicit completion with the child's result
                if nxt.action is not None:
                    nxt.action(edge_result, self.ctx)
                if nxt.target in (T_DONE, T_ERROR):
                    return result
                cur_id = nxt.target
                # Re-enter with the child's value; an error-carrying edge (e.g. a
                # supervised retry on a failed slice) has no .value, so keep the
                # prior feed — the retry re-runs the body with the same input.
                feed = getattr(result, "value", feed)
            return result
        finally:
            self._notify("on_leave", state.id, result.ok)
            # Bubble this frame's still-unhandled named events to the parent: from the
            # parent's perspective they emerged from THIS superstate, so re-key their
            # source to state.id (one level of propagation per returning frame).
            for ev in self._external:
                if ev.source in child_ids:
                    ev.source = state.id
            self.config.leave(state.id)

    def _run_parallel(self, state: State, payload: Any) -> Result:
        """Orthogonal regions. Single-region stub: regions run sequentially and
        join on all-success; the first failing region short-circuits. Concurrency
        and cross-region events are the reserved seam."""
        self.config.enter(state.id)
        self._notify("on_enter", state.id, state.kind)
        ok = True
        try:
            outputs: List[Any] = []
            for region in state.children:
                r = self._run_state(region, payload)
                self._emit(Event(source=region.id, name=EV_DONE if r.ok else EV_ERROR, result=r))
                if not r.ok:
                    ok = False
                    return r
                outputs.append(r.value)
            return Output(outputs)
        finally:
            self._notify("on_leave", state.id, ok)
            self.config.leave(state.id)

    # -- entry / deep history ----------------------------------------------
    def _initial_child(self, state: State) -> str:
        """The child a superstate enters. Normally its ``initial`` (or first
        child). When ``resuming`` and ``history`` records a still-valid child for
        this superstate, re-enter there instead — so each superstate on the
        recorded active path resumes at its last active child (deep history),
        skipping earlier siblings that already completed. Branches the dead run
        never reached carry no history entry and enter normally."""
        default = state.initial or (state.children[0].id if state.children else "")
        if self.resuming:
            resumed = self.history.get(state.id)
            if resumed and any(c.id == resumed for c in state.children):
                return resumed
        return default

    # -- agenda + selection -------------------------------------------------
    def _emit(self, event: Event) -> None:
        """Place a completion event on the internal queue and drain it. Draining
        is the RTC discipline; with one region it is synchronous, but the queue is
        the seam where external/broadcast events will interleave between macrosteps
        under the async pin."""
        self._internal.append(event)
        while self._internal:
            ev = self._internal.popleft()
            d = ev.to_dict()
            self.events.append(d)
            self._notify("on_event", d)

    def _select(self, state: State, source: str, event: str, result: Result) -> Optional[Transition]:
        """Consult the chart's first-class transitions: the first one whose source
        and event match and whose guard admits the Result wins. No hard-coded
        next-step; control flow lives in the transition table."""
        for t in state.transitions:
            if t.source == source and t.event == event:
                if t.guard is None or t.guard(result, self.ctx):
                    return t
        return None

    def _collect_raised(self, source: str) -> None:
        """Move events an activity raised (``ctx.raise_event``) onto the external
        queue, tagged with the child id they emerged from at this frame. They are
        matched against named-event transitions here and, if unhandled, bubble up."""
        while self.ctx.raised:
            re = self.ctx.raised.pop(0)
            self._external.append(
                Event(source=source, name=re.name, result=Output(re.payload),
                      external=True, payload=re.payload)
            )

    def _select_external(self, state: State, source: str):
        """Give a named-event (broadcast) transition at THIS level first refusal over
        the plain done/error edge. Returns ``(transition, event)`` and consumes the
        event from the external queue when one matches (source + event name + guard);
        otherwise ``(None, None)`` and the events stay queued to bubble upward. The
        event's payload is wrapped as the Result the transition's guard/action sees."""
        for ev in list(self._external):
            if ev.source != source:
                continue
            for t in state.transitions:
                if t.source == source and t.event == ev.name:
                    if t.guard is None or t.guard(ev.result, self.ctx):
                        self._external.remove(ev)
                        return t, ev
        return None, None

    # -- resume seam --------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "history": dict(self.history),
            "events": list(self.events),
        }

    def restore(self, snapshot: Dict[str, Any]) -> "Interpreter":
        """Seed this interpreter from a prior :meth:`snapshot` so the next
        :meth:`run` re-enters at the deepest active configuration (deep history,
        H*) rather than restarting from zero. Returns ``self`` for chaining."""
        self.history = dict(snapshot.get("history", {}))
        self.resuming = True
        return self


def interpret(chart: Statechart, ctx: Any, payload: Any = None, *, semantics: str = "async") -> Result:
    """Run a chart to completion and return its Result. Convenience wrapper used
    by ``Controller.run``."""
    return Interpreter(chart, ctx, semantics=semantics).run(payload)


def resume(
    chart: Statechart, ctx: Any, snapshot: Dict[str, Any], payload: Any = None, *, semantics: str = "async"
) -> Result:
    """Run a chart from a prior ``snapshot`` (deep-history resume) and return its
    Result. The mirror of :func:`interpret` for the durability path: a controller
    that died mid-run resumes its nested active configuration instead of replaying
    completed work."""
    return Interpreter(chart, ctx, semantics=semantics).restore(snapshot).run(payload)
