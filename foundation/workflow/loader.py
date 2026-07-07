#!/usr/bin/env python3
"""loader.py — the token parser: YAML document -> workflow node tree.

Reads a workflow YAML, expands its ``uses:`` includes into the per-action
interface manifests (``#base`` semantics — globs resolve relative to the workflow
file's own directory), and parses each phase's step list into the structural node
tree. Each step is either a bare ``ns:name`` token (an ``ActionRefNode``, resolved
against the loaded manifests) or a single-key mapping naming a structural token
(``loop`` / ``sequence`` / ``parallel``). Shape problems raise ``SchemaError`` with
a located path so the "format test" points at the exact spot.

The workflow path resolves through ``foundation.filesys`` (app-dir rooted, with an
absolute / existing-path escape hatch).

Leaf module: stdlib + in-function PyYAML + ``foundation.filesys`` + sibling leaves.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from foundation import filesys

from .manifest import ActionManifest, load_manifests
from .schema import parse_schemas
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

_STRUCTURAL = ("loop", "sequence", "parallel", "controller")


class SchemaError(Exception):
    """A structural problem in the workflow YAML (the parse-time format error)."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def _resolve_path(relpath: str) -> str:
    """Absolute path for the workflow file: an existing cwd-relative/absolute path
    is used as-is; otherwise it is resolved app-dir-rooted via ``filesys``."""
    if os.path.isabs(relpath):
        return relpath
    if os.path.exists(relpath):
        return os.path.abspath(relpath)
    return filesys.resolve(relpath)


def _parse_step(step: Any, manifests: Dict[str, ActionManifest], path: str) -> Node:
    if isinstance(step, str):
        m = manifests.get(step)
        if m is None:
            raise SchemaError(path, f"unknown action token {step!r}; known: {sorted(manifests)}")
        return ActionRefNode(token=step, manifest=m)
    if isinstance(step, dict):
        if len(step) != 1:
            raise SchemaError(path, f"a step maps exactly one structural token, got keys {sorted(step)}")
        (kind, spec), = step.items()
        if kind == "loop":
            return _parse_loop(spec, manifests, f"{path}.loop")
        if kind == "sequence":
            return _parse_sequence(spec, manifests, f"{path}.sequence")
        if kind == "parallel":
            return _parse_parallel(spec, manifests, f"{path}.parallel")
        if kind == "controller":
            return _parse_controller(spec, manifests, f"{path}.controller")
        raise SchemaError(path, f"unknown structural token {kind!r} (one of {_STRUCTURAL} or an action token)")
    raise SchemaError(path, f"a step is a token string or a mapping, got {type(step).__name__}")


def _parse_controller(spec: Any, manifests: Dict[str, ActionManifest], path: str) -> ControllerRefNode:
    """A controller reference (ADR-003): ``- controller: <name>`` or
    ``- controller: {name: <name>}``. The name resolves against the registry at
    validate/compile time; the loaded manifests ride along for the expansion."""
    if isinstance(spec, str):
        name = spec
    elif isinstance(spec, dict):
        name = str(spec.get("name") or "")
    else:
        raise SchemaError(path, f"controller must be a name or a mapping with 'name', got {type(spec).__name__}")
    if not name:
        raise SchemaError(path, "controller reference needs a name")
    return ControllerRefNode(name=name, manifests=manifests)


def _parse_loop(spec: Any, manifests: Dict[str, ActionManifest], path: str) -> LoopNode:
    if not isinstance(spec, dict):
        raise SchemaError(path, "loop must be a mapping")
    body = spec.get("body")
    if body is None:
        raise SchemaError(path, "loop requires a 'body'")
    return LoopNode(
        name=str(spec.get("name", "loop")),
        body=_parse_step(body, manifests, f"{path}.body"),
        until=spec.get("until"),
        abort_when=spec.get("abort_when"),
        while_=spec.get("while"),
        max_iterations=int(spec.get("max_iterations", 1)),
    )


def _parse_sequence(spec: Any, manifests: Dict[str, ActionManifest], path: str) -> SequenceNode:
    if not isinstance(spec, dict):
        raise SchemaError(path, "sequence must be a mapping")
    steps = spec.get("steps") or []
    return SequenceNode(
        name=str(spec.get("name", "sequence")),
        steps=tuple(_parse_step(s, manifests, f"{path}[{i}]") for i, s in enumerate(steps)),
    )


def _parse_parallel(spec: Any, manifests: Dict[str, ActionManifest], path: str) -> ParallelNode:
    if not isinstance(spec, dict):
        raise SchemaError(path, "parallel must be a mapping")
    branches = spec.get("branches") or spec.get("steps") or []
    return ParallelNode(
        name=str(spec.get("name", "parallel")),
        branches=tuple(_parse_step(s, manifests, f"{path}[{i}]") for i, s in enumerate(branches)),
    )


def parse_document(doc: Dict[str, Any], manifests: Dict[str, ActionManifest], *, source: str = "") -> WorkflowNode:
    """Parse a loaded YAML mapping (+ resolved manifests) into a ``WorkflowNode``."""
    name = str(doc.get("name") or "workflow")
    phases_doc = doc.get("phases") or {}
    if not isinstance(phases_doc, dict):
        raise SchemaError(source or name, "'phases' must be a mapping of phase-name -> step list")
    phases = []
    for pname, steps in phases_doc.items():
        if not isinstance(steps, list):
            raise SchemaError(f"{name}.phases.{pname}", "phase steps must be a list")
        phases.append(
            PhaseNode(
                name=str(pname),
                steps=tuple(_parse_step(s, manifests, f"{pname}[{i}]") for i, s in enumerate(steps)),
            )
        )
    return WorkflowNode(
        name=name,
        phases=tuple(phases),
        inputs=tuple(str(k) for k in (doc.get("inputs") or ())),
        seed=dict(doc.get("seed") or {}),
        budgets=dict(doc.get("budgets") or {}),
        controllers=tuple(str(c) for c in (doc.get("controllers") or ())),
        manifests=manifests,
        admin_spec_split=float(doc.get("admin_spec_split", 0.5)),
        terminal_when=(str(doc["terminal_when"]) if doc.get("terminal_when") else None),
    )


def _apply_header(merged: WorkflowNode, doc: Dict[str, Any], base: WorkflowNode) -> WorkflowNode:
    """Carry the overlay document's own header onto the merged tree: its ``name``
    wins, and its ``seed`` / ``budgets`` *extend* the base's (overlay keys override
    same-named base keys). ``inputs`` and ``terminal_when`` are inherited from the
    base unless the overlay restates them."""
    from dataclasses import replace

    return replace(
        merged,
        name=str(doc.get("name") or merged.name),
        seed={**base.seed, **(doc.get("seed") or {})},
        budgets={**base.budgets, **(doc.get("budgets") or {})},
        inputs=(tuple(str(k) for k in doc["inputs"]) if doc.get("inputs") else merged.inputs),
        controllers=(tuple(str(c) for c in doc["controllers"]) if doc.get("controllers") else merged.controllers),
        terminal_when=(str(doc["terminal_when"]) if doc.get("terminal_when") else merged.terminal_when),
    )


def load_schemas(base_dir: str, spec: Any) -> Dict[str, Any]:
    """Resolve a workflow's ``schemas:`` declaration into ``{"shelf.key": ShelfSchema}``.

    ``spec`` may be an **inline map** (``ref -> schema``) or a **list of file globs**
    (each file a ``ref -> schema`` map), resolved relative to ``base_dir`` — the
    ``#base`` convention, exactly like ``uses:``. Later files/keys win on collision.
    The declarative-file form is the DRY authoring model: a key like ``input.job`` is
    declared once and shared by every action that reads it."""
    if not spec:
        return {}
    if isinstance(spec, dict):
        return parse_schemas(spec, where="schemas")
    import glob as _glob

    import yaml

    merged: Dict[str, Any] = {}
    for g in spec:
        pattern = g if os.path.isabs(g) else os.path.join(base_dir, g)
        for path in sorted(_glob.glob(pattern, recursive=True)):
            with open(path, encoding="utf-8") as fh:
                d = yaml.safe_load(fh) or {}
            merged.update(parse_schemas(d, where=os.path.relpath(path, base_dir)))
    return merged


def load_workflow(relpath: str) -> WorkflowNode:
    """Read + parse a workflow YAML into a ``WorkflowNode``. ``relpath`` resolves
    via :func:`_resolve_path`; ``uses:`` manifest globs resolve relative to the
    workflow file's directory. A document that declares ``extends: <base>`` is an
    *overlay*: the sibling ``<base>.yml`` is loaded and parsed, this document's
    ``overlays:`` ops are folded onto it (see :mod:`foundation.workflow.overlay`),
    and the merged tree carries this document's header."""
    import yaml

    path = _resolve_path(relpath)
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    wf_dir = os.path.dirname(os.path.abspath(path))
    manifests = load_manifests(wf_dir, doc.get("uses") or [])
    schemas = load_schemas(wf_dir, doc.get("schemas"))

    from dataclasses import replace

    extends = doc.get("extends")
    if extends:
        from .overlay import apply_overlays

        base = load_workflow(os.path.join(wf_dir, f"{extends}.yml"))
        merged = apply_overlays(base, doc.get("overlays") or [], manifests, source=relpath)
        # The merged tree's interface library is the base's plus the overlay's own.
        merged = replace(merged, manifests={**base.manifests, **manifests})
        # Contract likewise extends the base's (overlay keys override same refs).
        return replace(_apply_header(merged, doc, base), schemas={**base.schemas, **schemas})

    return replace(parse_document(doc, manifests, source=relpath), schemas=schemas)
