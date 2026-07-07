#!/usr/bin/env python3
"""graph.py — a Visitor that flattens a workflow node tree into a drawable graph.

This is one more *operation* over the ``nodes`` Elements (the visitor pattern the
rest of ``foundation.workflow`` uses): it walks the tree and emits a plain
``{"nodes": [...], "edges": [...]}`` graph — the shape a debug viewer draws as a
statechart. It is deliberately serialisable (JSON-only primitives) so a UI can
render it without importing any engine code.

Each drawable node carries a ``state_id`` — the *exact* statechart id the compiled
controller will mint for that state (``<workflow>/<i>.<phase>/<j>.<token>``). The
path is threaded here identically to ``foundation.actions.compile_node`` /
``_child_path`` (action leaf name = its token; a governor is transparent, so the
token survives; control names come from the node), so a live run's
``Interpreter`` ``on_enter``/``on_leave`` events map back onto the drawn nodes by
``state_id`` with no guesswork.

Controllers and supersede (ADR-003). A ``controller:`` reference draws as a
``controller`` boundary node; given a ``registry`` it expands into its actions'
subgraph, whose state_ids match the compiled controller (so its inner states
light up live). A **superseedable** state is rendered too: the workflow's
declared controllers that no phase references are *dynamic* supersede targets —
``ctx.supersede`` spawns them at run time. They are drawn dashed, and — crucially
— their subgraph is minted under the variant's OWN name, exactly the id namespace
the nested interpreter uses when ``compile_controller`` spawns the variant. So
when a run actually supersedes into one, that dashed subgraph lights up with no
special-casing in the engine or the viewer.

Each ``visit_*`` returns ``(entry_id, [exit_ids])`` — the single node control
enters the subgraph through, and the nodes it can leave through — so a parent can
chain a Sequence, fan a Parallel, close a Loop's back-edge, or open a controller.
Control flow is made explicit as typed edges: ``seq``, ``fork``/``join``
(Parallel), ``loop-back`` (Loop back-edge, dashed), and ``supersede`` (a
superseding state to its dynamic target, dashed).

Leaf module: stdlib + sibling workflow leaves (``make_id`` for id parity).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from foundation.actions.statechart import make_id
from foundation.needs import KIND_SERVICE

from .nodes import ActionRefNode, ControllerRefNode
from .visitor import WorkflowVisitor

Exit = List[str]
SubGraph = Tuple[str, Exit]


def _seg_name(step: Any) -> str:
    """The path segment a node compiles under — must match ``compile_node``.

    An action leaf compiles under its Action's ``name``, which is the token (a
    governor delegates its name to the inner action). A ``proxy`` action is built
    as ``factory.proxy("proxy:<token>", ...)``, so its segment carries that prefix.
    Every control node (Sequence/Loop/Parallel/Phase) and a controller reference
    compiles under its ``name``.
    """
    if isinstance(step, ActionRefNode):
        m = step.manifest
        return ("proxy:" + m.token) if m.kind == "proxy" else m.token
    return getattr(step, "name", "step")


class GraphVisitor(WorkflowVisitor):
    """Flatten a ``WorkflowNode`` into ``{name, phases, budgets, inputs, nodes,
    edges}``. Call :meth:`visit` on the root workflow node. Pass a ``registry`` to
    expand ``controller:`` references (and dynamic supersede targets) into their
    action subgraphs; without one they draw as single, unresolved boundary nodes."""

    def __init__(self, registry: Any = None) -> None:
        self.registry = registry
        self.nodes: List[Dict[str, Any]] = []
        self.edges: List[Dict[str, Any]] = []
        self._counter = 0
        self._phase = ""
        self._pathstack: List[str] = []
        self._as_target = False               # rendering a dynamic supersede target
        self._seen_controllers: set = set()    # phase-referenced controller names
        self._superseders: List[Any] = []      # (name, node id) of phase-referenced controller boundaries
        self._expanding: set = set()           # cyclic controller-reference guard

    # -- helpers ------------------------------------------------------------
    @property
    def _path(self) -> str:
        return self._pathstack[-1] if self._pathstack else ""

    def _walk(self, node: Any, path: str) -> SubGraph:
        """Visit ``node`` as the state at ``path`` (its compiled ``state_id``)."""
        self._pathstack.append(path)
        try:
            return node.accept(self)
        finally:
            self._pathstack.pop()

    def _nid(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def _add_node(self, nid: str, label: str, kind: str, **meta: Any) -> None:
        node: Dict[str, Any] = {"id": nid, "label": label, "kind": kind, "phase": self._phase}
        node.update({k: v for k, v in meta.items() if v is not None})
        self.nodes.append(node)

    def _add_edge(self, src: str, dst: str, kind: str = "seq", label: str = "") -> None:
        edge: Dict[str, Any] = {"from": src, "to": dst, "kind": kind}
        if label:
            edge["label"] = label
        self.edges.append(edge)

    def _chain(self, steps: Tuple[Any, ...]) -> SubGraph:
        """Run children one at a time (an OR-superstate), wiring each exit into the
        next entry and minting each child's path as ``<parent>/<i>.<name>``."""
        parent = self._path
        first_entry = ""
        prev_exits: Exit = []
        for i, s in enumerate(steps):
            entry, exits = self._walk(s, make_id(parent, _seg_name(s), i))
            if not first_entry:
                first_entry = entry
            for x in prev_exits:
                self._add_edge(x, entry, "seq")
            prev_exits = exits
        return first_entry, prev_exits

    # -- controller expansion ----------------------------------------------
    def _controller_steps(self, node: Any) -> Optional[List[Any]]:
        """The controller's parsed step nodes, or ``None`` when it cannot be
        expanded (no registry, unregistered, or a cyclic reference) — in which case
        the caller draws it as a single, unresolved boundary node."""
        if self.registry is None or not self.registry.has_controller(node.name):
            return None
        if node.name in self._expanding:
            return None
        from .loader import _parse_step

        spec = self.registry.controller_spec(node.name)
        return [_parse_step(s, node.manifests, f"{node.name}[{i}]") for i, s in enumerate(spec.steps)]

    def _service_needs(self, steps: Optional[List[Any]]) -> List[str]:
        """The service-kind needs the controller's actions require (the cross-layer
        dependency worth surfacing on the boundary node)."""
        out: List[str] = []
        for s in steps or ():
            if isinstance(s, ActionRefNode):
                for n in s.manifest.effective_needs:
                    if n.kind == KIND_SERVICE and n.key not in out:
                        out.append(n.key)
            elif isinstance(s, ControllerRefNode):
                for k in self._service_needs(self._controller_steps(s)):
                    if k not in out:
                        out.append(k)
        return out

    # -- visitor methods ----------------------------------------------------
    def visit_workflow(self, node: Any) -> Dict[str, Any]:
        self.nodes, self.edges, self._counter = [], [], 0
        self._pathstack = []
        self._seen_controllers = set()
        self._superseders = []
        self._as_target = False
        root = node.name  # the controller compiles its lifecycle at its own name
        start = self._nid("start")
        self._phase = ""
        self._add_node(start, "start", "start")
        prev_exits: Exit = [start]
        for i, phase in enumerate(node.phases):
            self._phase = phase.name
            entry, exits = self._walk(phase, make_id(root, phase.name, i))
            for x in prev_exits:
                self._add_edge(x, entry, "seq")
            prev_exits = exits
        self._phase = ""
        end = self._nid("end")
        self._add_node(end, "done", "end")
        for x in prev_exits:
            self._add_edge(x, end, "seq")

        # Dynamic supersede targets: declared controllers no phase references. They
        # are entered at run time via ``ctx.supersede``; the interpreter runs the
        # spawned variant as a nested chart rooted at the variant's OWN name, so we
        # mint their subgraph under that name (id parity → they light up live). The
        # dashed ``supersede`` edge is anchored to the target's NAMESPACE-KIN
        # controller (``seed.handle.*`` ← ``seed.intake``): a variant is spawned by
        # its own family's router, so wiring it to every phase-referenced controller
        # would draw false "later phases supersede into seeding" edges (and drag the
        # variant subgraphs into those phases' bands). Only when no kin exists does
        # the edge fall back to every potential superseder.
        self._as_target = True
        self._phase = ""
        for name in (getattr(node, "controllers", ()) or ()):
            if name in self._seen_controllers:
                continue
            entry, _exits = self._walk(
                ControllerRefNode(name=name, manifests=dict(getattr(node, "manifests", {}) or {})),
                make_id("", name),
            )
            ns = name.split(".", 1)[0]
            kin = [nid for cname, nid in self._superseders if cname.split(".", 1)[0] == ns]
            for src in (kin or [nid for _cname, nid in self._superseders]):
                self._add_edge(src, entry, "supersede", label="supersede")
        self._as_target = False

        return {
            "name": node.name,
            "phases": [p.name for p in node.phases],
            "budgets": dict(node.budgets or {}),
            "inputs": list(node.inputs or ()),
            "controllers": list(getattr(node, "controllers", ()) or ()),
            # The declarative shelf-key contract, as JSON, keyed by "shelf.key" — the
            # viewer reflects these into typed, validated probe forms.
            "schemas": {ref: s.to_dict() for ref, s in (getattr(node, "schemas", {}) or {}).items()},
            "nodes": self.nodes,
            "edges": self.edges,
        }

    def visit_phase(self, node: Any) -> SubGraph:
        return self._chain(node.steps)

    def visit_sequence(self, node: Any) -> SubGraph:
        return self._chain(node.steps)

    def visit_controller_ref(self, node: Any) -> SubGraph:
        """A controller boundary. With a registry, expand its action chain under the
        controller's compiled path (so inner states carry live-matching ids);
        otherwise draw a single unresolved boundary node. A dynamic target
        (``_as_target``) is flagged and drawn dashed by the viewer."""
        path = self._path
        name = node.name
        nid = self._nid("ctrl")
        target = self._as_target
        steps = self._controller_steps(node)
        self._add_node(
            nid, name, "controller",
            state_id=path,
            controller=name,
            supersede_target=(True if target else None),
            needs=(self._service_needs(steps) or None),
            unresolved=(True if steps is None else None),
        )
        if not target:
            self._seen_controllers.add(name)
            self._superseders.append((name, nid))
        if not steps:
            return nid, [nid]
        self._expanding.add(name)
        try:
            first_entry = ""
            prev_exits: Exit = []
            for j, s in enumerate(steps):
                entry, exits = self._walk(s, make_id(path, _seg_name(s), j))
                if not first_entry:
                    first_entry = entry
                for x in prev_exits:
                    self._add_edge(x, entry, "seq")
                prev_exits = exits
        finally:
            self._expanding.discard(name)
        if first_entry:
            self._add_edge(nid, first_entry, "seq")
        return nid, (prev_exits or [nid])

    def visit_parallel(self, node: Any) -> SubGraph:
        ppath = self._path
        fork = self._nid("fork")
        join = self._nid("join")
        self._add_node(fork, node.name or "parallel", "fork", state_id=ppath)
        self._add_node(join, "join", "join")
        for i, branch in enumerate(node.branches):
            entry, exits = self._walk(branch, make_id(ppath, _seg_name(branch), i))
            self._add_edge(fork, entry, "fork")
            for x in exits:
                self._add_edge(x, join, "join")
        return fork, [join]

    def visit_loop(self, node: Any) -> SubGraph:
        loop_path = self._path
        head = self._nid("loop")
        until = node.until or (f"!({node.while_})" if node.while_ else "")
        self._add_node(
            head,
            node.name or "loop",
            "loop",
            state_id=loop_path,
            until=node.until or None,
            abort_when=node.abort_when or None,
            while_=node.while_ or None,
            max_iterations=int(node.max_iterations),
            exit_when=until or None,
        )
        body_entry, body_exits = self._walk(node.body, make_id(loop_path, _seg_name(node.body), 0))
        self._add_edge(head, body_entry, "seq")
        back_label = f"retry ≤{node.max_iterations}"
        for x in body_exits:
            self._add_edge(x, head, "loop-back", label=back_label)
        # The head is both the entry and the forward exit: control leaves it when
        # `until` is satisfied (shown on the node), otherwise the body loops back.
        return head, [head]

    def visit_action_ref(self, node: Any) -> SubGraph:
        m = node.manifest
        nid = self._nid("n")
        self._add_node(
            nid,
            m.token,
            "action",
            state_id=self._path,
            namespace=m.namespace,
            action_kind=m.kind,
            budget=m.budget,
            permission=list(m.permission) if m.permission else None,
            needs=[n.key for n in m.effective_needs] or None,
            inputs=[r.ref for r in m.inputs] or None,
            outputs=[r.ref for r in m.outputs] or None,
        )
        return nid, [nid]


def workflow_to_graph(node: Any, registry: Any = None) -> Dict[str, Any]:
    """Convenience: flatten a parsed ``WorkflowNode`` into a drawable graph dict.
    Pass a ``TokenRegistry`` to expand controller references + supersede targets."""
    return GraphVisitor(registry).visit(node)
