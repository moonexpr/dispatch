#!/usr/bin/env python3
"""predicate.py — the loop-predicate mini-language, parsed with the visitor pattern.

A Loop's ``until`` / ``abort_when`` / ``while`` are not Python lambdas (end users
write no Python) and not ``eval``'d strings (no arbitrary execution). They are a
tiny boolean expression grammar over *named predicate tokens* that ``src`` binds
in the registry — Valve's ``WaitForAllDead "wave06ab"`` style, with composition:

    until: engineering_succeeded
    abort_when: not engineering_succeeded and budget_exhausted

Grammar (precedence low→high): ``or`` < ``and`` < ``not`` < atom; an atom is a
predicate name or a parenthesised expression. Parsing is two stages:

  1. tokenize + recursive-descent parse → a predicate AST (``Ref``/``Not``/
     ``And``/``Or`` Elements, each with ``accept``).
  2. a ``PredicateVisitor`` walks the AST: ``PredicateCompiler`` resolves each
     ``Ref`` through the registry and folds the tree into one ``(result, ctx) ->
     bool`` callable; ``PredicateValidator`` collects unknown ``Ref`` names.

This is the "visitor pattern for predicate parsing"; the source of advanced loop
expression without code or ``eval``.

Leaf module: stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, List


class PredicateError(Exception):
    """A malformed predicate expression."""


# -- AST (Elements) ---------------------------------------------------------
class PNode:
    def accept(self, visitor: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True)
class Ref(PNode):
    name: str

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_ref(self)


@dataclass(frozen=True)
class Not(PNode):
    operand: PNode

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_not(self)


@dataclass(frozen=True)
class And(PNode):
    left: PNode
    right: PNode

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_and(self)


@dataclass(frozen=True)
class Or(PNode):
    left: PNode
    right: PNode

    def accept(self, visitor: Any) -> Any:
        return visitor.visit_or(self)


# -- tokenizer + parser -----------------------------------------------------
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_:.\-]*")
_KEYWORDS = ("and", "or", "not")


def _tokenize(expr: str) -> List[str]:
    toks: List[str] = []
    i, n = 0, len(expr)
    while i < n:
        c = expr[i]
        if c.isspace():
            i += 1
            continue
        if c in "()":
            toks.append(c)
            i += 1
            continue
        m = _WORD.match(expr, i)
        if not m:
            raise PredicateError(f"unexpected character {c!r} in predicate {expr!r}")
        toks.append(m.group(0))
        i = m.end()
    return toks


class _Parser:
    def __init__(self, toks: List[str]) -> None:
        self.toks = toks
        self.i = 0

    def _peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _next(self):
        t = self._peek()
        self.i += 1
        return t

    def parse(self) -> PNode:
        node = self._parse_or()
        if self.i != len(self.toks):
            raise PredicateError(f"trailing tokens in predicate: {self.toks[self.i:]}")
        return node

    def _parse_or(self) -> PNode:
        left = self._parse_and()
        while self._peek() == "or":
            self._next()
            left = Or(left, self._parse_and())
        return left

    def _parse_and(self) -> PNode:
        left = self._parse_not()
        while self._peek() == "and":
            self._next()
            left = And(left, self._parse_not())
        return left

    def _parse_not(self) -> PNode:
        if self._peek() == "not":
            self._next()
            return Not(self._parse_not())
        return self._parse_atom()

    def _parse_atom(self) -> PNode:
        t = self._peek()
        if t == "(":
            self._next()
            node = self._parse_or()
            if self._peek() != ")":
                raise PredicateError("missing ')' in predicate")
            self._next()
            return node
        if t is None or t in _KEYWORDS or t == ")":
            raise PredicateError(f"expected a predicate name, got {t!r}")
        self._next()
        return Ref(t)


def parse_predicate(expr: str) -> PNode:
    """Parse a predicate expression string into a predicate AST."""
    toks = _tokenize(expr)
    if not toks:
        raise PredicateError("empty predicate expression")
    return _Parser(toks).parse()


# -- visitors ---------------------------------------------------------------
class PredicateVisitor:
    """Base visitor over the predicate AST."""

    def visit(self, node: PNode) -> Any:
        return node.accept(self)

    def visit_ref(self, node: Ref) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def visit_not(self, node: Not) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def visit_and(self, node: And) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def visit_or(self, node: Or) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


class PredicateCompiler(PredicateVisitor):
    """Fold the AST into one ``(result, ctx) -> bool`` callable, resolving each
    leaf ``Ref`` to its registered predicate function."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def visit_ref(self, node: Ref) -> Callable[[Any, Any], bool]:
        fn = self.registry.predicate_fn(node.name)
        return lambda result, ctx: bool(fn(result, ctx))

    def visit_not(self, node: Not) -> Callable[[Any, Any], bool]:
        inner = self.visit(node.operand)
        return lambda result, ctx: not inner(result, ctx)

    def visit_and(self, node: And) -> Callable[[Any, Any], bool]:
        left = self.visit(node.left)
        right = self.visit(node.right)
        return lambda result, ctx: left(result, ctx) and right(result, ctx)

    def visit_or(self, node: Or) -> Callable[[Any, Any], bool]:
        left = self.visit(node.left)
        right = self.visit(node.right)
        return lambda result, ctx: left(result, ctx) or right(result, ctx)


class PredicateValidator(PredicateVisitor):
    """Collect the names of any ``Ref`` not registered as a predicate."""

    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self.errors: List[str] = []

    def visit_ref(self, node: Ref) -> None:
        if not self.registry.has_predicate(node.name):
            self.errors.append(node.name)

    def visit_not(self, node: Not) -> None:
        self.visit(node.operand)

    def visit_and(self, node: And) -> None:
        self.visit(node.left)
        self.visit(node.right)

    def visit_or(self, node: Or) -> None:
        self.visit(node.left)
        self.visit(node.right)


def compile_predicate(expr: str, registry: Any) -> Callable[[Any, Any], bool]:
    """Parse + compile a predicate expression into a ``(result, ctx) -> bool``."""
    return PredicateCompiler(registry).visit(parse_predicate(expr))
