#!/usr/bin/env python3
"""controller.py — the generic, YAML-driven Controller.

``YamlController`` is the N-phase generalisation of the old hand-coded
``src/basecontroller``: its phase list, name and per-phase Control bodies come
from a compiled workflow rather than from three abstract methods. It is an
``engine.actions.Controller`` whose body is a ``Sequence`` of the phase Sequences
named ``"lifecycle"``, compiled at the controller's own name as the root path — so
the state ids it mints are identical to the hand-coded skeleton's
(``<name>/<i>.<phase>``). The optional ``until`` halt-gate trims phases by ordinal
(derived from the phase list, never hard-coded), the same discipline the stage
walk uses.

Leaf module: stdlib + ``engine.actions``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from engine.actions import (
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
    ordered phase names, and the compiled per-phase Control bodies."""

    name: str
    phase_names: List[str]
    phase_controls: List[Control] = field(default_factory=list)


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
    ) -> None:
        super().__init__(factory, shelves=shelves)
        self.name = name
        self.PHASES = tuple(phase_names)
        self._phase_controls: List[Control] = list(phase_controls)

    # -- phase assembly -----------------------------------------------------
    def _phase_steps(self) -> "list[Control]":
        return list(self._phase_controls)

    def body(self) -> Control:
        return Sequence(self._phase_steps(), name="lifecycle")

    def phase_ord(self, name: str) -> Optional[int]:
        """1-based ordinal of a phase in this workflow's phase list, or None."""
        return ordinal(self.PHASES, name)

    # -- run with an optional halt-gate -------------------------------------
    def compile(self, *, until: str = "") -> Statechart:
        steps = self._phase_steps()
        o = self.phase_ord(until) if until else None
        if o:
            steps = steps[:o]
        return Statechart(root=compile_node(Sequence(steps, name="lifecycle"), self.name))

    def run(self, payload: Any = None, ctx: Optional[Context] = None, *, until: str = "") -> Result:
        if ctx is None:
            ctx = self.context()
        interp = Interpreter(self.compile(until=until), ctx)
        self.last_interpreter = interp
        return interp.run(payload)
