#!/usr/bin/env python3
"""engine.actions — a uniform Action/Controller/Factory substrate.

Every unit of work is an ``Action`` with one contract: ``run(payload, ctx) ->
Result`` where ``Result = Output | Error``. Control flow (``Sequence``, ``Loop``,
``Parallel``, ``Controller``) branches only on ``result.ok``; it never inspects an
Action's concrete subtype. Whether an Action runs against a mock or a real
backend is decided solely by which ``AbstractActionFactory`` is injected — so the
identical control-flow code serves the end-to-end test and production.

Subtypes:  Procedure (deterministic leaf) · Inference (agent leaf) · Program
(wraps a Controller — the only recursion point, bounded to 5 levels).
Cross-cutting concerns are Decorators (``Governor``): budget, permission
(deny-by-default), and iteration caps — never inlined into action logic.

This package is a leaf under ``engine/``: it imports only stdlib and sibling
engine leaves (proc, runtime, filesys, models), never ``src/*``. Domain wiring
(which subsystem fills which Action) lives in ``src/`` — see
``src/baseworkflow/catalog.py``.
"""
from __future__ import annotations

from .action import Action, BudgetMeter, Context, Inference, Procedure, Program
from .adapter import Adapter, JsonAdapter, TextAdapter, YamlAdapter, adapter_for
from .control import Control, Controller, Loop, Parallel, Sequence, compile_node, ordinal
from .factory import AbstractActionFactory, MockActionFactory, RealActionFactory
from .interpreter import Event, Interpreter, Observer, interpret
from .statechart import (
    Configuration,
    State,
    Statechart,
    Transition,
    make_id,
)
from .governor import (
    BudgetGovernor,
    Governor,
    IterationGovernor,
    PermissionGovernor,
)
from .result import (
    ActionError,
    BudgetExceeded,
    Error,
    InferenceError,
    Output,
    PermissionDenied,
    ProcedureError,
    ProgramError,
    Result,
    err,
    ok,
)
from .script import AgentSpec, InferenceSpec, OrchestrationScript, PhaseSpec
from .shelf import FileShelf, MemoryShelf, Shelf, Shelves

__all__ = [
    # results
    "Result", "Output", "Error", "ok", "err",
    "ActionError", "InferenceError", "ProcedureError", "ProgramError",
    "PermissionDenied", "BudgetExceeded",
    # actions
    "Action", "Procedure", "Inference", "Program", "Context", "BudgetMeter",
    # shelves
    "Shelf", "MemoryShelf", "FileShelf", "Shelves",
    # adapters
    "Adapter", "TextAdapter", "JsonAdapter", "YamlAdapter", "adapter_for",
    # governors
    "Governor", "BudgetGovernor", "PermissionGovernor", "IterationGovernor",
    # control
    "Control", "Sequence", "Loop", "Parallel", "Controller", "compile_node", "ordinal",
    # statechart + interpreter
    "State", "Transition", "Statechart", "Configuration", "make_id",
    "Interpreter", "Event", "Observer", "interpret",
    # script
    "OrchestrationScript", "PhaseSpec", "AgentSpec", "InferenceSpec",
    # factory
    "AbstractActionFactory", "MockActionFactory", "RealActionFactory",
]
