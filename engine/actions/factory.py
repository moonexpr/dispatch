#!/usr/bin/env python3
"""factory.py — the Abstract Factory and its Mock/Real concretes.

This is the inversion-of-control hinge. ``AbstractActionFactory`` produces the
whole family of backend-themed objects — actions, shelves, adapters, governors —
so that "mock" and "real" are consistent across the entire family: you never get
a mock shelf with a real governor. Control-flow code receives a factory and asks
it for parts; it never names a concrete class, so the same Controller body runs
in the test (``MockActionFactory``) and in production (``RealActionFactory``) with
zero changes.

What actually differs between the two:

  * ``shelf``      — MemoryShelf (in-memory, no side effects) vs. FileShelf (durable).
  * inference runner — a deterministic oracle/fixture (no model, no network) vs.
                       ``engine.models.chat`` (a real query()).
  * ``governor``   — permissive (mock) vs. enforcing/deny-by-default (real).

``adapter`` is shared verbatim (format coercion is backend-agnostic) and
``deserialize`` is shared concrete (both factories rebuild an orchestration script
the same way; only the injected runner differs), which is exactly why one script
runs mock or real unchanged.

Leaf module: stdlib + sibling engine leaves (engine.models is imported lazily,
only on the real path, so the mock path never touches a backend).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from .action import Action, Inference, Procedure, Program
from .adapter import Adapter, adapter_for
from .control import Controller, Parallel, Sequence
from .governor import BudgetGovernor, Governor, IterationGovernor, PermissionGovernor
from .result import Output
from .script import InferenceSpec, OrchestrationScript
from .shelf import FileShelf, MemoryShelf, Shelf, Shelves

# Default route -> alias map for the real backend, kept local so engine/ never
# imports src/tuning (which would be a forbidden engine->src edge). The workflow
# may pass explicit aliases instead of gen-* routes.
_ROUTE_ALIAS = {"gen-local": "local", "gen-default": "sonnet", "gen-frontier": "opus"}


class AbstractActionFactory(ABC):
    """Produces the full family of action-layer objects. Subclasses fix the
    backend theme (mock vs. real); the methods below are the family."""

    enforce_governors: bool = True

    # -- the action family (core requirement) -------------------------------
    def procedure(self, name: str, fn: Any, *, adapter: Optional[Adapter] = None) -> Action:
        return Procedure(name, fn, adapter=adapter)

    def inference(self, name: str, spec: InferenceSpec, *, adapter: Optional[Adapter] = None) -> Action:
        if adapter is None and getattr(spec, "fmt", "text") in ("json", "yaml"):
            adapter = self.adapter(spec.fmt)
        return Inference(name, spec, self._inference_runner, adapter=adapter)

    def program(self, controller: Controller, *, name: str = "program") -> Action:
        return Program(name, controller)

    # -- supporting members of the same theme -------------------------------
    @abstractmethod
    def shelf(self, kind: str) -> Shelf: ...

    def adapter(self, fmt: str) -> Adapter:
        # Shared verbatim: format coercion is identical mock or real.
        return adapter_for(fmt)

    @abstractmethod
    def governor(self, kind: str, inner: Action, **cfg: Any) -> Governor: ...

    # -- the inference runner: the one method that makes mock != real -------
    @abstractmethod
    def _inference_runner(self, spec: InferenceSpec, payload: Any, ctx: Any) -> Any: ...

    # -- composite helpers (shared) -----------------------------------------
    def shelves(self) -> Shelves:
        return Shelves(self.shelf("input"), self.shelf("deliverables"), self.shelf("shared"))

    # -- deserialize an orchestration script into a Program (shared) --------
    def deserialize(self, script: Any, *, name: str = "engineering") -> Action:
        """Rebuild a serialized Program. Accepts an ``OrchestrationScript``, a
        plain dict, or a JSON string (whatever was stored on a Shelf). Sequential
        phases chain; ``parallel: true`` runs fan out under one parent; each phase
        is wrapped in a permission Governor (the script's allowlist) and a budget
        Governor (the phase's share of ENGINEERING_BUDGET)."""
        script = _coerce_script(script)
        return self.program(_ScriptController(self, script), name=name)

    def _build_script_body(self, script: OrchestrationScript) -> Sequence:
        steps: list = []
        parallel_buffer: list = []

        def flush() -> None:
            if parallel_buffer:
                steps.append(Parallel(list(parallel_buffer), name="orchestration.parallel"))
                parallel_buffer.clear()

        # The engineering phases share one ENGINEERING_BUDGET sub-meter (the script
        # carries the bucket; fall back to the sum of per-unit shares); each phase is
        # also pre-flight-checked against its own share via ``estimate``.
        eng_total = getattr(script, "budget", 0) or sum(ph.budget for ph in script.phases) or 0
        for ph in script.phases:
            spec = InferenceSpec(
                prompt=ph.agent.prompt,
                system=ph.agent.description,
                model=ph.agent.model,
                tools=ph.agent.tools,
                max_tokens=max(256, ph.budget // 8) if ph.budget else 1024,
                mock_usage=min(ph.budget // 4 if ph.budget else 1000, 2000),
            )
            act: Action = self.inference(f"phase:{ph.id}", spec)
            # deny-by-default permission gate (the script's allowlist).
            act = self.governor("permission", act, allow=script.allow_tools)
            # shared engineering budget meter; per-unit share is the pre-flight estimate.
            if eng_total:
                act = self.governor(
                    "budget", act, cap=eng_total, estimate=spec.mock_usage,
                    phase="engineering", label=f"phase:{ph.id}",
                )
            if ph.parallel:
                parallel_buffer.append(act)
            else:
                flush()
                steps.append(act)
        flush()
        return Sequence(steps, name="orchestration")


class _ScriptController(Controller):
    """A Controller materialised from an orchestration script. Wrapped in a
    Program by ``deserialize``; runs with the parent's Context (shelves, meter)
    because the Program passes ``ctx.descend()`` rather than a fresh context."""

    def __init__(self, factory: AbstractActionFactory, script: OrchestrationScript) -> None:
        super().__init__(factory)
        self._script = script

    def body(self) -> Sequence:
        return self.factory._build_script_body(self._script)


def _coerce_script(script: Any) -> OrchestrationScript:
    if isinstance(script, OrchestrationScript):
        return script
    if isinstance(script, str):
        return OrchestrationScript.from_json(script)
    if isinstance(script, dict):
        return OrchestrationScript.from_dict(script)
    raise TypeError(f"cannot deserialize orchestration script of type {type(script).__name__}")


class MockActionFactory(AbstractActionFactory):
    """In-memory, side-effect-free family. Inference is fulfilled by a
    deterministic oracle or a canned fixture (no model, no network); governors
    charge/observe but never abort or deny. This is what the end-to-end test runs
    against — a full BaseWorkflow with zero real effects."""

    enforce_governors = False

    def shelf(self, kind: str) -> Shelf:
        return MemoryShelf(kind)

    def governor(self, kind: str, inner: Action, **cfg: Any) -> Governor:
        if kind == "budget":
            return BudgetGovernor(
                inner, cap=cfg["cap"], estimate=cfg.get("estimate", 0),
                label=cfg.get("label", ""), phase=cfg.get("phase", ""), enforce=False,
            )
        if kind == "permission":
            # permissive: allow everything (still constructed so the family is whole).
            return PermissionGovernor(inner, allow={PermissionGovernor.WILDCARD}, enforce=False)
        if kind == "iteration":
            return IterationGovernor(inner, cap=cfg["cap"], key=cfg.get("key", ""), enforce=False)
        raise ValueError(f"unknown governor kind {kind!r}")

    def _inference_runner(self, spec: InferenceSpec, payload: Any, ctx: Any) -> Output:
        if spec.oracle is not None:
            value = spec.oracle(payload, ctx)
        elif spec.fixture is not None:
            value = spec.fixture
        else:
            value = f"[mock:{spec.model}] {spec.prompt[:80]}".strip()
        return Output(value, meta={"usage": int(spec.mock_usage), "model": spec.model, "source": "mock"})


class RealActionFactory(AbstractActionFactory):
    """Durable, enforcing family. Inference calls ``engine.models.chat`` (a real
    query()); governors enforce budgets and deny-by-default permissions; shelves
    persist through ``engine.filesys``. Under ``ctx.dry_run`` the inference runner
    emits a deterministic placeholder instead of calling a model — the same
    dry-run discipline the rest of the pipeline follows."""

    enforce_governors = True

    def __init__(self, *, shelf_root: str = "actions/shelves") -> None:
        self._shelf_root = shelf_root

    def shelf(self, kind: str) -> Shelf:
        return FileShelf(kind, root=self._shelf_root)

    def governor(self, kind: str, inner: Action, **cfg: Any) -> Governor:
        if kind == "budget":
            return BudgetGovernor(
                inner, cap=cfg["cap"], estimate=cfg.get("estimate", 0),
                label=cfg.get("label", ""), phase=cfg.get("phase", ""), enforce=True,
            )
        if kind == "permission":
            return PermissionGovernor(
                inner, allow=cfg.get("allow", ()), required=cfg.get("required"), enforce=True,
            )
        if kind == "iteration":
            return IterationGovernor(inner, cap=cfg["cap"], key=cfg.get("key", ""), enforce=True)
        raise ValueError(f"unknown governor kind {kind!r}")

    def _inference_runner(self, spec: InferenceSpec, payload: Any, ctx: Any) -> Output:
        if getattr(ctx, "dry_run", True):
            return Output(
                f"[dry-run:{spec.model}] {spec.prompt[:80]}".strip(),
                meta={"usage": 0, "model": spec.model, "source": "dry-run"},
            )
        from engine import models

        alias = _ROUTE_ALIAS.get(spec.model, spec.model)
        text = models.chat(alias, spec.messages(payload), max_tokens=spec.max_tokens)
        return Output(text, meta={"model": spec.model, "source": "real"})
