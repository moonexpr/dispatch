#!/usr/bin/env python3
"""bindings/engineer — engineering-execution token + loop predicates.

``engineer:execute_orchestration`` is the recursion seam: a RAW, factory-bound
body that deserializes the orchestration script into a Program and runs it (the
depth operator), returning the program's Result so the monitor loop can read
success/abort. The two loop predicates the monitor uses are registered here too.
"""
from __future__ import annotations

from typing import Any


def build_execute_orchestration(factory: Any):
    """M4 — return the raw body that deserializes the orchestration script (via the
    factory) into a Program and runs it. Self-manages its shelf I/O; returns the
    program's Result."""

    def _execute(payload: Any, ctx: Any) -> Any:
        script = ctx.shelves.deliverables.get("orchestration_script")
        if not script:
            raise RuntimeError("no orchestration_script on deliverables; author_orchestration must run first")
        program = factory.deserialize(script, name="engineering")
        result = program.run(payload, ctx)
        ctx.shelves.deliverables.put(
            "engineering_result", {"ok": result.ok, "value": result.value, "meta": result.meta}
        )
        return result

    return _execute


def engineering_succeeded(result: Any, ctx: Any) -> bool:
    """The monitor's success predicate: the program completed with a truthy value."""
    return result.ok and bool(result.value)


def engineering_failed(result: Any, ctx: Any) -> bool:
    """The monitor's abort predicate: the program errored."""
    return not result.ok


def register(reg: Any) -> None:
    reg.register_action("execute_orchestration", build_execute_orchestration, needs_factory=True)
    reg.register_predicate("engineering_succeeded", engineering_succeeded)
    reg.register_predicate("engineering_failed", engineering_failed)
