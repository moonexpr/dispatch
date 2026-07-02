#!/usr/bin/env python3
"""baseworkflow.py — the thin loader for the YAML-driven work-unit lifecycle.

The workflow's technical details — which actions run in spec/work/build, their
kind, governors, budgets, and the monitor loop — now live in
``app/workflows/baseworkflow.yml`` plus one interface file per action under
``app/config/actions/``. This module is the small adapter that:

  * loads + compiles that YAML into an ``foundation.workflow.YamlController`` (the
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
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from foundation.actions import BudgetMeter, Context  # noqa: E402
from foundation.workflow import (  # noqa: E402
    CompileVisitor,
    WorkflowValidationError,
    load_workflow,
    validate,
)
from foundation.workflow.controller import YamlController  # noqa: E402

import bindings  # noqa: E402  (baseworkflow/bindings package)

# The workflow definition, parsed once. Path is app-dir-relative (resolved via
# foundation.filesys): app/workflows/baseworkflow.yml.
WORKFLOW_PATH = "workflows/baseworkflow.yml"
_DOC = load_workflow(WORKFLOW_PATH)

# -- budget constants, read FROM the YAML (single source of truth) ----------
_BUDGETS = _DOC.budgets
BUDGET_UNIT = "tokens"
TOTAL_BUDGET = int(_BUDGETS["total"])
ARCHITECT_BUDGET = int(_BUDGETS["architect"])
ENGINEERING_BUDGET = int(_BUDGETS["engineering"])
ADMIN_BUDGET = int(_BUDGETS["admin"])


def _default_request(job: Dict[str, Any]) -> Dict[str, Any]:
    """The legacy-compat request: callers that hand in a pre-fetched ``job`` get
    a non-interactive job-source request synthesized from it, so the seed
    controller accepts and hands off without any service dependency."""
    title = job.get("title") or f"issue #{job.get('issue')}"
    return {
        "source": "job",
        "job": {"title": title, "goal": job.get("body") or title, "acceptance": ""},
    }


class BaseWorkflow(YamlController):
    """One work-unit cycle across seed/spec/work/build, compiled from the YAML.
    ``job``/``triage`` — and ``request``, the seed controller's raw intake
    (ADR-003; synthesized from ``job`` when not given, so existing callers are
    unchanged) — are seeded onto the input shelf at run time. ``services`` (a
    ``ServiceRegistry``) is both the validation-time provision source and the
    run ``Context``'s resolution surface.

    Subclassable into a sibling *engine* (e.g. ``WebsiteWF``): override
    :meth:`_load_doc` (the parsed workflow — e.g. an overlay), :meth:`_build_registry`
    (the token bind layer) and :meth:`_total_budget` (the workflow meter cap), and
    inherit everything else BaseWorkflow builds. The base implementations return the
    module-level baseworkflow doc / registry / budget, so base behaviour is
    unchanged."""

    WORKFLOW_PATH = WORKFLOW_PATH  # class-visible; overridden by subclasses

    # -- engine hooks (override in a sibling engine) ------------------------
    def _load_doc(self) -> Any:
        """The parsed ``WorkflowNode`` to compile. Base: the module-level doc."""
        return _DOC

    def _build_registry(self) -> Any:
        """The token bind layer. Base: baseworkflow's ``bindings.build_registry``."""
        return bindings.build_registry()

    def _total_budget(self) -> int:
        """The workflow meter cap. Base: ``TOTAL_BUDGET`` from the YAML."""
        return TOTAL_BUDGET

    def __init__(
        self,
        factory: Any,
        *,
        job: Dict[str, Any],
        triage: Dict[str, Any],
        admin_spec_split: float = 0.5,
        shelves: Any = None,
        request: Optional[Dict[str, Any]] = None,
        services: Any = None,
    ) -> None:
        doc = self._load_doc()
        registry = self._build_registry()
        # Structure/data-flow/token validation only: CONTRACT satisfaction (all
        # declared controllers' needs met) is the CLI validator's strict check
        # (`--services`); at run time the seed applies per-REQUEST satisfaction,
        # so a host providing only some services still takes the requests it can.
        errors = validate(doc, registry)
        if errors:
            raise WorkflowValidationError(errors)
        compiled = CompileVisitor(factory, registry).visit(doc)
        super().__init__(
            factory,
            name=compiled.name,
            phase_names=compiled.phase_names,
            phase_controls=compiled.phase_controls,
            shelves=shelves,
            terminal_when=compiled.terminal_when,
        )
        self.job = dict(job)
        self.triage = dict(triage)
        self.request = dict(request) if request is not None else _default_request(self.job)
        self.admin_spec_split = float(admin_spec_split)
        self._seed_static = dict(doc.seed or {})
        self._services = services

    # -- budget helpers (API-compat) ----------------------------------------
    @property
    def admin_spec_budget(self) -> int:
        return int(ADMIN_BUDGET * self.admin_spec_split)

    @property
    def admin_build_budget(self) -> int:
        return ADMIN_BUDGET - self.admin_spec_budget

    # -- run-time shelf seeding ---------------------------------------------
    def _seed(self, shelves: Any) -> None:
        shelves.input.put("request", self.request)
        shelves.input.put("job", self.job)
        shelves.input.put("triage", self.triage)
        for key, value in self._seed_static.items():
            shelves.input.put(key, value)

    def context(self, **kw: Any) -> Context:
        kw.setdefault("meter", BudgetMeter(self._total_budget(), label="workflow"))
        if self._services is not None:
            kw.setdefault("services", self._services)
        return super().context(**kw)

    def run(self, payload: Any = None, ctx: Optional[Context] = None, *, until: str = ""):
        if ctx is None:
            ctx = self.context()
        self._seed(ctx.shelves)
        return super().run(payload, ctx=ctx, until=until)


def run_mock(
    job: Dict[str, Any],
    triage: Dict[str, Any],
    *,
    dry_run: bool = True,
    request: Optional[Dict[str, Any]] = None,
    services: Any = None,
) -> Dict[str, Any]:
    """Run a BaseWorkflow against the MockActionFactory and return a summary — the
    spine of the end-to-end test. No real side effects: with no explicit
    ``services`` the offline service family (canned issues, scripted operator)
    provides the seed's provisions."""
    from foundation.actions import MockActionFactory

    import services as bw_services  # baseworkflow/services.py (path-bootstrapped)

    factory = MockActionFactory()
    svc = services if services is not None else bw_services.build_mock_services()
    wf = BaseWorkflow(factory, job=job, triage=triage, request=request, services=svc)
    ctx = wf.context(dry_run=dry_run)
    result = wf.run(ctx=ctx)
    return {
        "result": result,
        "ctx": ctx,
        "workflow": wf,
        "deliverables": wf.shelves.deliverables.snapshot(),
        "interpreter": wf.last_interpreter,
    }


def run_live(
    job: Dict[str, Any],
    triage: Dict[str, Any],
    *,
    dry_run: bool = True,
    request: Optional[Dict[str, Any]] = None,
    services: Any = None,
) -> Dict[str, Any]:
    """Run a BaseWorkflow against the ``RealActionFactory`` and return the same
    summary shape as :func:`run_mock`. The real service family (gh-backed
    github.access, TTY-backed operator.interactive) provides the seed's
    provisions unless the caller injects its own.

    This is the foundational *live* runner (Phase 1 of wiring BaseWorkflow onto
    the live tick). It is identical to :func:`run_mock` except for the injected
    factory: ``RealActionFactory`` carries the enforcing governors, the durable
    ``FileShelf`` family, and the ``foundation.models.chat`` inference runner.

    Under ``dry_run=True`` (the default) the real inference runner returns a
    deterministic placeholder instead of calling a model, so this runner stays
    side-effect-free with respect to the network/model. NOTE: ``RealActionFactory``
    uses ``FileShelf`` (durable on-disk shelves), so it is not as hermetic as the
    in-memory mock path — callers wanting zero disk writes should pass an explicit
    shelf-backed configuration. No orchestration-tick wiring is done here; that is
    Phase 2+.
    """
    import shutil
    import tempfile

    # ArchitectFactory is a RealActionFactory whose only override is the live
    # inference runner for architect:draft_work_plan (the architect SDK agent); under
    # dry_run it defers to the deterministic oracle, so that path is unchanged.
    from bindings.architect import ArchitectFactory

    # Per-run isolated shelf root. The FileShelf family is durable WITHIN a run, but
    # shelves are within-run state only (CLAUDE.md) — a fixed shared on-disk root let
    # sequential / concurrent ticks read each other's stale `job`/deliverables (a tick
    # once read another issue's job and mutated the wrong issue). A fresh temp root per
    # run, torn down in the finally below, gives each tick clean isolation.
    shelf_root = tempfile.mkdtemp(prefix="dispatch-shelves-")
    import services as bw_services  # baseworkflow/services.py (path-bootstrapped)

    factory = ArchitectFactory(shelf_root=shelf_root)
    svc = services if services is not None else bw_services.build_services()
    wf = BaseWorkflow(factory, job=job, triage=triage, request=request, services=svc)
    ctx = wf.context(dry_run=dry_run)
    try:
        result = wf.run(ctx=ctx)
        return {
            "result": result,
            "ctx": ctx,
            "workflow": wf,
            "deliverables": wf.shelves.deliverables.snapshot(),
            "interpreter": wf.last_interpreter,
        }
    finally:
        shutil.rmtree(shelf_root, ignore_errors=True)
