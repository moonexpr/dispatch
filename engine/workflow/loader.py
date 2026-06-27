#!/usr/bin/env python3
"""loader.py — the token parser: YAML document -> workflow node tree.

Reads a workflow YAML, expands its ``uses:`` includes into the per-action
interface manifests (``#base`` semantics — globs resolve relative to the workflow
file's own directory), and parses each phase's step list into the structural node
tree. Each step is either a bare ``ns:name`` token (an ``ActionRefNode``, resolved
against the loaded manifests) or a single-key mapping naming a structural token
(``loop`` / ``sequence`` / ``parallel``). Shape problems raise ``SchemaError`` with
a located path so the "format test" points at the exact spot.

The workflow path resolves through ``engine.filesys`` (app-dir rooted, with an
absolute / existing-path escape hatch) so ``app/config/baseworkflow.yml`` is found
the same way the other config YAMLs are.

Leaf module: stdlib + in-function PyYAML + ``engine.filesys`` + sibling leaves.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from engine import filesys

from .manifest import ActionManifest, load_manifests
from .nodes import (
    ActionRefNode,
    LoopNode,
    Node,
    ParallelNode,
    PhaseNode,
    SequenceNode,
    WorkflowNode,
)

_STRUCTURAL = ("loop", "sequence", "parallel")


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
        raise SchemaError(path, f"unknown structural token {kind!r} (one of {_STRUCTURAL} or an action token)")
    raise SchemaError(path, f"a step is a token string or a mapping, got {type(step).__name__}")


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
        admin_spec_split=float(doc.get("admin_spec_split", 0.5)),
        terminal_when=(str(doc["terminal_when"]) if doc.get("terminal_when") else None),
    )


def load_workflow(relpath: str) -> WorkflowNode:
    """Read + parse a workflow YAML into a ``WorkflowNode``. ``relpath`` resolves
    via :func:`_resolve_path`; ``uses:`` manifest globs resolve relative to the
    workflow file's directory."""
    import yaml

    path = _resolve_path(relpath)
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    wf_dir = os.path.dirname(os.path.abspath(path))
    manifests = load_manifests(wf_dir, doc.get("uses") or [])
    return parse_document(doc, manifests, source=relpath)
