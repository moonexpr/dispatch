#!/usr/bin/env python3
"""action.py — the Action contract and its three subtypes.

An Action is the atom of execution. Its contract is uniform: ``run(payload, ctx)
-> Result``. Three subtypes specialise *how* the work happens, never *what shape*
comes back:

  * ``Procedure`` — a deterministic program (LEAF; no internal control flow).
  * ``Inference`` — an agent invocation (LEAF). The *runner* is injected by the
    factory, so the same ``Inference`` object is mock or real depending only on
    which factory built it. This is the inversion-of-control hinge.
  * ``Program``   — wraps a Controller; the ONLY recursion point, bounded to 5
    nesting levels.

``run()`` is shared and final: it invokes the subtype, normalises the return into
a ``Result``, turns any raised exception into an ``Error`` (failures travel as
data, never as exceptions through control flow), and records an audit line on the
``Context``. Subtypes implement ``_invoke``.

The ``Context`` threads execution state through a run — one mutable record
shared across the whole tree (shelves, budget meter, dry-run flag, audit trace,
iteration counters).

Leaf module: imports only stdlib + sibling foundation leaves.
"""
from __future__ import annotations

import os
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

from foundation.runtime import Logger

from .result import ActionError, Error, Output, ProgramError, Result
from .shelf import Shelves


def _debug_on() -> bool:
    """Per-action streaming trace toggle (DISPATCH_DEBUG / -v). Read live so an
    entry point can enable it after import."""
    return os.environ.get("DISPATCH_DEBUG") not in (None, "", "0")


class BudgetMeter:
    """A running token tally with an optional parent. Charging a child also
    charges its parent, so a scoped child meter and an enclosing parent meter
    are both enforced by the same call. Pure mechanism — domain budget figures
    are supplied by callers, not baked in here."""

    def __init__(self, total: int, *, label: str = "", parent: Optional["BudgetMeter"] = None) -> None:
        self.total = int(total)
        self.spent = 0
        self.label = label
        self.parent = parent

    def charge(self, n: int) -> int:
        n = int(n)
        self.spent += n
        if self.parent is not None:
            self.parent.charge(n)
        return self.remaining

    @property
    def remaining(self) -> int:
        return self.total - self.spent

    def would_exceed(self, n: int) -> bool:
        if (self.spent + int(n)) > self.total:
            return True
        if self.parent is not None:
            return self.parent.would_exceed(n)
        return False

    def child(self, total: int, *, label: str = "") -> "BudgetMeter":
        return BudgetMeter(total, label=label, parent=self)

    def to_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "total": self.total, "spent": self.spent, "remaining": self.remaining}


@dataclass
class RaisedEvent:
    """A named event an activity raises via :meth:`Context.raise_event` for the
    statechart interpreter to dispatch UP the active configuration (the broadcast
    seam, ADR-001 action items 4 & 8). Distinct from a Result's ``done``/``error``
    completion edge: a raised event names a condition (e.g. ``slice.failed``) that a
    *supervising* superstate handles, not just the immediate parent's edges."""

    name: str
    payload: Any = None


@dataclass
class Context:
    """Execution state threaded through one Controller run and every Action,
    Sequence, Loop and nested Program inside it. Mutable and *shared*: ``descend``
    bumps ``depth`` for a nested Program but keeps the same shelves, meter, trace
    and counters, so budget, audit and iteration caps see the whole tree."""

    shelves: Shelves
    meter: BudgetMeter
    log: Logger = field(default_factory=lambda: Logger("foundation.actions"))
    dry_run: bool = True
    depth: int = 0
    permissions: Any = None
    trace: List[Dict[str, Any]] = field(default_factory=list)
    counters: Dict[str, int] = field(default_factory=dict)
    # Optional run observer (default None = un-observed). The Interpreter notifies
    # it on every state enter/leave and completion event, so a debug viewer (or any
    # monitor) can watch a run live without the engine knowing what it is. Shared
    # across ``descend`` (``replace`` copies it) so nested Programs are observed too.
    observer: Any = None
    # Scoped sub-meters (one per named scope), each a child of ``meter``
    # so a scope cap and the global total are enforced by one charge. Shared across
    # a scope's actions: the first BudgetGovernor with a given key creates it.
    budgets: Dict[str, "BudgetMeter"] = field(default_factory=dict)
    # Named events raised by activities, drained by the interpreter and dispatched
    # up the active configuration. Shared across ``descend`` (one list), so a leaf
    # inside a nested Program raises onto the same sink the enclosing interpreters
    # drain at each Program boundary.
    raised: List[RaisedEvent] = field(default_factory=list)

    def phase_meter(self, key: str, cap: int) -> "BudgetMeter":
        m = self.budgets.get(key)
        if m is None:
            m = self.meter.child(cap, label=key)
            self.budgets[key] = m
        return m

    def record(self, action: "Action", result: Result) -> None:
        self.trace.append(
            {"kind": action.kind, "name": action.name, "ok": result.ok, "depth": self.depth}
        )

    def descend(self) -> "Context":
        """A child context one level deeper. Shelves, meter, trace and counters
        are shared (same objects); only ``depth`` increments."""
        return replace(self, depth=self.depth + 1)

    def tick(self, key: str) -> int:
        """Increment and return a named counter (iteration Governors key off
        these). Shared across ``descend`` because ``counters`` is one dict."""
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def raise_event(self, name: str, payload: Any = None) -> None:
        """Raise a named event for the interpreter to dispatch UP the active
        configuration to the nearest superstate with a matching transition (e.g. a
        supervising Loop that retries the failed child). The activity only signals
        the condition; the chart's transition table decides who handles it and how —
        keeping control flow in the transition layer, not in the activity."""
        self.raised.append(RaisedEvent(name=name, payload=payload))


# A runner fulfils an Inference: (spec, payload, ctx) -> value | Result.
InferenceRunner = Callable[[Any, Any, Context], Any]


# TODO(John): add a 'lifetime' to actions in the future — an explicit notion of how
# long an action (and any agent it spawns) lives / when it is (re)evaluated, so the
# mock-vs-live and agent fan-out semantics are first-class rather than implied by kind.
class Action(ABC):
    """Uniform execution atom. Subclasses implement ``_invoke``; ``run`` is the
    shared, final entry point that normalises results, traps exceptions into
    ``Error``, and records the audit line."""

    kind: str = "action"

    def __init__(self, name: str, *, adapter: Any = None) -> None:
        self.name = name
        self.adapter = adapter

    @abstractmethod
    def _invoke(self, payload: Any, ctx: Context) -> Any: ...

    def run(self, payload: Any, ctx: Context) -> Result:
        dbg = _debug_on()
        if dbg:
            print(f"  → {'  ' * int(getattr(ctx, 'depth', 0))}{self.kind}:{self.name}",
                  file=sys.stderr, flush=True)
        t0 = time.monotonic()
        try:
            result = self._invoke(payload, ctx)
            if not isinstance(result, Result):
                result = Output(result)
        except ActionError as exc:
            result = Error(exc, detail=exc.detail or f"{self.kind}:{self.name}")
        except Exception as exc:  # noqa: BLE001 — leaf boundary: failure becomes data
            result = Error(exc, detail=f"{self.kind}:{self.name} raised {type(exc).__name__}")
        ctx.record(self, result)
        if dbg:
            dt = time.monotonic() - t0
            tag = "ok" if result.ok else "FAIL"
            extra = "" if result.ok else f"  {getattr(result, 'detail', '')}"
            print(f"  ← {'  ' * int(getattr(ctx, 'depth', 0))}{self.kind}:{self.name} "
                  f"[{tag} {dt:.1f}s]{extra}", file=sys.stderr, flush=True)
        return result

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<{type(self).__name__} {self.kind}:{self.name}>"


class Procedure(Action):
    """Deterministic leaf. Wraps a callable ``fn(payload, ctx) -> value|Result``.
    Callers wrap deterministic functions as Procedures; the kind is purely
    mechanical — no inference, no side effects beyond what the function itself
    performs."""

    kind = "procedure"

    def __init__(self, name: str, fn: Callable[[Any, Context], Any], *, adapter: Any = None) -> None:
        super().__init__(name, adapter=adapter)
        self._fn = fn

    def _invoke(self, payload: Any, ctx: Context) -> Any:
        return self._fn(payload, ctx)


class Inference(Action):
    """Agent-invocation leaf. The ``runner`` is injected by the factory: the mock
    factory's runner returns a fixture or calls a deterministic oracle; the real
    factory's runner calls ``foundation.models.chat``. The Action is identical either
    way — only the runner differs. If an ``adapter`` is set, a string output is
    decoded into structured data (text is the default channel)."""

    kind = "inference"

    def __init__(self, name: str, spec: Any, runner: InferenceRunner, *, adapter: Any = None) -> None:
        super().__init__(name, adapter=adapter)
        self.spec = spec
        self._runner = runner

    def _invoke(self, payload: Any, ctx: Context) -> Result:
        out = self._runner(self.spec, payload, ctx)
        if not isinstance(out, Result):
            out = Output(out)
        if out.ok and self.adapter is not None and isinstance(out.value, str):
            decoded = self.adapter.decode(out.value)
            out = Output(decoded, meta={**out.meta, "raw": out.value})
        return out


class Program(Action):
    """Composite / recursion point. Wraps a Controller; running it executes the
    controller one level deeper. Bounded to ``MAX_DEPTH`` nesting levels so a
    cyclic or runaway orchestration script cannot recurse without bound."""

    kind = "program"
    MAX_DEPTH = 5

    def __init__(self, name: str, controller: Any, *, adapter: Any = None) -> None:
        super().__init__(name, adapter=adapter)
        self.controller = controller

    def _invoke(self, payload: Any, ctx: Context) -> Result:
        if ctx.depth >= self.MAX_DEPTH:
            raise ProgramError(
                f"program nesting depth {ctx.depth} exceeds max {self.MAX_DEPTH}",
                kind="program",
                name=self.name,
            )
        return self.controller.run(payload, ctx.descend())


class Proxy(Action):
    """I/O-reroute wrapper — the fourth Action kind, for workflow overlays.

    Wraps a target ``Action`` and reroutes shelf entries *around* its run: before
    running the target it copies each ``pre`` ``(src_shelf, src_key) ->
    (dst_shelf, dst_key)``; after, each ``post`` likewise. The target's body and
    its governors are untouched — the proxy only rewires the data edges, so it
    works even when the target self-manages its I/O (a ``raw`` action). Pure
    mechanism: identical under the mock and real factories. The wrapped target's
    ``Result`` is returned verbatim."""

    kind = "proxy"

    # Each pre/post entry is a 4-tuple (src_shelf, src_key, dst_shelf, dst_key).
    def __init__(
        self,
        name: str,
        inner: Action,
        *,
        pre: Any = (),
        post: Any = (),
        adapter: Any = None,
    ) -> None:
        super().__init__(name, adapter=adapter)
        self.inner = inner
        self.pre = tuple(pre)
        self.post = tuple(post)

    @staticmethod
    def _copy(ctx: Context, src_shelf: str, src_key: str, dst_shelf: str, dst_key: str) -> None:
        value = getattr(ctx.shelves, src_shelf).get(src_key)
        getattr(ctx.shelves, dst_shelf).put(dst_key, value)

    def _invoke(self, payload: Any, ctx: Context) -> Result:
        for src_shelf, src_key, dst_shelf, dst_key in self.pre:
            self._copy(ctx, src_shelf, src_key, dst_shelf, dst_key)
        result = self.inner.run(payload, ctx)
        for src_shelf, src_key, dst_shelf, dst_key in self.post:
            self._copy(ctx, src_shelf, src_key, dst_shelf, dst_key)
        return result
