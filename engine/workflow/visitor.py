#!/usr/bin/env python3
"""visitor.py — the operations over the workflow node tree (the Visitors).

Each visitor is one operation; the node classes never change as operations are
added. Three concrete visitors:

  * ``CompileVisitor`` — node tree → an ``engine.actions`` statechart. The crux is
    the **interface wrapper** built in ``visit_action_ref``: a ``(payload, ctx) ->
    value`` closure that reads the action's declared ``interface.in`` from the
    shelves into an ``inputs`` dict, calls the registered body, and writes the
    body's returned dict back to ``interface.out`` — so the manifest's I/O is the
    *runtime* contract, and the bodies stay pure (no shelf calls). The wrapped
    body is handed to the factory exactly as the old ``_inference``/``_procedure``
    did, then wrapped in the same governors (permission inner, budget outer).
  * ``ValidateVisitor`` — node tree → a list of ``ValidationError`` (the YAML
    "format test"): unknown bind/budget/predicate, structural problems, AND the
    data-flow check (every ``in`` is seeded or produced upstream).
  * ``RenderVisitor`` — node tree → a plain dict (the inverse of the loader; used
    for a serialization round-trip).

Leaf module: stdlib + ``engine.actions`` + sibling workflow leaves.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from engine.actions import InferenceSpec, Loop, Parallel, Sequence

from .controller import CompiledWorkflow
from .predicate import PredicateError, PredicateValidator, compile_predicate, parse_predicate

# Per-inference deterministic mock cost + default route — preserve the old
# baseworkflow values so the budget meters and spend are identical.
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
    """Compile the node tree into an ``engine.actions`` controller + statechart."""

    def __init__(self, factory: Any, registry: Any) -> None:
        self.factory = factory
        self.registry = registry
        self.budgets: Dict[str, int] = {}
        self.terminal: Optional[Callable[[Any, Any], bool]] = None

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
    """Collect structural, token, budget and data-flow errors. ``registry`` may be
    ``None`` to skip bind/predicate checks (structure + data-flow only)."""

    def __init__(self, registry: Optional[Any], budgets, inputs, seed) -> None:
        self.registry = registry
        self.budgets = set(budgets or ())
        self.errors: List[ValidationError] = []
        self.available = set()
        for k in (inputs or ()):
            self.available.add(f"input.{k}")
        for k in (seed or {}):
            self.available.add(f"input.{k}")

    def _err(self, path: str, message: str) -> None:
        self.errors.append(ValidationError(path, message))

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
        for ref in m.inputs:
            if ref.ref not in self.available:
                self._err(m.token, f"input {ref.alias!r} reads {ref.ref!r}, not seeded or produced upstream")
        for ref in m.outputs:
            self.available.add(ref.ref)


# -- render -----------------------------------------------------------------
class RenderVisitor(WorkflowVisitor):
    """Serialize the node tree back to a plain dict (inverse of the loader)."""

    def visit_workflow(self, node: Any) -> Dict[str, Any]:
        return {
            "name": node.name,
            "inputs": list(node.inputs),
            "seed": dict(node.seed),
            "budgets": dict(node.budgets),
            "admin_spec_split": node.admin_spec_split,
            "phases": {p.name: [self.visit(s) for s in p.steps] for p in node.phases},
        }

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
