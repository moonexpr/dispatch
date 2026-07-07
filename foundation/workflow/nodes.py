#!/usr/bin/env python3
"""nodes.py — the workflow node tree: the Elements of the visitor pattern.

A parsed workflow is a tree of immutable nodes. Each node implements
``accept(visitor)`` (the GoF double-dispatch hook), so an *operation* over the
tree is a visitor class with one ``visit_<node>`` method per node type — adding an
operation (compile, validate, render, cost-estimate) never touches the node
classes, and adding a node type is one method per visitor. The node hierarchy is
the structural-token vocabulary the loader parses YAML into; the visitors
(``visitor.py``) turn it into a ``foundation.actions`` statechart, validate it, or
serialize it back.

Leaf module: stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .manifest import ActionManifest


class Node:
    """Base Element. Subclasses dispatch to the matching ``visit_*`` method."""

    def accept(self, visitor: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True)
class ActionRefNode(Node):
    """A leaf: a reference to one action token plus its resolved interface rule."""

    token: str
    manifest: ActionManifest

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_action_ref(self)


@dataclass(frozen=True)
class ControllerRefNode(Node):
    """A reference to a registered controller (ADR-003): the workflow names the
    party; the controller's registered ``ControllerSpec`` supplies the action
    composition. Resolution is deferred to the visitors (the registry is not
    available at load time — the same discipline as action binds), so the node
    carries the loaded ``manifests`` its expansion will resolve tokens against."""

    name: str
    manifests: Dict[str, ActionManifest] = field(default_factory=dict)

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_controller_ref(self)


@dataclass(frozen=True)
class SequenceNode(Node):
    """Children run one at a time, in order (compiles to an OR-superstate)."""

    name: str
    steps: Tuple[Node, ...]

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_sequence(self)


@dataclass(frozen=True)
class ParallelNode(Node):
    """Orthogonal branches (compiles to a PARALLEL state)."""

    name: str
    branches: Tuple[Node, ...]

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_parallel(self)


@dataclass(frozen=True)
class LoopNode(Node):
    """A constrained loop. ``until``/``abort_when``/``while`` are predicate
    *expressions* (compiled by the predicate visitor); ``max_iterations`` is the
    hard cap. Advanced loop shapes compose named predicates in those expressions."""

    name: str
    body: Node
    until: Optional[str] = None
    abort_when: Optional[str] = None
    while_: Optional[str] = None
    max_iterations: int = 1

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_loop(self)


@dataclass(frozen=True)
class PhaseNode(Node):
    """A named phase — a Sequence tagged for the controller's ordinal/halt-gate."""

    name: str
    steps: Tuple[Node, ...]

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_phase(self)


@dataclass(frozen=True)
class WorkflowNode(Node):
    """The root: name, the declared runtime ``inputs``, the static ``seed``, the
    named ``budgets``, the ordered ``phases``, and the declared ``controllers``
    — every party to the contract, including controllers only entered
    dynamically (supersede targets), so the workflow's aggregate needs are
    computable even where the phase graph is not the whole story (ADR-003)."""

    name: str
    phases: Tuple[PhaseNode, ...]
    inputs: Tuple[str, ...] = ()
    seed: Dict[str, Any] = field(default_factory=dict)
    budgets: Dict[str, int] = field(default_factory=dict)
    controllers: Tuple[str, ...] = ()
    # The workflow's resolved interface library (token -> manifest), carried so
    # declared-only controllers (dynamic supersede targets, never in a phase)
    # can still be expanded, needs-checked and materialized.
    manifests: Dict[str, Any] = field(default_factory=dict)
    admin_spec_split: float = 0.5
    # Optional declarative early-completion: a registered predicate name. When a
    # step completes *successfully* and this predicate admits its Result, the whole
    # lifecycle completes there (the Sequence ``terminal_when`` primitive). Lets a
    # YAML workflow express conditional early-exit without a control-flow node.
    terminal_when: Optional[str] = None

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_workflow(self)
