#!/usr/bin/env python3
"""visitor.py — the operations over the workflow node tree (the Visitors).

Each visitor is one operation; the node classes never change as operations are
added. Three concrete visitors:

  * ``CompileVisitor`` — node tree → a ``foundation.actions`` statechart. The crux is
    the **interface wrapper** built in ``visit_action_ref``: a ``(payload, ctx) ->
    value`` closure that reads the action's declared ``interface.in`` from the
    shelves into an ``inputs`` dict, calls the registered body, and writes the
    body's returned dict back to ``interface.out`` — so the manifest's I/O is the
    *runtime* contract, and the bodies stay pure (no shelf calls). The wrapped
    body is handed to the factory exactly as the old ``_inference``/``_procedure``
    did, then wrapped in the same governors (permission inner, budget outer).
  * ``ValidateVisitor`` — node tree → a list of ``ValidationError`` (the YAML
    "format test"): unknown bind/budget/predicate, structural problems, the
    data-flow check (every ``in`` is seeded or produced upstream), AND — with a
    ``ServiceRegistry`` — the needs-satisfaction check (ADR-003: every declared
    controller's non-optional needs met by the environment's provisions).
  * ``RenderVisitor`` — node tree → a plain dict (the inverse of the loader; used
    for a serialization round-trip).

A ``controller:`` reference expands through ONE code path for both the static
and the dynamic case: the visitors expand it in place (an OR-superstate named
after the controller), and :func:`compile_controller` materializes the same
registered spec into a runnable ``Control`` for run-time spawners (supersede
targets). :func:`controller_needs` computes the ADR-003 needs union without
executing anything.

Leaf module: stdlib + ``foundation.actions``/``foundation.needs`` + sibling
workflow leaves.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from foundation.actions import InferenceSpec, Loop, Parallel, Sequence
from foundation.needs import KIND_INPUT, Need, NeedSet, Provision, input_provisions

from .controller import CompiledWorkflow
from .loader import SchemaError, _parse_step
from .nodes import ActionRefNode, ControllerRefNode, SequenceNode
from .predicate import PredicateError, PredicateValidator, compile_predicate, parse_predicate

# Per-inference deterministic mock cost + default route — preserve the old
# base workflow values so the budget meters and spend are identical.
_MOCK_USAGE = 1_000
_MODEL = "gen-default"


# -- base visitor -----------------------------------------------------------
class WorkflowVisitor:
    """Base visitor: ``visit`` dispatches via the node's ``accept``."""

    def visit(self, node: Any) -> Any:
        return node.accept(self)

    def visit_workflow(self, node: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def visit_phase(self, node: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def visit_sequence(self, node: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def visit_parallel(self, node: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def visit_loop(self, node: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def visit_action_ref(self, node: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def visit_controller_ref(self, node: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


# -- controller expansion (one path for static AND dynamic use) --------------
def expand_controller(ref: ControllerRefNode, registry: Any) -> SequenceNode:
    """Expand a controller reference into a ``SequenceNode`` named after the
    controller: the registered spec's steps, parsed with the loader's own step
    grammar against the reference's manifest library. Raises ``UnknownToken``
    for an unregistered name and ``SchemaError`` for a malformed spec step."""
    spec = registry.controller_spec(ref.name)
    steps = tuple(
        _parse_step(s, ref.manifests, f"{ref.name}[{i}]") for i, s in enumerate(spec.steps)
    )
    return SequenceNode(name=ref.name, steps=steps)


def _node_needs(node: Any, registry: Any, manifests: Dict[str, Any], seen: Set[str]) -> NeedSet:
    """The needs union over one node subtree (ADR-003). ``seen`` guards cyclic
    controller references."""
    if isinstance(node, ActionRefNode):
        return NeedSet(node.manifest.effective_needs)
    if isinstance(node, ControllerRefNode):
        return controller_needs(node.name, registry=registry, manifests=node.manifests or manifests, _seen=seen)
    ns = NeedSet()
    for child in (
        list(getattr(node, "steps", ()) or ())
        + list(getattr(node, "branches", ()) or ())
        + ([node.body] if getattr(node, "body", None) is not None else [])
    ):
        ns = ns | _node_needs(child, registry, manifests, seen)
    return ns


def controller_needs(
    name: str, *, registry: Any, manifests: Dict[str, Any], _seen: Optional[Set[str]] = None
) -> NeedSet:
    """A controller's needs: the union of its member actions' effective needs
    plus the spec's own extras — computable statically, never by execution."""
    seen = _seen if _seen is not None else set()
    if name in seen:
        return NeedSet()  # cycle: the validator reports it; the union just terminates
    seen.add(name)
    spec = registry.controller_spec(name)
    ns = NeedSet(spec.needs)
    for i, s in enumerate(spec.steps):
        node = _parse_step(s, manifests, f"{name}[{i}]")
        ns = ns | _node_needs(node, registry, manifests, seen)
    return ns


def compile_controller(
    name: str,
    *,
    registry: Any,
    manifests: Dict[str, Any],
    factory: Any,
    budgets: Optional[Dict[str, int]] = None,
) -> Sequence:
    """Materialize a registered controller into a runnable ``Control`` — the
    dynamic counterpart of a phase's ``controller:`` reference, used by run-time
    spawners (e.g. a supersede target). Same expansion, same compilation."""
    visitor = CompileVisitor(factory, registry)
    visitor.budgets = dict(budgets or {})
    return visitor.visit(ControllerRefNode(name=name, manifests=dict(manifests)))


# -- shelf I/O helpers (the interface wiring) -------------------------------
def _read_inputs(manifest: Any, ctx: Any) -> Dict[str, Any]:
    inputs: Dict[str, Any] = {}
    for ref in manifest.inputs:
        shelf = getattr(ctx.shelves, ref.shelf)
        inputs[ref.alias] = shelf.get(ref.key)
    return inputs


def _write_outputs(manifest: Any, ctx: Any, out: Any) -> None:
    if not isinstance(out, dict):
        return
    for ref in manifest.outputs:
        if ref.alias in out:
            getattr(ctx.shelves, ref.shelf).put(ref.key, out[ref.alias])


# -- compile ----------------------------------------------------------------
class CompileVisitor(WorkflowVisitor):
    """Compile the node tree into a ``foundation.actions`` controller + statechart."""

    def __init__(self, factory: Any, registry: Any) -> None:
        self.factory = factory
        self.registry = registry
        self.budgets: Dict[str, int] = {}
        self.terminal: Optional[Callable[[Any, Any], bool]] = None
        self._expanding: Set[str] = set()  # cyclic controller-reference guard

    def visit_workflow(self, node: Any) -> CompiledWorkflow:
        self.budgets = dict(node.budgets or {})
        # Compile the optional declarative early-completion predicate first, so the
        # per-phase Sequences built below can carry it (and the lifecycle too).
        tw = getattr(node, "terminal_when", None)
        self.terminal = compile_predicate(tw, self.registry) if tw else None
        phase_controls = [self.visit(p) for p in node.phases]
        return CompiledWorkflow(
            name=node.name,
            phase_names=[p.name for p in node.phases],
            phase_controls=phase_controls,
            terminal_when=self.terminal,
        )

    def visit_phase(self, node: Any) -> Sequence:
        return Sequence([self.visit(s) for s in node.steps], name=node.name, terminal_when=self.terminal)

    def visit_sequence(self, node: Any) -> Sequence:
        return Sequence([self.visit(s) for s in node.steps], name=node.name)

    def visit_parallel(self, node: Any) -> Parallel:
        return Parallel([self.visit(b) for b in node.branches], name=node.name)

    def visit_loop(self, node: Any) -> Loop:
        body = self.visit(node.body)
        until = compile_predicate(node.until, self.registry) if node.until else None
        abort = compile_predicate(node.abort_when, self.registry) if node.abort_when else None
        if node.while_ and until is None:
            w = compile_predicate(node.while_, self.registry)
            until = lambda result, ctx, _w=w: not _w(result, ctx)
        return Loop(
            body,
            until=until,
            abort_when=abort,
            max_iterations=int(node.max_iterations),
            name=node.name,
        )

    def visit_action_ref(self, node: Any) -> Any:
        m = node.manifest
        if m.kind == "proxy":
            return self._build_proxy(m)
        return self._build_action(m)

    def visit_controller_ref(self, node: Any) -> Sequence:
        """Expand the registered spec and compile it as an OR-superstate named
        after the controller (ADR-001's Controller = OR-superstate, now at the
        YAML surface)."""
        if node.name in self._expanding:
            raise SchemaError(node.name, "cyclic controller reference")
        self._expanding.add(node.name)
        try:
            return self.visit(expand_controller(node, self.registry))
        finally:
            self._expanding.discard(node.name)

    def _build_action(self, m: Any) -> Any:
        """Compile one (non-proxy) action manifest into a governed Action."""
        binding = self.registry.action_binding(m.bind)
        body = self._interface_body(m, binding)
        if m.kind == "inference":
            spec = InferenceSpec(oracle=body, fmt=m.fmt, mock_usage=_MOCK_USAGE, model=_MODEL)
            act = self.factory.inference(m.token, spec)
        else:
            act = self.factory.procedure(m.token, body)
        # Governor stack, innermost first: permission then budget (budget outermost),
        # matching the old _inference (perm+budget) / _procedure (budget) / work-gate
        # (perm only) wirings exactly.
        if m.permission is not None:
            act = self.factory.governor("permission", act, allow=tuple(m.permission))
        if m.budget is not None:
            cap = int(self.budgets[m.budget])
            estimate = _MOCK_USAGE if m.kind == "inference" else 0
            act = self.factory.governor("budget", act, cap=cap, estimate=estimate, phase=m.budget)
        return act

    def _build_proxy(self, m: Any) -> Any:
        """Compile a ``kind == "proxy"`` manifest: build the wrapped target action
        (with its own governors) and wrap it in a Proxy that reroutes the target's
        shelf I/O per the rewire maps (the target's default I/O locations are read
        from its own interface; the rewire names the NEW source/sink)."""
        target = m.proxy_target
        inner = self._build_action(target)
        in_by_alias = {r.alias: r for r in target.inputs}
        out_by_alias = {r.alias: r for r in target.outputs}
        # pre: NEW source -> the target's default input location (before it runs).
        pre = [(r.shelf, r.key, in_by_alias[r.alias].shelf, in_by_alias[r.alias].key) for r in m.rewire_in]
        # post: the target's default output location -> NEW sink (after it runs).
        post = [(out_by_alias[r.alias].shelf, out_by_alias[r.alias].key, r.shelf, r.key) for r in m.rewire_out]
        return self.factory.proxy(f"proxy:{target.token}", inner, pre=pre, post=post)

    def _interface_body(self, manifest: Any, binding: Any) -> Callable[[Any, Any], Any]:
        fn = binding.fn
        if binding.needs_factory:
            real = fn(self.factory)  # builder -> (payload, ctx) -> value|Result; self-manages I/O

            def raw_body(payload: Any, ctx: Any) -> Any:
                return real(payload, ctx)

            return raw_body

        def body(payload: Any, ctx: Any) -> Any:
            inputs = _read_inputs(manifest, ctx)
            out = fn(inputs, ctx) if binding.needs_ctx else fn(inputs)
            if not manifest.raw:
                _write_outputs(manifest, ctx, out)
            return out

        return body


# -- validate ---------------------------------------------------------------
@dataclass
class ValidationError:
    """One located workflow validation failure."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class ValidateVisitor(WorkflowVisitor):
    """Collect structural, token, budget, data-flow and needs errors.
    ``registry`` may be ``None`` to skip bind/predicate/controller checks
    (structure + data-flow only). ``services`` (a ``ServiceRegistry``) arms the
    needs-satisfaction check: with it, every non-optional ``service:``/
    ``config:`` need must be met by the environment's provisions (ADR-003);
    declared ``input:`` needs are always checked against the workflow's own
    inputs/seed and upstream productions."""

    def __init__(self, registry: Optional[Any], budgets, inputs, seed, *, services: Any = None) -> None:
        self.registry = registry
        self.budgets = set(budgets or ())
        self.errors: List[ValidationError] = []
        self.available = set()
        for k in (inputs or ()):
            self.available.add(f"input.{k}")
        for k in (seed or {}):
            self.available.add(f"input.{k}")
        self.services = services
        provisions = list(input_provisions(inputs or (), dict(seed or {})))
        if services is not None:
            provisions.extend(services.provisions())
        self._provision_keys = {p.key for p in provisions}
        # An unresolvable controller ref (no registry) hides its productions, so
        # downstream missing-input findings would be false — flow goes "open".
        self.open_flow = False
        self._validated_controllers: Set[str] = set()
        self._expanding: Set[str] = set()

    def _err(self, path: str, message: str) -> None:
        self.errors.append(ValidationError(path, message))

    def _check_needs(self, needs: Iterable[Any], path: str) -> None:
        """Check *declared* needs (derived input needs stay the data-flow
        check's job — no double report)."""
        for n in needs or ():
            if getattr(n, "optional", False):
                continue
            kind, key = getattr(n, "kind", ""), getattr(n, "key", "")
            if kind == KIND_INPUT:
                if key not in self._provision_keys and f"input.{getattr(n, 'name', '')}" not in self.available:
                    self._err(path, f"unmet need {key!r}: not seeded, declared or produced upstream")
            elif self.services is not None and key not in self._provision_keys:
                self._err(
                    path,
                    f"unmet need {key!r}: not provided "
                    f"(services: {', '.join(self.services.ids()) or 'none'})",
                )

    def visit_workflow(self, node: Any) -> List[ValidationError]:
        if not node.phases:
            self._err(node.name, "workflow has no phases")
        tw = getattr(node, "terminal_when", None)
        if tw and self.registry is not None:
            try:
                ast = parse_predicate(tw)
            except PredicateError as exc:
                self._err(f"{node.name}.terminal_when", f"bad predicate {tw!r}: {exc}")
            else:
                pv = PredicateValidator(self.registry)
                pv.visit(ast)
                for nm in pv.errors:
                    self._err(f"{node.name}.terminal_when", f"unknown predicate {nm!r}")
        seen = set()
        for p in node.phases:
            if p.name in seen:
                self._err(node.name, f"duplicate phase {p.name!r}")
            seen.add(p.name)
            self.visit(p)
        # Declared-only controllers (dynamic supersede targets, never in a
        # phase) are still parties to the contract: resolve + needs-check them
        # against the post-phase data-flow state (their spawn-time environment).
        for cname in (getattr(node, "controllers", ()) or ()):
            if cname in self._validated_controllers or self.registry is None:
                continue
            self.visit(ControllerRefNode(name=cname, manifests=dict(getattr(node, "manifests", {}) or {})))
        return self.errors

    def visit_phase(self, node: Any) -> None:
        if not node.steps:
            self._err(node.name, "phase has no steps")
        for s in node.steps:
            self.visit(s)

    def visit_sequence(self, node: Any) -> None:
        for s in node.steps:
            self.visit(s)

    def visit_parallel(self, node: Any) -> None:
        snapshot = set(self.available)
        produced = set()
        for b in node.branches:
            self.available = set(snapshot)
            self.visit(b)
            produced |= self.available - snapshot
        self.available = snapshot | produced

    def visit_loop(self, node: Any) -> None:
        if node.body is None:
            self._err(node.name, "loop has no body")
        else:
            self.visit(node.body)
        for expr, label in ((node.until, "until"), (node.abort_when, "abort_when"), (node.while_, "while")):
            if not expr:
                continue
            try:
                ast = parse_predicate(expr)
            except PredicateError as exc:
                self._err(f"{node.name}.{label}", f"bad predicate {expr!r}: {exc}")
                continue
            if self.registry is not None:
                pv = PredicateValidator(self.registry)
                pv.visit(ast)
                for nm in pv.errors:
                    self._err(f"{node.name}.{label}", f"unknown predicate {nm!r}")

    def visit_action_ref(self, node: Any) -> None:
        m = node.manifest
        if self.registry is not None and not self.registry.has_action(m.bind):
            self._err(m.token, f"bind {m.bind!r} not registered (known: {sorted(self.registry.action_binds())})")
        if m.budget is not None and self.budgets and m.budget not in self.budgets:
            self._err(m.token, f"unknown budget bucket {m.budget!r} (defined: {sorted(self.budgets)})")
        self._check_needs(m.needs, m.token)
        for ref in m.inputs:
            if ref.ref not in self.available and not self.open_flow:
                self._err(m.token, f"input {ref.alias!r} reads {ref.ref!r}, not seeded or produced upstream")
        for ref in m.outputs:
            self.available.add(ref.ref)

    def visit_controller_ref(self, node: Any) -> None:
        if self.registry is None:
            self.open_flow = True  # cannot expand: its productions are unknown
            return
        if not self.registry.has_controller(node.name):
            self._err(
                node.name,
                f"controller {node.name!r} not registered "
                f"(known: {sorted(self.registry.controller_names())})",
            )
            return
        if node.name in self._expanding:
            self._err(node.name, "cyclic controller reference")
            return
        self._validated_controllers.add(node.name)
        self._check_needs(self.registry.controller_spec(node.name).needs, node.name)
        self._expanding.add(node.name)
        try:
            try:
                expanded = expand_controller(node, self.registry)
            except SchemaError as exc:
                self._err(exc.path, exc.message)
                return
            self.visit(expanded)
        finally:
            self._expanding.discard(node.name)


# -- render -----------------------------------------------------------------
class RenderVisitor(WorkflowVisitor):
    """Serialize the node tree back to a plain dict (inverse of the loader)."""

    def visit_workflow(self, node: Any) -> Dict[str, Any]:
        d = {
            "name": node.name,
            "inputs": list(node.inputs),
            "seed": dict(node.seed),
            "budgets": dict(node.budgets),
            "admin_spec_split": node.admin_spec_split,
            "phases": {p.name: [self.visit(s) for s in p.steps] for p in node.phases},
        }
        if getattr(node, "controllers", ()):
            d["controllers"] = list(node.controllers)
        return d

    def visit_phase(self, node: Any) -> Dict[str, Any]:
        return {node.name: [self.visit(s) for s in node.steps]}

    def visit_sequence(self, node: Any) -> Dict[str, Any]:
        return {"sequence": {"name": node.name, "steps": [self.visit(s) for s in node.steps]}}

    def visit_parallel(self, node: Any) -> Dict[str, Any]:
        return {"parallel": {"name": node.name, "branches": [self.visit(b) for b in node.branches]}}

    def visit_loop(self, node: Any) -> Dict[str, Any]:
        d: Dict[str, Any] = {"name": node.name, "body": self.visit(node.body), "max_iterations": node.max_iterations}
        if node.until:
            d["until"] = node.until
        if node.abort_when:
            d["abort_when"] = node.abort_when
        if node.while_:
            d["while"] = node.while_
        return {"loop": d}

    def visit_action_ref(self, node: Any) -> str:
        return node.token

    def visit_controller_ref(self, node: Any) -> Dict[str, Any]:
        return {"controller": node.name}


# -- needs report -------------------------------------------------------------
def _collect_refs(node: Any, ctrls: "List[str]", acts: "List[Any]") -> None:
    if isinstance(node, ControllerRefNode):
        if node.name not in ctrls:
            ctrls.append(node.name)
        return
    if isinstance(node, ActionRefNode):
        acts.append(node.manifest)
        return
    for child in (
        list(getattr(node, "phases", ()) or ())
        + list(getattr(node, "steps", ()) or ())
        + list(getattr(node, "branches", ()) or ())
        + ([node.body] if getattr(node, "body", None) is not None else [])
    ):
        _collect_refs(child, ctrls, acts)


def needs_report(node: Any, registry: Optional[Any] = None, services: Any = None) -> Dict[str, Any]:
    """The introspection half of ADR-003's static validation: per-controller and
    per-action needs (union semantics, optional flags), each annotated with the
    provision that satisfies it (or ``None`` — unmet against the given
    environment). ``satisfied`` reports contract satisfaction over the
    ``service:``/``config:`` kinds when a ``ServiceRegistry`` is supplied
    (``input:`` needs belong to the data-flow check); ``None`` means unchecked."""
    provisions: List[Provision] = list(input_provisions(node.inputs, node.seed))
    if services is not None:
        provisions.extend(services.provisions())
    by_key = {}
    for p in provisions:
        by_key.setdefault(p.key, p)

    def annotate(ns: Iterable[Need]) -> List[Dict[str, Any]]:
        out = []
        for n in ns:
            p = by_key.get(n.key)
            out.append({"need": n.key, "optional": n.optional, "satisfied_by": (p.source if p else None)})
        return out

    ctrl_names: List[str] = []
    manifests: List[Any] = []
    _collect_refs(node, ctrl_names, manifests)
    for cname in (getattr(node, "controllers", ()) or ()):
        if cname not in ctrl_names:
            ctrl_names.append(cname)

    report: Dict[str, Any] = {"workflow": node.name, "controllers": {}, "actions": {}, "unmet": []}
    checked = services is not None
    for cname in ctrl_names:
        if registry is None or not registry.has_controller(cname):
            report["controllers"][cname] = {"registered": False, "needs": []}
            continue
        ns = controller_needs(cname, registry=registry, manifests=dict(getattr(node, "manifests", {}) or {}))
        report["controllers"][cname] = {"registered": True, "needs": annotate(ns)}
        if checked:
            report["unmet"].extend(
                n.key for n in ns.required
                if n.kind != KIND_INPUT and n.key not in by_key and n.key not in report["unmet"]
            )
    for m in manifests:
        ns = NeedSet(m.effective_needs)
        if ns:
            report["actions"][m.token] = annotate(ns)
        if checked:
            report["unmet"].extend(
                n.key for n in ns.required
                if n.kind != KIND_INPUT and n.key not in by_key and n.key not in report["unmet"]
            )
    report["satisfied"] = (not report["unmet"]) if checked else None
    report["provisions"] = [str(p) for p in provisions]
    return report
