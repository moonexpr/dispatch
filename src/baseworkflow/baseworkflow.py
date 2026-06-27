#!/usr/bin/env python3
"""baseworkflow.py — the thin loader for the YAML-driven work-unit lifecycle.

The workflow's technical details — which actions run in spec/work/build, their
kind, governors, budgets, and the monitor loop — now live in
``app/config/baseworkflow.yml`` plus one interface file per action under
``app/config/actions/``. This module is the small adapter that:

  * loads + compiles that YAML into an ``engine.workflow.YamlController`` (the
    statechart), using the token bindings in ``bindings`` (the bind layer), and
  * preserves the historical public surface — ``BaseWorkflow``, ``run_mock``, and
    the ``*_BUDGET`` constants — so existing callers and the end-to-end test are
    unchanged.

The budget constants are read FROM the YAML (single source of truth). Building a
``BaseWorkflow`` is side-effect-free (it constructs a statechart and compiles it);
the input shelf is seeded at run time, not construction.

Architecture (ADR-001 — Harel statecharts / HFSM). A ``BaseWorkflow`` is a
**Controller** (an OR-superstate) whose ``spec → work → build`` phases compile to
a serializable statechart driven by the engine's run-to-completion interpreter
over an active configuration. The YAML names the **Actions** — ``Procedure``
(deterministic leaf), ``Inference`` (agent leaf), ``Program`` (nested Controller,
the sole recursion point) — wired through ``bindings``; **Governor** decorators
carry the budget / permission / iteration caps; the three **Shelves**
(``input`` / ``deliverables`` / ``shared``) are the data model. See
``docs/adr/001-hfsm-automata.md``.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SRC)
for _p in (_ROOT, _SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine.actions import BudgetMeter, Context  # noqa: E402
from engine.workflow import (  # noqa: E402
    CompileVisitor,
    WorkflowValidationError,
    load_workflow,
    validate,
)
from engine.workflow.controller import YamlController  # noqa: E402

import bindings  # noqa: E402  (src/baseworkflow/bindings package)

# The workflow definition, parsed once. Path is app-dir-relative (resolved via
# engine.filesys): app/config/baseworkflow.yml.
WORKFLOW_PATH = "config/baseworkflow.yml"
_DOC = load_workflow(WORKFLOW_PATH)

# -- budget constants, read FROM the YAML (single source of truth) ----------
_BUDGETS = _DOC.budgets
BUDGET_UNIT = "tokens"
TOTAL_BUDGET = int(_BUDGETS["total"])
ARCHITECT_BUDGET = int(_BUDGETS["architect"])
ENGINEERING_BUDGET = int(_BUDGETS["engineering"])
ADMIN_BUDGET = int(_BUDGETS["admin"])


class BaseWorkflow(YamlController):
    """One work-unit cycle across spec/work/build, compiled from the YAML. The
    constructor signature is unchanged; ``job``/``triage`` are seeded onto the
    input shelf at run time."""

    def __init__(
        self,
        factory: Any,
        *,
        job: Dict[str, Any],
        triage: Dict[str, Any],
        admin_spec_split: float = 0.5,
        shelves: Any = None,
    ) -> None:
        registry = bindings.build_registry()
        errors = validate(_DOC, registry)
        if errors:
            raise WorkflowValidationError(errors)
        compiled = CompileVisitor(factory, registry).visit(_DOC)
        super().__init__(
            factory,
            name=compiled.name,
            phase_names=compiled.phase_names,
            phase_controls=compiled.phase_controls,
            shelves=shelves,
        )
        self.job = dict(job)
        self.triage = dict(triage)
        self.admin_spec_split = float(admin_spec_split)
        self._seed_static = dict(_DOC.seed or {})

    # -- budget helpers (API-compat) ----------------------------------------
    @property
    def admin_spec_budget(self) -> int:
        return int(ADMIN_BUDGET * self.admin_spec_split)

    @property
    def admin_build_budget(self) -> int:
        return ADMIN_BUDGET - self.admin_spec_budget

    # -- run-time shelf seeding ---------------------------------------------
    def _seed(self, shelves: Any) -> None:
        shelves.input.put("job", self.job)
        shelves.input.put("triage", self.triage)
        for key, value in self._seed_static.items():
            shelves.input.put(key, value)

    def context(self, **kw: Any) -> Context:
        kw.setdefault("meter", BudgetMeter(TOTAL_BUDGET, label="workflow"))
        return super().context(**kw)

    def run(self, payload: Any = None, ctx: Optional[Context] = None, *, until: str = ""):
        if ctx is None:
            ctx = self.context()
        self._seed(ctx.shelves)
        return super().run(payload, ctx=ctx, until=until)


def run_mock(job: Dict[str, Any], triage: Dict[str, Any], *, dry_run: bool = True) -> Dict[str, Any]:
    """Run a BaseWorkflow against the MockActionFactory and return a summary — the
    spine of the end-to-end test. No real side effects."""
    from engine.actions import MockActionFactory

    factory = MockActionFactory()
    wf = BaseWorkflow(factory, job=job, triage=triage)
    ctx = wf.context(dry_run=dry_run)
    result = wf.run(ctx=ctx)
    return {
        "result": result,
        "ctx": ctx,
        "workflow": wf,
        "deliverables": wf.shelves.deliverables.snapshot(),
        "interpreter": wf.last_interpreter,
    }
