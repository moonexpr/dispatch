#!/usr/bin/env python3
"""registry.py — the TokenRegistry: the binding interface between the engine's
workflow *mechanism* and the application's *implementations*.

The workflow compiler (this package) knows how to parse a YAML workflow and its
action interface-files into a statechart, but it knows nothing about what any
given action *does* — that is domain code, which lives in ``src/``. The seam
between the two is the ``TokenRegistry``: ``src`` fills one with its action bodies
and loop predicates (the "bind"), and hands it to the compiler. This keeps the
dependency edge pointing the right way — ``src`` imports ``engine``; ``engine``
never imports ``src`` — exactly the leaf discipline the rest of ``engine`` follows.

Two token namespaces:

  * **actions** — ``bind_name -> callable``. The callable is the action *body*. A
    plain body is ``fn(inputs) -> {out_alias: value}`` (pure; the compiler wires
    its declared interface I/O around it); ``needs_ctx`` bodies receive the run
    ``Context`` as a second arg; ``needs_factory`` bodies are *builders*
    (``fn(factory) -> (payload, ctx) -> value|Result``) that self-manage I/O — the
    recursion/sub-program seam.
  * **predicates** — ``name -> (result, ctx) -> bool``. Referenced from a Loop's
    ``until``/``abort_when`` predicate expression.

Leaf module: stdlib only; no imports from ``engine.*`` or ``src.*``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Set


class TokenError(Exception):
    """Base class for token-resolution failures."""


class UnknownToken(TokenError):
    """A token (action bind or predicate) was requested but never registered."""

    def __init__(self, kind: str, name: str, available) -> None:
        self.kind = kind
        self.name = name
        self.available = sorted(available)
        known = ", ".join(self.available) or "(none)"
        super().__init__(f"unknown {kind} token {name!r}; registered: {known}")


@dataclass(frozen=True)
class ActionBinding:
    """A registered action body plus how the compiler must call it.

    ``needs_ctx`` — the body takes ``(inputs, ctx)`` instead of ``(inputs)``.
    ``needs_factory`` — the body is a *builder* ``fn(factory) -> body`` and the
    built body self-manages its shelf I/O (it may return a ``Result``); the
    compiler does not wire the manifest interface around it.
    """

    fn: Callable[..., Any]
    needs_ctx: bool = False
    needs_factory: bool = False


class TokenRegistry:
    """Mutable registry of action bindings and predicate functions. Created and
    populated by ``src`` (the bind), then passed into the workflow compiler."""

    def __init__(self) -> None:
        self._actions: Dict[str, ActionBinding] = {}
        self._predicates: Dict[str, Callable[[Any, Any], bool]] = {}

    # -- actions ------------------------------------------------------------
    def register_action(
        self,
        bind: str,
        fn: Callable[..., Any],
        *,
        needs_ctx: bool = False,
        needs_factory: bool = False,
    ) -> Callable[..., Any]:
        """Bind an action body under ``bind``. Returns ``fn`` so it doubles as a
        decorator target."""
        self._actions[bind] = ActionBinding(fn, needs_ctx=needs_ctx, needs_factory=needs_factory)
        return fn

    def action(self, bind: str, *, needs_ctx: bool = False, needs_factory: bool = False):
        """Decorator form of :meth:`register_action`."""

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            return self.register_action(bind, fn, needs_ctx=needs_ctx, needs_factory=needs_factory)

        return deco

    def action_binding(self, bind: str) -> ActionBinding:
        try:
            return self._actions[bind]
        except KeyError:
            raise UnknownToken("action", bind, self._actions.keys())

    def has_action(self, bind: str) -> bool:
        return bind in self._actions

    def action_binds(self) -> Set[str]:
        return set(self._actions)

    # -- predicates ---------------------------------------------------------
    def register_predicate(self, name: str, fn: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
        """Bind a loop predicate ``(result, ctx) -> bool`` under ``name``."""
        self._predicates[name] = fn
        return fn

    def predicate(self, name: str):
        """Decorator form of :meth:`register_predicate`."""

        def deco(fn: Callable[[Any, Any], bool]) -> Callable[[Any, Any], bool]:
            return self.register_predicate(name, fn)

        return deco

    def predicate_fn(self, name: str) -> Callable[[Any, Any], bool]:
        try:
            return self._predicates[name]
        except KeyError:
            raise UnknownToken("predicate", name, self._predicates.keys())

    def has_predicate(self, name: str) -> bool:
        return name in self._predicates

    def predicate_names(self) -> Set[str]:
        return set(self._predicates)
