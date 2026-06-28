#!/usr/bin/env python3
"""websitewf.py — the WebsiteWF engine.

A sibling of ``src/baseworkflow`` that *inherits everything baseworkflow builds*:
:class:`WebsiteWF` subclasses :class:`~baseworkflow.BaseWorkflow` and overrides only
the three engine hooks — the parsed workflow doc (the ``websitewf.yml`` overlay), the
token bind layer (``src.websitewf.bindings``, base binds + ``web:*``), and the budget
cap. The phases, agents, governors, shelves and run loop are all inherited.

Public surface mirrors ``baseworkflow`` (``WebsiteWF``, ``run_mock``, ``run_live``),
so the orchestration bridge can select it by name with ``DISPATCH_ENGINE=websitewf``.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SRC)
for _p in (_ROOT, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine.workflow import load_workflow  # noqa: E402
from src.baseworkflow.baseworkflow import BaseWorkflow  # noqa: E402
from src.websitewf import bindings  # noqa: E402

# The overlay (extends: baseworkflow), parsed once — app/workflows/websitewf.yml.
WORKFLOW_PATH = "workflows/websitewf.yml"
_DOC = load_workflow(WORKFLOW_PATH)
TOTAL_BUDGET = int(_DOC.budgets["total"])


class WebsiteWF(BaseWorkflow):
    """BaseWorkflow specialized for web development by the websitewf overlay."""

    WORKFLOW_PATH = WORKFLOW_PATH

    def _load_doc(self) -> Any:
        return _DOC

    def _build_registry(self) -> Any:
        return bindings.build_registry()

    def _total_budget(self) -> int:
        return TOTAL_BUDGET


def _run(factory: Any, job: Dict[str, Any], triage: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    wf = WebsiteWF(factory, job=job, triage=triage)
    ctx = wf.context(dry_run=dry_run)
    result = wf.run(ctx=ctx)
    return {
        "result": result,
        "ctx": ctx,
        "workflow": wf,
        "deliverables": wf.shelves.deliverables.snapshot(),
        "interpreter": wf.last_interpreter,
    }


def run_mock(job: Dict[str, Any], triage: Dict[str, Any], *, dry_run: bool = True) -> Dict[str, Any]:
    """Run WebsiteWF against the MockActionFactory (no real effects) — the e2e spine."""
    from engine.actions import MockActionFactory

    return _run(MockActionFactory(), job, triage, dry_run=dry_run)


def run_live(job: Dict[str, Any], triage: Dict[str, Any], *, dry_run: bool = True) -> Dict[str, Any]:
    """Run WebsiteWF against the RealActionFactory (same summary shape as run_mock).
    Under ``dry_run=True`` the inference runner emits a deterministic placeholder
    instead of calling a model."""
    from engine.actions import RealActionFactory

    return _run(RealActionFactory(), job, triage, dry_run=dry_run)
