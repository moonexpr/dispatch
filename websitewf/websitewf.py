#!/usr/bin/env python3
"""websitewf.py — the WebsiteWF engine.

A sibling of ``baseworkflow`` that *inherits everything baseworkflow builds*:
:class:`WebsiteWF` subclasses :class:`~baseworkflow.BaseWorkflow` and overrides only
the three engine hooks — the parsed workflow doc (the ``websitewf.yml`` overlay), the
token bind layer (``websitewf.bindings``, base binds + ``web:*``), and the budget
cap. The phases, agents, governors, shelves and run loop are all inherited.

Public surface mirrors ``baseworkflow`` (``WebsiteWF``, ``run_mock``, ``run_live``),
so the orchestration bridge can select it by name with ``DISPATCH_ENGINE=websitewf``.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from foundation.workflow import load_workflow  # noqa: E402
from baseworkflow.baseworkflow import BaseWorkflow  # noqa: E402

# The default overlay (extends: baseworkflow) — app/workflows/websitewf.yml, the
# proof vertical. Other use-case overlays are selected by WEBSITEWF_USECASE (below).
WORKFLOW_PATH = "workflows/websitewf.yml"

# --- use-case registry: slug -> (overlay workflow path, "module:fn" registry builder) ---
# Default (no WEBSITEWF_USECASE / unknown value) = the proof vertical, unchanged.
_USECASES = {
    None:                  ("workflows/websitewf.yml",                     "websitewf.bindings:build_registry"),
    "scaffold-foundation": ("workflows/websitewf-scaffold-foundation.yml", "websitewf.usecases.scaffold_foundation:build_registry"),
    "backend-feature":     ("workflows/websitewf-backend-feature.yml",     "websitewf.usecases.backend_feature:build_registry"),
    "adopt-integration":   ("workflows/websitewf-adopt-integration.yml",   "websitewf.usecases.adopt_integration:build_registry"),
    "ux-loop-issues":      ("workflows/websitewf-ux-loop-issues.yml",      "websitewf.usecases.ux_loop_issues:build_registry"),
    "feedback-ticket":     ("workflows/websitewf-feedback-ticket.yml",     "websitewf.usecases.feedback_ticket:build_registry"),
}


def _selected_usecase() -> Any:
    """Resolve WEBSITEWF_USECASE -> a known slug, or None (proof vertical) for unset/unknown."""
    slug = os.environ.get("WEBSITEWF_USECASE")
    return slug if slug in _USECASES else None


def _resolve_registry(spec: str) -> Any:
    import importlib

    mod_name, fn_name = spec.split(":", 1)
    return getattr(importlib.import_module(mod_name), fn_name)()


class WebsiteWF(BaseWorkflow):
    """BaseWorkflow specialized for web development by a websitewf overlay.

    The active overlay + registry are selected per instance by WEBSITEWF_USECASE;
    unset/unknown falls back to the proof vertical (fully backward-compatible)."""

    WORKFLOW_PATH = WORKFLOW_PATH

    def _usecase(self) -> Any:
        return _USECASES[_selected_usecase()]

    def _load_doc(self) -> Any:
        path, _ = self._usecase()
        return load_workflow(path)

    def _build_registry(self) -> Any:
        _, reg_spec = self._usecase()
        return _resolve_registry(reg_spec)

    def _total_budget(self) -> int:
        return int(self._load_doc().budgets["total"])


def _run(
    factory: Any,
    job: Dict[str, Any],
    triage: Dict[str, Any],
    *,
    dry_run: bool,
    request: Any = None,
    services: Any = None,
) -> Dict[str, Any]:
    wf = WebsiteWF(factory, job=job, triage=triage, request=request, services=services)
    ctx = wf.context(dry_run=dry_run)
    result = wf.run(ctx=ctx)
    return {
        "result": result,
        "ctx": ctx,
        "workflow": wf,
        "deliverables": wf.shelves.deliverables.snapshot(),
        "interpreter": wf.last_interpreter,
    }


def run_mock(
    job: Dict[str, Any],
    triage: Dict[str, Any],
    *,
    dry_run: bool = True,
    request: Any = None,
    services: Any = None,
) -> Dict[str, Any]:
    """Run WebsiteWF against the MockActionFactory (no real effects) — the e2e spine."""
    from foundation.actions import MockActionFactory

    return _run(MockActionFactory(), job, triage, dry_run=dry_run, request=request, services=services)


def run_live(
    job: Dict[str, Any],
    triage: Dict[str, Any],
    *,
    dry_run: bool = True,
    request: Any = None,
    services: Any = None,
) -> Dict[str, Any]:
    """Run WebsiteWF against the ArchitectFactory (same summary shape as run_mock).
    Under ``dry_run=True`` the inference runner emits a deterministic placeholder
    instead of calling a model."""
    import shutil
    import tempfile

    # ArchitectFactory is a RealActionFactory whose only override is the live
    # architect:draft_work_plan inference — it computes the deterministic baseline
    # work plan and FAILS SAFE to it on any model/parse error (e.g. an empty SDK
    # response), instead of letting the generic inference runner raise on a json.loads
    # of empty output. WebsiteWF inherits the same single inference, so it needs the
    # same fail-safe factory baseworkflow.run_live uses (not the plain
    # RealActionFactory, which has no architect fallback).
    from baseworkflow.bindings.architect import ArchitectFactory

    # Per-run isolated shelf root (see baseworkflow.run_live): a fresh temp root per
    # tick, torn down after, so concurrent / sequential ticks can't read each other's
    # stale shelf state (shelves are within-run only — CLAUDE.md).
    shelf_root = tempfile.mkdtemp(prefix="dispatch-shelves-")
    try:
        return _run(
            ArchitectFactory(shelf_root=shelf_root), job, triage,
            dry_run=dry_run, request=request, services=services,
        )
    finally:
        shutil.rmtree(shelf_root, ignore_errors=True)
