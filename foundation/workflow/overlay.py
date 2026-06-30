#!/usr/bin/env python3
"""overlay.py — apply an overlay document's ops to a base ``WorkflowNode``.

A workflow may declare ``extends: <base>`` plus an ordered ``overlays:`` list,
expressing specialization as a *diff* over a base workflow rather than a fork. The loader resolves
and parses the base into a ``WorkflowNode``, then this module folds the ops onto
it — top to bottom, each op seeing the previous ops' effect — and returns the
merged tree. Four verbs:

  * ``replace: <tok> with: <tok>`` — swap the action bound at ``<tok>``'s position
    for ``<with>`` (same position; new manifest/binding).
  * ``extend: <tok> with: <tok> [mode: before|after]`` — compose: insert ``<with>``
    adjacent to ``<tok>`` in its phase (the base action still runs).
  * ``add: <tok> phase: <p> [after|before: <tok> | at: start|end]`` — splice a new
    action into phase ``<p>``.
  * ``proxy: <tok> rewire: {in: {...}, out: {...}}`` — replace ``<tok>`` with a
    ``kind: proxy`` wrapper that reroutes its shelf I/O (the new proxy action).

New/substitute action manifests come from the overlay document's own ``uses:``
globs (passed in as ``manifests``).

Leaf module: stdlib + sibling workflow leaves (nodes, manifest).
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from .manifest import ActionManifest, proxy_manifest
from .nodes import (
    ActionRefNode,
    LoopNode,
    Node,
    ParallelNode,
    PhaseNode,
    SequenceNode,
    WorkflowNode,
)

_VERBS = ("replace", "extend", "add", "proxy")


class OverlayError(Exception):
    """A malformed or unsatisfiable overlay op (located message)."""


# -- tree walk helpers ------------------------------------------------------
# TODO(John): Refactor this using a better design pattern.
def _contains(node: Node, token: str) -> bool:
    """True if any ``ActionRefNode`` under ``node`` carries ``token``."""
    if isinstance(node, ActionRefNode):
        return node.token == token
    if isinstance(node, (SequenceNode, PhaseNode)):
        return any(_contains(s, token) for s in node.steps)
    if isinstance(node, ParallelNode):
        return any(_contains(b, token) for b in node.branches)
    if isinstance(node, LoopNode):
        return _contains(node.body, token)
    return False


def _map_actions(node: Node, fn: Callable[[ActionRefNode], Node]) -> Node:
    """Rebuild ``node``, applying ``fn`` to every ``ActionRefNode`` leaf."""
    if isinstance(node, ActionRefNode):
        return fn(node)
    if isinstance(node, WorkflowNode):
        return replace(node, phases=tuple(_map_actions(p, fn) for p in node.phases))
    if isinstance(node, SequenceNode):
        return replace(node, steps=tuple(_map_actions(s, fn) for s in node.steps))
    if isinstance(node, PhaseNode):
        return replace(node, steps=tuple(_map_actions(s, fn) for s in node.steps))
    if isinstance(node, ParallelNode):
        return replace(node, branches=tuple(_map_actions(b, fn) for b in node.branches))
    if isinstance(node, LoopNode):
        return replace(node, body=_map_actions(node.body, fn))
    return node


def _manifest(manifests: Dict[str, ActionManifest], token: str, where: str) -> ActionManifest:
    m = manifests.get(token)
    if m is None:
        raise OverlayError(
            f"{where}: action token {token!r} not found in the overlay's own manifests "
            f"(declare it under the overlay's uses:); known: {sorted(manifests)}"
        )
    return m


# -- the four verbs ---------------------------------------------------------
def _op_replace(wf: WorkflowNode, op: Dict[str, Any], manifests, where: str) -> WorkflowNode:
    target = str(op["replace"])
    with_tok = op.get("with")
    if not with_tok:
        raise OverlayError(f"{where}: replace {target!r} needs a 'with:' token")
    new = ActionRefNode(token=str(with_tok), manifest=_manifest(manifests, str(with_tok), where))
    hit = {"n": 0}

    def fn(a: ActionRefNode) -> Node:
        if a.token == target:
            hit["n"] += 1
            return new
        return a

    wf = _map_actions(wf, fn)  # type: ignore[assignment]
    if not hit["n"]:
        raise OverlayError(f"{where}: replace target {target!r} not found in the base workflow")
    return wf


def _op_proxy(wf: WorkflowNode, op: Dict[str, Any], manifests, where: str) -> WorkflowNode:
    target = str(op["proxy"])
    rewire = op.get("rewire") or {}
    hit = {"n": 0}

    def fn(a: ActionRefNode) -> Node:
        if a.token == target:
            hit["n"] += 1
            pm = proxy_manifest(a.manifest, rewire.get("in"), rewire.get("out"), source=where)
            return ActionRefNode(token=a.token, manifest=pm)
        return a

    wf = _map_actions(wf, fn)  # type: ignore[assignment]
    if not hit["n"]:
        raise OverlayError(f"{where}: proxy target {target!r} not found in the base workflow")
    return wf


def _phase_index_for_token(wf: WorkflowNode, token: str) -> int:
    for i, p in enumerate(wf.phases):
        if _contains(p, token):
            return i
    raise OverlayError(f"anchor token {token!r} not found in any phase")


def _phase_index_by_name(wf: WorkflowNode, name: str) -> int:
    for i, p in enumerate(wf.phases):
        if p.name == name:
            return i
    raise OverlayError(f"phase {name!r} not found (phases: {[p.name for p in wf.phases]})")


def _insert(
    wf: WorkflowNode,
    new_node: ActionRefNode,
    *,
    phase: Optional[str],
    after: Optional[str],
    before: Optional[str],
    at: Optional[str],
    where: str,
) -> WorkflowNode:
    """Splice ``new_node`` into a phase at the top-level step granularity. The
    anchor (``after``/``before`` token) resolves to the top-level step that
    *contains* it (so a token nested in a loop anchors on the loop)."""
    if phase is not None:
        pi = _phase_index_by_name(wf, phase)
    elif after is not None:
        pi = _phase_index_for_token(wf, after)
    elif before is not None:
        pi = _phase_index_for_token(wf, before)
    else:
        raise OverlayError(f"{where}: insertion needs one of phase:/after:/before:")

    p = wf.phases[pi]
    steps: List[Node] = list(p.steps)
    if at == "start":
        idx = 0
    elif at == "end":
        idx = len(steps)
    elif after is not None:
        idx = next(i for i, s in enumerate(steps) if _contains(s, after)) + 1
    elif before is not None:
        idx = next(i for i, s in enumerate(steps) if _contains(s, before))
    else:
        idx = len(steps)  # default: append
    steps.insert(idx, new_node)
    new_phases = list(wf.phases)
    new_phases[pi] = replace(p, steps=tuple(steps))
    return replace(wf, phases=tuple(new_phases))


def _op_add(wf: WorkflowNode, op: Dict[str, Any], manifests, where: str) -> WorkflowNode:
    token = str(op["add"])
    node = ActionRefNode(token=token, manifest=_manifest(manifests, token, where))
    return _insert(
        wf, node,
        phase=(str(op["phase"]) if op.get("phase") is not None else None),
        after=(str(op["after"]) if op.get("after") is not None else None),
        before=(str(op["before"]) if op.get("before") is not None else None),
        at=(str(op["at"]) if op.get("at") is not None else None),
        where=where,
    )


def _op_extend(wf: WorkflowNode, op: Dict[str, Any], manifests, where: str) -> WorkflowNode:
    target = str(op["extend"])
    with_tok = op.get("with")
    if not with_tok:
        raise OverlayError(f"{where}: extend {target!r} needs a 'with:' token")
    mode = str(op.get("mode", "after"))
    if mode not in ("before", "after"):
        raise OverlayError(
            f"{where}: extend mode {mode!r} not supported (use 'before' or 'after'; "
            f"'wrap' is reserved for a future increment)"
        )
    node = ActionRefNode(token=str(with_tok), manifest=_manifest(manifests, str(with_tok), where))
    return _insert(
        wf, node,
        phase=None,
        after=(target if mode == "after" else None),
        before=(target if mode == "before" else None),
        at=None,
        where=where,
    )


_DISPATCH: Dict[str, Callable[..., WorkflowNode]] = {
    "replace": _op_replace,
    "extend": _op_extend,
    "add": _op_add,
    "proxy": _op_proxy,
}


def _verb_of(op: Dict[str, Any], where: str) -> str:
    verbs = [v for v in _VERBS if v in op]
    if len(verbs) != 1:
        raise OverlayError(
            f"{where}: each overlay op names exactly one verb of {_VERBS}, got keys {sorted(op)}"
        )
    return verbs[0]


def apply_overlays(
    base: WorkflowNode,
    ops: Any,
    manifests: Dict[str, ActionManifest],
    *,
    source: str = "",
) -> WorkflowNode:
    """Fold ``ops`` (a list of overlay-op mappings) onto ``base``, in order."""
    wf = base
    for i, op in enumerate(ops or ()):
        where = f"{source}.overlays[{i}]"
        if not isinstance(op, dict):
            raise OverlayError(f"{where}: an overlay op is a mapping, got {type(op).__name__}")
        wf = _DISPATCH[_verb_of(op, where)](wf, op, manifests, where)
    return wf
