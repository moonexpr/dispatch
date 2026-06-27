#!/usr/bin/env python3
"""bindings/github — work-unit sourcing tokens (the github: namespace).

Composes the ``purpose`` subsystem (issue -> purpose classification) into the
``github:generate_work_units`` action body.
"""
from __future__ import annotations

from typing import Any, Dict

import purpose  # src/architect/purpose.py — clean, pure interface


def generate_work_units(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A1 — read the work unit from the issue and classify its purpose."""
    job = inputs.get("job") or {}
    labels = list(job.get("labels", []) or [])
    result = purpose.classify_purpose(job, labels)
    work_unit = {"issue": job.get("issue"), "title": job.get("title", ""), "purpose": result}
    return {"purpose": result, "work_unit": work_unit}


def register(reg: Any) -> None:
    reg.register_action("generate_work_units", generate_work_units)
