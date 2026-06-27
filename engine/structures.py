"""structures.py — generic data structures for the engine (stdlib-only leaf).

Reusable mechanism with **no** pipeline/domain knowledge:

  * ``Node`` / ``LinkedList`` — a singly linked list; the FSM's canonical state
    order (the lifecycle "ladder") rides one.
  * ``Stack``               — LIFO; the FSM's visited-history rides one, so a
    transition can be undone (``back``).
  * ``State`` / ``StateMachine`` — a finite state machine: named states carrying
    arbitrary metadata, directed event-labelled transitions, a current pointer,
    and a history stack.
  * ``StateMachineBuilder`` — a creational **Builder**: assemble a StateMachine
    step by step (or in one shot from a parsed mapping), then ``build()``.
  * ``Graph`` / ``GraphBuilder`` — a directed graph (deps, longest-chain depth,
    roots, cycle participants), assembled by a Builder.
  * Issue dependency DAG (``IssueGraph`` + ``build`` + renderers) — a thin domain
    layer over ``Graph`` that parses GitHub issue ``#<number>`` cross-references
    and renders the result (JSON / Mermaid / Markdown table).

What states/nodes exist, their metadata, and the legal transitions/edges are
**not** baked in — callers feed them in via the Builders (e.g. parsed out of a
YAML config or issue bodies). That keeps the *mechanism* in the engine and the
*state* in configuration.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional


class Node:
    """A singly linked list node."""

    __slots__ = ("value", "next")

    def __init__(self, value: Any, nxt: "Optional[Node]" = None) -> None:
        self.value = value
        self.next = nxt


class LinkedList:
    """Minimal singly linked list: append + ordered iteration + membership."""

    def __init__(self, items: Optional[Any] = None) -> None:
        self._head: Optional[Node] = None
        self._tail: Optional[Node] = None
        self._len = 0
        for it in (items or []):
            self.append(it)

    def append(self, value: Any) -> "LinkedList":
        node = Node(value)
        if self._head is None:
            self._head = self._tail = node
        else:
            self._tail.next = node  # type: ignore[union-attr]
            self._tail = node
        self._len += 1
        return self

    def __iter__(self) -> Iterator[Any]:
        cur = self._head
        while cur is not None:
            yield cur.value
            cur = cur.next

    def __len__(self) -> int:
        return self._len

    def __contains__(self, value: Any) -> bool:
        return any(v == value for v in self)

    def to_list(self) -> List[Any]:
        return list(self)


class Stack:
    """A LIFO stack."""

    def __init__(self, items: Optional[Any] = None) -> None:
        self._items: List[Any] = list(items or [])

    def push(self, value: Any) -> "Stack":
        self._items.append(value)
        return self

    def pop(self) -> Any:
        if not self._items:
            raise IndexError("pop from empty stack")
        return self._items.pop()

    def peek(self) -> Any:
        return self._items[-1] if self._items else None

    def is_empty(self) -> bool:
        return not self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Any]:
        return reversed(self._items)  # top first

    def to_list(self) -> List[Any]:
        return list(self._items)


class State:
    """An FSM state: a name, free-form metadata, and outgoing event edges."""

    __slots__ = ("name", "meta", "edges")

    def __init__(self, name: str, meta: Optional[Dict[str, Any]] = None) -> None:
        self.name = name
        self.meta: Dict[str, Any] = dict(meta or {})
        self.edges: Dict[str, str] = {}  # event -> target state name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"State({self.name!r})"


class StateMachine:
    """A finite state machine.

    Canonical states live in a :class:`LinkedList` (insertion order = the
    declared ladder); visited-from states live in a :class:`Stack` so a
    transition can be undone. ``fire`` only follows declared edges.
    """

    def __init__(self, states: LinkedList, initial: str) -> None:
        self._order = states  # LinkedList[State], declared order
        self._states: Dict[str, State] = {s.name: s for s in states}
        if initial not in self._states:
            raise ValueError(f"unknown initial state: {initial!r}")
        self._initial = initial
        self._current = initial
        self._history = Stack()

    @property
    def current(self) -> str:
        return self._current

    @property
    def history(self) -> Stack:
        return self._history

    def state(self, name: str) -> State:
        return self._states[name]

    def names(self) -> List[str]:
        """All state names, in declared order."""
        return [s.name for s in self._order]

    def states_where(self, **meta: Any) -> List[str]:
        """State names whose metadata matches every key=value, in declared order."""
        return [s.name for s in self._order
                if all(s.meta.get(k) == v for k, v in meta.items())]

    def can(self, event: str) -> bool:
        return event in self._states[self._current].edges

    def fire(self, event: str) -> str:
        edges = self._states[self._current].edges
        if event not in edges:
            raise ValueError(f"no transition {event!r} from state {self._current!r}")
        self._history.push(self._current)
        self._current = edges[event]
        return self._current

    def back(self) -> str:
        """Undo the last transition (pop the history stack)."""
        if self._history.is_empty():
            raise IndexError("no transition to undo")
        self._current = self._history.pop()
        return self._current

    def reset(self) -> str:
        self._current = self._initial
        self._history = Stack()
        return self._current


class StateMachineBuilder:
    """Creational Builder for :class:`StateMachine`.

    Fluent and incremental::

        sm = (StateMachineBuilder()
              .state("queued", kind="state")
              .state("claimed", kind="state")
              .transition("queued", "claim", "claimed")
              .initial("queued")
              .build())

    or in one shot from a parsed mapping (e.g. YAML)::

        sm = StateMachineBuilder().from_mapping(cfg).build()
    """

    def __init__(self) -> None:
        self._states = LinkedList()
        self._seen: set = set()
        self._transitions: List[tuple] = []  # (from, event, to)
        self._initial: Optional[str] = None

    def state(self, name: str, **meta: Any) -> "StateMachineBuilder":
        if name in self._seen:
            raise ValueError(f"duplicate state: {name!r}")
        self._seen.add(name)
        self._states.append(State(name, meta))
        if self._initial is None:
            self._initial = name  # first declared state is the default initial
        return self

    def transition(self, frm: str, event: str, to: str) -> "StateMachineBuilder":
        self._transitions.append((frm, event, to))
        return self

    def initial(self, name: str) -> "StateMachineBuilder":
        self._initial = name
        return self

    def from_mapping(self, cfg: Dict[str, Any]) -> "StateMachineBuilder":
        """Ingest ``{initial, states: {name: meta}, transitions: [{from,event,to}]}``."""
        for name, meta in (cfg.get("states") or {}).items():
            self.state(name, **(meta or {}))
        for t in (cfg.get("transitions") or []):
            self.transition(t["from"], t["event"], t["to"])
        if cfg.get("initial"):
            self.initial(cfg["initial"])
        return self

    def build(self) -> StateMachine:
        if self._initial is None:
            raise ValueError("state machine has no states / initial state")
        by_name = {s.name: s for s in self._states}
        for frm, event, to in self._transitions:
            if frm not in by_name:
                raise ValueError(f"transition from unknown state: {frm!r}")
            if to not in by_name:
                raise ValueError(f"transition to unknown state: {to!r}")
            by_name[frm].edges[event] = to
        return StateMachine(self._states, self._initial)


class Graph:
    """An immutable directed graph. Edge ``a -> b`` reads "a depends on b".

    Domain-agnostic: node ids are any hashable, metadata is free-form. Derives
    (in node-insertion order):

      * ``deps``   — id -> sorted list of ids it depends on
      * ``depth``  — id -> longest dependency-chain length (cycle-guarded)
      * ``roots``  — ids with no dependencies
      * ``cycles`` — ids that participate in a dependency cycle

    Build one with :class:`GraphBuilder`; callers add domain meaning (e.g. issue
    numbers + titles) on top.
    """

    def __init__(self, nodes: List[Any], deps: Dict[Any, Any],
                 meta: Dict[Any, Dict[str, Any]]) -> None:
        self.nodes = list(nodes)
        self.deps: Dict[Any, list] = {n: sorted(deps[n]) for n in self.nodes}
        self.meta = meta
        self.depth = self._depths()
        self.roots = [n for n in self.nodes if not self.deps[n]]
        self.cycles = [n for n in self.nodes if self._on_cycle(n)]

    def _depths(self) -> Dict[Any, int]:
        cache: Dict[Any, int] = {}

        def depth(n: Any, stack: frozenset) -> int:
            if n in cache:
                return cache[n]
            if n in stack:  # cycle guard (bounds recursion)
                return 0
            d = 0
            for p in self.deps[n]:
                d = max(d, 1 + depth(p, stack | {n}))
            cache[n] = d
            return d

        return {n: depth(n, frozenset()) for n in self.nodes}

    def _on_cycle(self, start: Any) -> bool:
        # `start` is on a cycle iff reachable from itself via >=1 edge. Computed
        # independently of depth()'s memoization (which masks all but the first
        # node of a cycle).
        seen: set = set()
        stack = list(self.deps[start])
        while stack:
            x = stack.pop()
            if x == start:
                return True
            if x in seen:
                continue
            seen.add(x)
            stack.extend(self.deps[x])
        return False


class GraphBuilder:
    """Creational Builder for :class:`Graph`.

    Node insertion order is preserved (it rides a :class:`LinkedList`), so add
    nodes in the order you want ``nodes``/``roots``/``cycles`` reported::

        g = (GraphBuilder()
             .node(1, title="root").node(2, title="leaf")
             .edge(2, 1)          # #2 depends on #1
             .build())
    """

    def __init__(self) -> None:
        self._order = LinkedList()
        self._nodes: set = set()
        self._meta: Dict[Any, Dict[str, Any]] = {}
        self._deps: Dict[Any, set] = {}

    def node(self, nid: Any, **meta: Any) -> "GraphBuilder":
        if nid not in self._nodes:
            self._nodes.add(nid)
            self._order.append(nid)
            self._deps[nid] = set()
        if meta:
            self._meta.setdefault(nid, {}).update(meta)
        return self

    def edge(self, frm: Any, to: Any) -> "GraphBuilder":
        """Record "frm depends on to". Ignored unless both are known, distinct nodes."""
        if frm in self._nodes and to in self._nodes and frm != to:
            self._deps[frm].add(to)
        return self

    def build(self) -> Graph:
        nodes = list(self._order)
        meta = {n: self._meta.get(n, {}) for n in nodes}
        return Graph(nodes, self._deps, meta)


# ===========================================================================
# Issue dependency DAG — a thin DOMAIN layer over Graph (was engine/dag.py).
#
# The single source of truth for "which issue blocks which": the offline ranker
# builds it to order the queue foundational -> dependent; `dispatch --dag`
# persists it as a static artifact. Construction is deterministic and offline:
# identical (items, patterns) yield an identical graph. Issue text is parsed as
# DATA — only `#<number>` cross-references are read.
#
# Edge convention: `deps[n]` is the issues #n depends on. In JSON an edge
# `[a, b]` reads "#a depends on #b"; in Mermaid an arrow `B --> A` reads "#B is
# foundational to #A". Roots have no dependencies.
# ===========================================================================
@dataclass(frozen=True)
class IssueGraph:
    numbers: List[int]            # all issue numbers, ascending
    titles: Dict[int, str]        # number -> title
    deps: Dict[int, List[int]]    # number -> sorted list it depends on
    depth: Dict[int, int]         # number -> longest dependency chain length
    roots: List[int]              # numbers with no dependencies, ascending
    cycles: List[int]             # numbers participating in a dependency cycle


_MD_EMPHASIS = re.compile(r"[*_`]+")


def _strip_md(text: str) -> str:
    """Drop markdown emphasis/code runs (``*``, ``_``, `````) so cross-reference
    patterns match real GitHub bodies, e.g. ``**Parent epic:** #1``. Only the
    formatting characters are removed; ``#<number>`` references are untouched."""
    return _MD_EMPHASIS.sub("", text)


def _refs(text: str, patterns) -> set:
    out: set = set()
    text = _strip_md(text)
    for pat in patterns:
        # MULTILINE so line-anchored patterns (e.g. task-list items) match each
        # body line; IGNORECASE for prose phrasing. Existing patterns use no
        # ^/$ anchor, so MULTILINE leaves their matches unchanged.
        for m in re.finditer(pat, text, re.IGNORECASE | re.MULTILINE):
            out.add(int(m.group(1)))
    return out


def build(items: List[Dict[str, Any]], *, fwd, rev) -> IssueGraph:
    """Build the issue dependency graph from intake items.

    `fwd` patterns capture "this issue depends on #N"; `rev` patterns capture
    "this issue unblocks/blocks #N" (=> #N depends on this issue). Each pattern
    has a single `(\\d+)` group. Markdown emphasis is stripped before matching
    (see `_refs`), so references written as `**Parent epic:** #N` are read.
    """
    # Parse issue cross-references (the DOMAIN layer), then hand the nodes/edges
    # to the generic GraphBuilder (the MECHANISM) for the depth / roots / cycle
    # computation. Nodes are added ascending so numbers/roots/cycles come back
    # ascending — the order callers and renderers expect.
    numbers = sorted({int(i["number"]) for i in items})
    builder = GraphBuilder()
    for n in numbers:
        builder.node(n)
    for item in items:
        n = int(item["number"])
        text = f"{item.get('title', '')}\n{item.get('body', '')}"
        for r in _refs(text, fwd):   # fwd: this issue depends on #r
            builder.edge(n, r)
        for r in _refs(text, rev):   # rev: #r depends on this issue
            builder.edge(r, n)
    g = builder.build()             # generic graph: deps (sorted), depth, roots, cycles

    titles = {int(i["number"]): (i.get("title") or "") for i in items}
    return IssueGraph(g.nodes, titles, g.deps, g.depth, g.roots, g.cycles)


# --------------------------------------------------------------------------
# Renderers — all deterministic, no timestamps.
# --------------------------------------------------------------------------
def to_json(graph: IssueGraph, *, source: str = "") -> Dict[str, Any]:
    """Canonical machine view: nodes + edges + roots. The --issue selector and
    any external tooling read this."""
    return {
        "source": source,
        "roots": list(graph.roots),
        "cycles": list(graph.cycles),
        "nodes": [
            {
                "number": n,
                "title": graph.titles.get(n, ""),
                "depth": graph.depth[n],
                "depends_on": list(graph.deps[n]),
                "root": n in graph.roots,
            }
            for n in graph.numbers
        ],
        # edge [a, b] reads "#a depends on #b"
        "edges": [[n, d] for n in graph.numbers for d in graph.deps[n]],
    }


def _mermaid_label(n: int, title: str, width: int = 48) -> str:
    # Issue title is UNTRUSTED data. Neutralize every character that could break
    # out of the `["..."]` label or the surrounding ```mermaid fence: quotes and
    # brackets (label breakout), backticks (fence breakout -> Markdown injection
    # into the persisted artifact), and angle brackets (raw-HTML injection).
    # Whitespace is collapsed, which also blocks newline-based statement injection.
    t = " ".join(title.split())
    t = (t.replace('"', "'").replace("[", "(").replace("]", ")")
          .replace("`", "'").replace("<", "(").replace(">", ")"))
    if len(t) > width:
        t = t[: width - 1].rstrip() + "…"
    return f'N{n}["#{n} — {t}"]' if t else f'N{n}["#{n}"]'


def render_mermaid(graph: IssueGraph) -> str:
    """A ```mermaid graph TD``` block (renders visually on GitHub)."""
    lines = ["```mermaid", "graph TD",
             "  %% B --> A: #B is foundational to #A (do B first). Roots are highlighted."]
    for n in graph.numbers:
        lines.append("  " + _mermaid_label(n, graph.titles.get(n, "")))
    for n in graph.numbers:
        for d in graph.deps[n]:
            lines.append(f"  N{d} --> N{n}")
    if graph.roots:
        lines.append("  classDef root fill:#d4f4dd,stroke:#2e7d32,color:#1b5e20;")
        lines.append("  class " + ",".join(f"N{n}" for n in graph.roots) + " root;")
    lines.append("```")
    return "\n".join(lines)


def render_markdown_table(graph: IssueGraph, *,
                          eligibility: Optional[Dict[int, str]] = None) -> str:
    """A sortable Markdown table: Issue | Title | Depth | Depends on | Root [| Eligible?]."""
    has_e = eligibility is not None
    head = "| Issue | Title | Depth | Depends on | Root |" + (" Eligible? |" if has_e else "")
    sep = "|---|---|---|---|---|" + ("---|" if has_e else "")
    rows = [head, sep]
    for n in graph.numbers:
        title = " ".join(graph.titles.get(n, "").split()).replace("|", "\\|")
        dep = ", ".join(f"#{d}" for d in graph.deps[n]) or "—"
        root = "✓" if n in graph.roots else ""
        row = f"| #{n} | {title} | {graph.depth[n]} | {dep} | {root} |"
        if has_e:
            row += f" {eligibility.get(n, '')} |"
        rows.append(row)
    return "\n".join(rows)
