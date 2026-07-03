#!/usr/bin/env python3
"""controller.py — the generic, YAML-driven Controller.

``YamlController`` is the N-phase Controller whose phase list, name and per-phase
Control bodies come from a compiled workflow rather than from hand-coded abstract
methods. It is a ``foundation.actions.Controller`` whose body is a ``Sequence`` of
the phase Sequences named ``"lifecycle"``, compiled at the controller's own name as
the root path — so the state ids it mints follow the pattern
``<name>/<i>.<phase>``. The optional ``until`` halt-gate trims phases by ordinal
(derived from the phase list, never hard-coded), the same discipline the stage
walk uses.

Leaf module: stdlib + ``foundation.actions``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from foundation.actions import (
    Context,
    Control,
    Controller,
    Interpreter,
    Result,
    Sequence,
    Statechart,
    compile_node,
    ordinal,
)


@dataclass
class CompiledWorkflow:
    """The product of compiling a ``WorkflowNode``: the controller name, the
    ordered phase names, the compiled per-phase Control bodies, and an optional
    compiled ``terminal_when`` predicate (the lifecycle early-completion guard;
    the per-phase Sequences carry their own copy, set by the CompileVisitor)."""

    name: str
    phase_names: List[str]
    phase_controls: List[Control] = field(default_factory=list)
    terminal_when: Optional[Any] = None


class YamlController(Controller):
    """A Controller whose phases are supplied (already compiled) rather than
    declared by subclass methods. ``name`` and ``PHASES`` come from the workflow."""

    def __init__(
        self,
        factory: Any,
        *,
        name: str,
        phase_names,
        phase_controls,
        shelves: Any = None,
        terminal_when: Optional[Any] = None,
    ) -> None:
        super().__init__(factory, shelves=shelves)
        self.name = name
        self.PHASES = tuple(phase_names)
        self._phase_controls: List[Control] = list(phase_controls)
        # Lifecycle-level early-completion guard (None -> classic run-all). The
        # per-phase Sequences already carry the same guard; this one short-circuits
        # *between* phases so a terminal step in phase 1 skips phases 2..N.
        self._terminal_when = terminal_when

    # -- phase assembly -----------------------------------------------------
    def _phase_steps(self) -> "list[Control]":
        return list(self._phase_controls)

    def body(self) -> Control:
        return Sequence(self._phase_steps(), name="lifecycle", terminal_when=self._terminal_when)

    def phase_ord(self, name: str) -> Optional[int]:
        """1-based ordinal of a phase in this workflow's phase list, or None."""
        return ordinal(self.PHASES, name)

    # -- run with an optional halt-gate -------------------------------------
    def compile(self, *, until: str = "") -> Statechart:
        steps = self._phase_steps()
        o = self.phase_ord(until) if until else None
        if o:
            steps = steps[:o]
        return Statechart(root=compile_node(
            Sequence(steps, name="lifecycle", terminal_when=self._terminal_when), self.name))

    def run(self, payload: Any = None, ctx: Optional[Context] = None, *, until: str = "") -> Result:
        if ctx is None:
            ctx = self.context()
        interp = Interpreter(self.compile(until=until), ctx)
        self.last_interpreter = interp
        return interp.run(payload)
