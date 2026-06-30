#!/usr/bin/env python3
"""control.py — control-flow builders that COMPILE to a statechart.

Control flow lives in the chart, not in a Python loop inside a controller body.
``Sequence``, ``Loop`` and ``Parallel`` are ergonomic *builders*: you author with
them, but each ``compile``s itself into ``State`` + first-class ``Transition``
objects, and a single ``Interpreter`` runs the result (RTC, async/UML semantics).
That is what satisfies the HFSM requirement "externalize transitions as first-class
objects — NOT control flow inside controller bodies" while keeping authoring clean.

Compilation mapping:
  * ``Sequence`` -> an OR-superstate (COMPOUND): default entry on the first child,
    a ``done`` completion transition to the next child (last -> ``@done``), and an
    ``error`` transition from every child to ``@error``.
  * ``Loop`` -> a LOOP superstate whose body carries a guarded self-transition
    (``done`` + continue-guard -> body), a stop edge (``until`` or iteration cap
    -> ``@done``) and an abort edge (-> ``@error``). The constraint IS the guard.
  * ``Parallel`` -> a PARALLEL state with one region per branch (orthogonal;
    single-region execution stub for now).
  * ``Controller`` -> the OR-superstate root; ``run`` compiles its ``body()`` to a
    ``Statechart`` and hands it to the ``Interpreter``.

Builders are deliberately **not** Action subtypes — leaves (Procedure/Inference/
Program) stay leaves; control flow stays in the chart.

Leaf module: stdlib + sibling foundation leaves.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .action import Action, BudgetMeter, Context
from .interpreter import Interpreter
from .result import Output, Result
from .shelf import Shelves
from .statechart import (
    COMPOUND,
    EV_DONE,
    EV_ERROR,
    LEAF,
    LOOP,
    PARALLEL,
    State,
    Statechart,
    T_DONE,
    T_ERROR,
    Transition,
    make_id,
)

# A step is an Action (leaf) or a Control (compiles to a sub-state).
Step = Any
# Loop predicates read the completing Result and the Context.
Predicate = Callable[[Result, Context], bool]

# Sentinel total for a bare Controller run with no externally supplied budget.
_UNBOUNDED = 10 ** 12


class Control:
    """Base builder. ``compile(path)`` produces the ``State`` for this node;
    ``run`` is a convenience that compiles a one-off chart and interprets it."""

    name: str = "control"

    def compile(self, path: str) -> State:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, payload: Any, ctx: Context) -> Result:
        return Interpreter(Statechart(root=self.compile(self.name)), ctx).run(payload)


def compile_node(node: Step, path: str) -> State:
    """Compile an Action (leaf state) or a Control (sub-chart) at ``path``."""
    if isinstance(node, Control):
        return node.compile(path)
    if isinstance(node, Action):
        return State(id=path, kind=LEAF, activity=node)
    raise TypeError(f"cannot compile {type(node).__name__} into a state")


def _child_path(path: str, step: Step, index: int) -> str:
    return make_id(path, getattr(step, "name", "step"), index)


@dataclass
class Sequence(Control):
    """OR-superstate: children run one at a time, wired by ``done`` completion
    transitions (last -> ``@done``); any child's ``error`` routes to ``@error``.

    ``terminal_when`` is an optional early-completion predicate: when a child
    completes *successfully* and the predicate admits its Result, the whole
    Sequence completes there (-> ``@done``) carrying that Result, instead of
    advancing to the next child. It compiles to a guarded ``done`` edge ranked
    *before* the unguarded ``done -> next`` edge — the same priority-ordered
    guarded-``EV_DONE`` technique a ``Loop`` uses for its stop edge — so a step
    can short-circuit the sequence with a non-error *terminal* result (e.g. a
    pipeline that has already decided its outcome and should not run later
    steps). With ``terminal_when`` None this is the classic run-all sequence."""

    steps: List[Step]
    name: str = "sequence"
    terminal_when: Optional[Predicate] = None

    def compile(self, path: str) -> State:
        children = [compile_node(s, _child_path(path, s, i)) for i, s in enumerate(self.steps)]
        terminal = self.terminal_when

        def terminal_guard(result: Result, ctx: Context) -> bool:
            return bool(terminal(result, ctx)) if terminal is not None else False

        transitions: List[Transition] = []
        for i, st in enumerate(children):
            transitions.append(
                Transition(source=st.id, event=EV_ERROR, target=T_ERROR, guard_name="error")
            )
            # Ranked first among this child's ``done`` edges: an enabled terminal
            # guard completes the sequence here. Unset (None) -> guard is always
            # False, so the classic ``done -> next`` edge below always wins.
            if terminal is not None:
                transitions.append(
                    Transition(
                        source=st.id, event=EV_DONE, target=T_DONE,
                        guard=terminal_guard, guard_name="terminal",
                    )
                )
            nxt = children[i + 1].id if i + 1 < len(children) else T_DONE
            transitions.append(
                Transition(source=st.id, event=EV_DONE, target=nxt, guard_name="done")
            )
        return State(
            id=path,
            kind=COMPOUND,
            children=children,
            initial=children[0].id if children else "",
            transitions=transitions,
        )


@dataclass
class Parallel(Control):
    """Orthogonal regions (AND-decomposition). Single-region execution stub: the
    interpreter runs regions sequentially and joins on all-success; the structure
    is recorded as parallel so concurrency drops in at the interpreter later."""

    branches: List[Step]
    name: str = "parallel"

    def compile(self, path: str) -> State:
        regions = [compile_node(b, _child_path(path, b, i)) for i, b in enumerate(self.branches)]
        return State(id=path, kind=PARALLEL, children=regions)


@dataclass
class Loop(Control):
    """A constrained loop = a state with a guarded self-transition. ``until`` is
    the success predicate (stop -> ``@done``); ``abort_when`` is the failure
    predicate (stop -> ``@error``); ``max_iterations`` is the hard cap. The
    interpreter publishes the live iteration count on ``ctx.counters[loop_id]`` so
    the guards can read it. With no ``until`` the body runs once."""

    body: Step
    until: Optional[Predicate] = None
    abort_when: Optional[Predicate] = None
    max_iterations: int = 1
    name: str = "loop"

    def compile(self, path: str) -> State:
        loop_id = path
        body_state = compile_node(self.body, _child_path(path, self.body, 0))
        bid = body_state.id
        until = self.until
        abort = self.abort_when
        max_it = max(1, self.max_iterations)

        def abort_guard(result: Result, ctx: Context) -> bool:
            return bool(abort(result, ctx)) if abort is not None else False

        def stop_guard(result: Result, ctx: Context) -> bool:
            if ctx.counters.get(loop_id, 0) >= max_it:
                return True
            return bool(until(result, ctx)) if until is not None else True

        # Order matters: the interpreter takes the first enabled transition.
        transitions = [
            Transition(source=bid, event=EV_DONE, target=T_ERROR, guard=abort_guard, guard_name="abort_when"),
            Transition(source=bid, event=EV_DONE, target=T_DONE, guard=stop_guard, guard_name="until|cap"),
            Transition(source=bid, event=EV_DONE, target=bid, guard_name="continue"),
            Transition(source=bid, event=EV_ERROR, target=T_ERROR, guard_name="error"),
        ]
        return State(id=loop_id, kind=LOOP, children=[body_state], initial=bid, transitions=transitions)


class Controller(ABC):
    """The OR-superstate root. Constructed with an injected ``AbstractActionFactory``
    and three Shelves; subclasses supply ``body()``. ``run`` compiles ``body()`` to
    a ``Statechart`` and drives it with one ``Interpreter``. Wrap a Controller in a
    ``Program`` (``factory.program``) to nest it — the only recursion point."""

    name: str = "controller"

    def __init__(self, factory: Any, shelves: Optional[Shelves] = None) -> None:
        self.factory = factory
        self.shelves = shelves if shelves is not None else factory.shelves()
        self.last_interpreter: Optional[Interpreter] = None

    @abstractmethod
    def body(self) -> Control: ...

    def compile(self) -> Statechart:
        return Statechart(root=compile_node(self.body(), self.name))

    def context(
        self,
        *,
        meter: Optional[BudgetMeter] = None,
        dry_run: bool = True,
        permissions: Any = None,
        log: Any = None,
    ) -> Context:
        kw: dict = {
            "shelves": self.shelves,
            "meter": meter or BudgetMeter(_UNBOUNDED, label="unbounded"),
            "dry_run": dry_run,
            "permissions": permissions,
        }
        if log is not None:
            kw["log"] = log
        return Context(**kw)

    def run(self, payload: Any = None, ctx: Optional[Context] = None) -> Result:
        if ctx is None:
            ctx = self.context()
        interp = Interpreter(self.compile(), ctx)
        self.last_interpreter = interp
        return interp.run(payload)


def ordinal(names: "list[str] | tuple[str, ...]", name: str) -> Optional[int]:
    """1-based position of ``name`` in ``names``, or ``None``. Use this
    instead of hard-coding ordinals in callers."""
    try:
        return list(names).index(name) + 1
    except ValueError:
        return None
