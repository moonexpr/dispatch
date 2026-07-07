#!/usr/bin/env python3
"""registry.py — the TokenRegistry: the binding interface between foundation's
workflow *mechanism* and the application's *implementations*.

The workflow compiler (this package) knows how to parse a YAML workflow and its
action interface-files into a statechart, but it knows nothing about what any
given action *does* — that is domain code supplied by callers. The seam between
the two is the ``TokenRegistry``: a caller fills one with its action bodies and
loop predicates (the "bind"), and hands it to the compiler. This keeps the
dependency edge pointing the right way — callers import ``foundation``;
``foundation`` never imports its callers — exactly the leaf discipline the rest of
``foundation`` follows.

Three token namespaces:

  * **actions** — ``bind_name -> callable``. The callable is the action *body*. A
    plain body is ``fn(inputs) -> {out_alias: value}`` (pure; the compiler wires
    its declared interface I/O around it); ``needs_ctx`` bodies receive the run
    ``Context`` as a second arg; ``needs_factory`` bodies are *builders*
    (``fn(factory) -> (payload, ctx) -> value|Result``) that self-manage I/O — the
    recursion/sub-program seam.
  * **predicates** — ``name -> (result, ctx) -> bool``. Referenced from a Loop's
    ``until``/``abort_when`` predicate expression.
  * **controllers** — ``name -> ControllerSpec`` (ADR-003). A controller is the
    code-owned coordination layer between the workflow (which references
    controllers) and the actions (which the controller's ``steps`` reference,
    in the same step grammar a workflow phase uses). The spec is *declarative*
    on purpose: the needs union — "a controller's needs are the union of its
    actions'" — stays computable without executing anything.

Leaf module: stdlib only; no imports from ``foundation.*`` or from callers
(``ControllerSpec.needs`` entries are opaque objects the workflow layer
interprets — this module never touches them).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Set, Tuple


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


@dataclass(frozen=True)
class ControllerSpec:
    """A registered controller: an ordered composition of action tokens (or
    structural mappings — the loader's step grammar), plus any controller-level
    extra ``needs`` beyond the union of its members', and a description for the
    needs report. The workflow layer expands/compiles it; this record only holds
    the declaration."""

    name: str
    steps: Tuple[Any, ...]
    needs: Tuple[Any, ...] = ()
    description: str = ""


class TokenRegistry:
    """Mutable registry of action bindings, predicate functions and controller
    specs. Created and populated by the caller (the bind), then passed into the
    workflow compiler."""

    def __init__(self) -> None:
        self._actions: Dict[str, ActionBinding] = {}
        self._predicates: Dict[str, Callable[[Any, Any], bool]] = {}
        self._controllers: Dict[str, ControllerSpec] = {}

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

    # -- controllers ----------------------------------------------------------
    def register_controller(
        self,
        name: str,
        steps,
        *,
        needs: Any = (),
        description: str = "",
    ) -> ControllerSpec:
        """Register a controller under ``name``: an ordered ``steps`` composition
        of action tokens (loader step grammar) plus optional extra ``needs``."""
        spec = ControllerSpec(
            name=name, steps=tuple(steps), needs=tuple(needs or ()), description=description
        )
        self._controllers[name] = spec
        return spec

    def controller_spec(self, name: str) -> ControllerSpec:
        try:
            return self._controllers[name]
        except KeyError:
            raise UnknownToken("controller", name, self._controllers.keys())

    def has_controller(self, name: str) -> bool:
        return name in self._controllers

    def controller_names(self) -> Set[str]:
        return set(self._controllers)
