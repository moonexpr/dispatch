#!/usr/bin/env python3
"""foundation.workflow — a YAML-driven workflow compiler (mechanism only).

This package turns a *declarative* workflow — a lean workflow YAML that composes
namespaced action tokens, plus one interface-rule file per action — into a
``foundation.actions`` statechart, using the visitor pattern throughout:

  * ``registry``   — ``TokenRegistry``: the binding interface higher layers fill
    with action bodies + loop predicates (the seam; ``foundation`` never imports
    its callers).
  * ``manifest``   — the per-action interface file (kind, bind, governors, I/O).
  * ``nodes``      — the workflow node tree (the visitor Elements).
  * ``predicate``  — the loop-predicate expression grammar (visitor-parsed).
  * ``visitor``    — ``CompileVisitor`` / ``ValidateVisitor`` / ``RenderVisitor``.
  * ``loader``     — the token parser (YAML -> node tree, ``#base`` includes).
  * ``controller`` — ``YamlController``, the generic N-phase Controller.

End users configure YAML and never touch Python; extenders add a token by
registering a body and dropping an interface file. Validation is stdlib-only (no
``jsonschema`` runtime dependency), so this package stays a leaf: stdlib + PyYAML
(lazy) + sibling ``foundation`` modules.

Top-level entry points: ``load_workflow``, ``validate``, ``compile_workflow``.
"""
from __future__ import annotations

from typing import Any, List, Optional

from .controller import CompiledWorkflow, YamlController
from .graph import GraphVisitor, workflow_to_graph
from .loader import SchemaError, load_workflow, parse_document
from .manifest import (
    ActionManifest,
    IORef,
    ManifestError,
    load_manifests,
    manifest_from_dict,
)
from .nodes import (
    ActionRefNode,
    ControllerRefNode,
    LoopNode,
    Node,
    ParallelNode,
    PhaseNode,
    SequenceNode,
    WorkflowNode,
)
from .predicate import (
    PredicateCompiler,
    PredicateError,
    PredicateValidator,
    compile_predicate,
    parse_predicate,
)
from .registry import ActionBinding, ControllerSpec, TokenError, TokenRegistry, UnknownToken
from .visitor import (
    CompileVisitor,
    RenderVisitor,
    ValidateVisitor,
    ValidationError,
    WorkflowVisitor,
    compile_controller,
    controller_needs,
    expand_controller,
    needs_report,
)

# The needs formalism (ADR-003) re-exported for consumers of this package.
from foundation.needs import Need, NeedSet, Provision  # noqa: E402  (leaf sibling)

__all__ = [
    # registry / binding interface
    "TokenRegistry", "ActionBinding", "ControllerSpec", "TokenError", "UnknownToken",
    # manifest
    "ActionManifest", "IORef", "ManifestError", "load_manifests", "manifest_from_dict",
    # nodes
    "Node", "WorkflowNode", "PhaseNode", "SequenceNode", "ParallelNode", "LoopNode",
    "ActionRefNode", "ControllerRefNode",
    # predicate
    "parse_predicate", "compile_predicate", "PredicateError", "PredicateCompiler", "PredicateValidator",
    # visitors
    "WorkflowVisitor", "CompileVisitor", "ValidateVisitor", "RenderVisitor", "ValidationError",
    # controllers + needs (ADR-003)
    "compile_controller", "controller_needs", "expand_controller", "needs_report",
    "Need", "NeedSet", "Provision",
    # graph / debug viewer (#197)
    "GraphVisitor", "workflow_to_graph",
    # loader
    "load_workflow", "parse_document", "SchemaError",
    # controller
    "YamlController", "CompiledWorkflow",
    # top-level
    "validate", "compile_workflow", "WorkflowValidationError",
]


class WorkflowValidationError(Exception):
    """Raised by :func:`compile_workflow` when validation finds any error."""

    def __init__(self, errors: List[ValidationError]) -> None:
        self.errors = list(errors)
        body = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(f"workflow validation failed ({len(self.errors)} error(s)):\n{body}")


def validate(
    node: WorkflowNode,
    registry: Optional[TokenRegistry] = None,
    *,
    services: Any = None,
) -> List[ValidationError]:
    """Validate a parsed workflow. With ``registry``, also checks that every
    action bind, loop predicate and controller reference is registered (and
    expands controllers for the data-flow check); with ``services`` (a
    ``ServiceRegistry``), additionally checks needs satisfaction — every
    non-optional ``service:``/``config:`` need met by the environment's
    provisions (ADR-003). Without them, structure + data-flow only."""
    v = ValidateVisitor(registry, node.budgets, node.inputs, node.seed, services=services)
    return v.visit(node)


def compile_workflow(src: Any, *, registry: TokenRegistry, factory: Any, shelves: Any = None) -> YamlController:
    """Validate then compile a workflow (a ``WorkflowNode`` or a path) into a
    ready-to-run :class:`YamlController`. Raises :class:`WorkflowValidationError`
    if validation finds any error."""
    node = src if isinstance(src, WorkflowNode) else load_workflow(src)
    errors = validate(node, registry)
    if errors:
        raise WorkflowValidationError(errors)
    compiled = CompileVisitor(factory, registry).visit(node)
    return YamlController(
        factory,
        name=compiled.name,
        phase_names=compiled.phase_names,
        phase_controls=compiled.phase_controls,
        shelves=shelves,
        terminal_when=compiled.terminal_when,
    )
