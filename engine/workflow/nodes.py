#!/usr/bin/env python3
"""nodes.py — the workflow node tree: the Elements of the visitor pattern.

A parsed workflow is a tree of immutable nodes. Each node implements
``accept(visitor)`` (the GoF double-dispatch hook), so an *operation* over the
tree is a visitor class with one ``visit_<node>`` method per node type — adding an
operation (compile, validate, render, cost-estimate) never touches the node
classes, and adding a node type is one method per visitor. The node hierarchy is
the structural-token vocabulary the loader parses YAML into; the visitors
(``visitor.py``) turn it into an ``engine.actions`` statechart, validate it, or
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
    named ``budgets``, and the ordered ``phases``."""

    name: str
    phases: Tuple[PhaseNode, ...]
    inputs: Tuple[str, ...] = ()
    seed: Dict[str, Any] = field(default_factory=dict)
    budgets: Dict[str, int] = field(default_factory=dict)
    admin_spec_split: float = 0.5

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_workflow(self)
