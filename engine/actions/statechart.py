#!/usr/bin/env python3
"""statechart.py — the serializable Harel statechart: States, first-class
Transitions, and the chart that holds them.

The execution model is a hierarchical state machine, not a host call stack. This
module is the *data*: every state has a stable, addressable id; every transition
is a first-class object (source, event, guard, target, action) rather than a
branch buried in a controller body; the whole chart serializes to an
SCXML/UML-2-aligned dict so the machine is inspectable and resumable.

Construct mapping (from the HFSM addendum):
  * ``COMPOUND`` — an OR-superstate (Controller / Sequence): exactly one child
    active, default entry + completion transitions.
  * ``LEAF``     — a Procedure/Inference/Program activity; runs on entry, emits a
    Result on completion (the Result is the trigger that selects the next edge).
  * ``LOOP``     — a state with a guarded self-transition (the loop constraint is
    the guard).
  * ``PARALLEL`` — orthogonal regions, co-active (single-region stub for now).
  * ``FINAL``    — an absorbing state the completion/error edges target.
  * history (H*) — a durability seam: a compound records its deepest active
    child as it runs, and ``Interpreter.restore``/``resume`` re-enter from that
    recorded configuration so a controller that died mid-run resumes its nested
    active position instead of restarting.

What is deliberately a *seam, not a feature* yet: the broadcast event bus,
``in(state)`` guards across sibling regions, and multi-region peer switching. The
fields exist (``Transition.event`` is a free string; ``guard`` can inspect the
config) but no second region is driven yet.

Leaf module: stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# State kinds.
LEAF = "leaf"
COMPOUND = "compound"
LOOP = "loop"
PARALLEL = "parallel"
FINAL = "final"

# Completion-event names a leaf raises (the Result decides which).
EV_DONE = "done"
EV_ERROR = "error"

# Special transition targets meaning "this superstate completes".
T_DONE = "@done"
T_ERROR = "@error"

# A guard reads the completing child's Result and the Context; returns True if the
# transition is enabled. Defaults to True (an unguarded completion edge). A
# deny-by-default guard returns False until its condition is met.
Guard = Callable[[Any, Any], bool]

# A transition action runs as the edge is taken (e.g. write to a Shelf). It
# receives the completing Result and the Context.
TransitionAction = Callable[[Any, Any], None]


@dataclass
class Transition:
    """A first-class transition. ``source`` and ``target`` are state ids; ``event``
    is the trigger (``done``/``error`` for completion edges, or a named event once
    the broadcast bus exists); ``guard`` gates it; ``action`` fires as it is taken.
    ``target`` may be the sentinel ``@done``/``@error`` meaning the enclosing
    superstate completes with that outcome."""

    source: str
    event: str
    target: str
    guard: Optional[Guard] = None
    guard_name: str = ""
    action: Optional[TransitionAction] = None
    id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "event": self.event,
            "target": self.target,
            "guard": self.guard_name or ("<guard>" if self.guard else ""),
        }


@dataclass
class State:
    """A node in the chart. ``id`` is stable and addressable. The fields used
    depend on ``kind``:
      * LEAF — ``activity`` (an Action) is run on entry.
      * COMPOUND — ``children`` + ``initial`` + ``transitions`` (among children).
      * LOOP — ``children[0]`` is the body; ``transitions`` carry the guarded
        self-transition and the exit edges.
      * PARALLEL — ``children`` are orthogonal regions.
      * FINAL — absorbing.
    ``history`` records the last deep configuration (resume seam)."""

    id: str
    kind: str
    activity: Any = None  # Action, for LEAF states
    children: List["State"] = field(default_factory=list)
    initial: str = ""
    transitions: List[Transition] = field(default_factory=list)
    history: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def child(self, sid: str) -> Optional["State"]:
        for c in self.children:
            if c.id == sid:
                return c
        return None

    def transitions_from(self, source: str, event: str) -> List[Transition]:
        return [t for t in self.transitions if t.source == source and t.event == event]

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.id, "kind": self.kind}
        if self.kind == LEAF and self.activity is not None:
            act = self.activity
            entry: Dict[str, Any] = {"kind": getattr(act, "kind", "action"), "name": getattr(act, "name", "")}
            spec = getattr(act, "spec", None)
            if spec is not None:
                # Serialize an Inference's spec shape (without callables).
                entry["spec"] = {
                    "model": getattr(spec, "model", ""),
                    "tools": list(getattr(spec, "tools", ()) or ()),
                    "fmt": getattr(spec, "fmt", "text"),
                }
            d["activity"] = entry
        if self.initial:
            d["initial"] = self.initial
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        if self.transitions:
            d["transitions"] = [t.to_dict() for t in self.transitions]
        if self.history is not None:
            d["history"] = self.history
        return d


@dataclass
class Configuration:
    """The active configuration: the path of active state ids from root to the
    current leaf (single region for now; a set once regions are orthogonal).
    Serializable — this is what a future resume reads back."""

    active: List[str] = field(default_factory=list)

    def enter(self, sid: str) -> None:
        self.active.append(sid)

    def leave(self, sid: str) -> None:
        if self.active and self.active[-1] == sid:
            self.active.pop()

    def to_dict(self) -> Dict[str, Any]:
        return {"active": list(self.active)}


@dataclass
class Statechart:
    """A chart: a root state plus a flat id->State index (built on construction)
    for O(1) addressing. ``to_dict`` emits the SCXML-aligned structure used to
    serialize an orchestration script as a statechart fragment."""

    root: State
    index: Dict[str, State] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.index:
            self.index = {}
            self._reindex(self.root)

    def _reindex(self, st: State) -> None:
        self.index[st.id] = st
        for c in st.children:
            self._reindex(c)

    def state(self, sid: str) -> State:
        return self.index[sid]

    def to_dict(self) -> Dict[str, Any]:
        return {"root": self.root.to_dict()}


# -- id minting -------------------------------------------------------------
def make_id(parent_path: str, name: str, index: Optional[int] = None) -> str:
    """Deterministic, path-based, addressable id. No randomness or wall-clock, so
    ids are stable across runs (a hard requirement for resume and addressing)."""
    seg = name.replace(" ", "_").replace("/", "_")
    if index is not None:
        seg = f"{index}.{seg}"
    return f"{parent_path}/{seg}" if parent_path else seg
