#!/usr/bin/env python3
"""src.baseworkflow — a concrete three-phase BaseController plus the subsystem
catalog that makes each existing dispatch subsystem a composable Action.

``BaseWorkflow`` fills spec/work/build with the Architect sub-actions (A1-A8),
the Engineering Program, and the Administrator's env/doc/store steps. ``catalog``
adapter-wraps ``src/architect/*`` and ``src/budget/*`` as Action bodies without
editing those modules — they become independent, composable Actions reachable
through the factory.
"""
from __future__ import annotations

from .baseworkflow import (
    ADMIN_BUDGET,
    ARCHITECT_BUDGET,
    BUDGET_UNIT,
    ENGINEERING_BUDGET,
    TOTAL_BUDGET,
    BaseWorkflow,
    run_mock,
)

__all__ = [
    "BaseWorkflow",
    "run_mock",
    "BUDGET_UNIT",
    "TOTAL_BUDGET",
    "ARCHITECT_BUDGET",
    "ENGINEERING_BUDGET",
    "ADMIN_BUDGET",
]
