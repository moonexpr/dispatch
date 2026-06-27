#!/usr/bin/env python3
"""result.py — the uniform Action result: ``Result = Output | Error``.

Every Action returns a ``Result``. Control-flow code (Sequence, Loop, Controller,
Governor) branches ONLY on ``result.ok`` — never on the concrete Action subtype
and never on which ``Result`` variant came back. That is the inversion-of-control
contract: mock and real backends are interchangeable because the shape they hand
back is identical.

The variant is modelled on ``engine.proc.Completed`` (``.ok`` + ``.check()``) so it
reads natively next to the rest of the engine. ``Output`` carries a ``value`` plus
free-form ``meta`` (usage counts, model id, source); ``Error`` carries an ``error``
(an exception or a message) plus ``detail``. Neither is ever raised by normal
flow — failures travel *as data* through the same channel as successes.

Leaf module: stdlib only. Safe for anything under ``engine/`` to import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict


class ActionError(RuntimeError):
    """Base for Action-layer failures. Mirrors engine.proc.ProcError: a
    RuntimeError subclass that carries structured context for the caller."""

    def __init__(self, message: str, *, kind: str = "", name: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.name = name
        self.detail = detail


class InferenceError(ActionError):
    """An Inference (agent) action could not produce an output."""


class ProcedureError(ActionError):
    """A Procedure (deterministic leaf) raised while executing."""


class ProgramError(ActionError):
    """A Program (sub-controller) failed to run — e.g. nesting depth exceeded."""


class PermissionDenied(ActionError):
    """A permission Governor denied an action (deny-by-default)."""


class BudgetExceeded(ActionError):
    """A budget Governor aborted because a cap would be breached."""


class Result:
    """Base class for the two variants. Concrete results are ``Output`` and
    ``Error``; this base only provides the variant-agnostic helpers callers use
    so they never have to ``isinstance``-check."""

    ok: ClassVar[bool] = False

    def value_or(self, default: Any = None) -> Any:
        """The output value on success, else ``default``."""
        return getattr(self, "value", default) if self.ok else default

    def unwrap(self) -> Any:
        """Return the value, or raise the carried error. Use only at a boundary
        where you have decided a failure should become an exception — control
        flow inside a Controller passes ``Result`` around without unwrapping."""
        if self.ok:
            return self.value  # type: ignore[attr-defined]
        err = getattr(self, "error", None)
        if isinstance(err, BaseException):
            raise err
        raise ActionError(str(err) or "action failed", detail=getattr(self, "detail", ""))

    def map(self, fn: Callable[[Any], Any]) -> "Result":
        """Transform the value on success; pass an ``Error`` through untouched."""
        if not self.ok:
            return self
        return Output(fn(self.value), meta=dict(getattr(self, "meta", {})))  # type: ignore[attr-defined]


@dataclass(frozen=True)
class Output(Result):
    """A successful result. ``value`` is plain text by default; structured
    payloads (dict/list) arrive here when an Adapter decoded them upstream.
    ``meta`` carries side-channel data — token ``usage``, ``model``, ``source``."""

    ok: ClassVar[bool] = True
    value: Any = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Error(Result):
    """A failed result. ``error`` is an exception instance or a message string;
    ``detail`` is a human-readable note; ``meta`` mirrors ``Output.meta`` so a
    partial run can still report what it spent before failing."""

    ok: ClassVar[bool] = False
    error: Any = None
    detail: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


def ok(value: Any = None, **meta: Any) -> Output:
    """Terse constructor: ``return ok(plan, usage=120)``."""
    return Output(value, meta=meta)


def err(error: Any, *, detail: str = "", **meta: Any) -> Error:
    """Terse constructor: ``return err(exc, detail="A6 failed")``."""
    return Error(error, detail=detail, meta=meta)
