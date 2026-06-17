#!/usr/bin/env python3
"""dag.py — the issue dependency graph: build it once, render it three ways.

This is the single source of truth for "which issue blocks which". The offline
ranker (ranker.py) builds it to order the queue foundational -> dependent; the
dispatch program (dispatch.py --dag) builds the same graph and persists it as a
*static* artifact an admin can browse to pick an issue — without resolving the
ordering inside a live conversation.

Construction is deterministic and offline: identical (items, patterns) always
yield an identical graph and identical renderings (no wall-clock, sorted output).
Issue text is parsed as DATA — only `#<number>` cross-references are read.

Edge convention
---------------
`deps[n]` is the set of issues #n depends on. In the JSON an edge `[a, b]` reads
"#a depends on #b". In the Mermaid graph an arrow `B --> A` reads "#B is
foundational to #A" (precedence: do B before A). Roots have no dependencies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Graph:
    numbers: List[int]            # all issue numbers, ascending
    titles: Dict[int, str]        # number -> title
    deps: Dict[int, List[int]]    # number -> sorted list it depends on
    depth: Dict[int, int]         # number -> longest dependency chain length
    roots: List[int]              # numbers with no dependencies, ascending
    cycles: List[int]             # numbers participating in a dependency cycle


def _refs(text: str, patterns) -> set:
    out: set = set()
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            out.add(int(m.group(1)))
    return out


def build(items: List[Dict[str, Any]], *, fwd, rev) -> Graph:
    """Build the dependency graph from intake items.

    `fwd` patterns capture "this issue depends on #N"; `rev` patterns capture
    "this issue unblocks/blocks #N" (=> #N depends on this issue). Each pattern
    has a single `(\\d+)` group. Mirrors the old inline parse in ranker.py so the
    offline ranking is byte-for-byte unchanged.
    """
    present = {int(i["number"]) for i in items}
    titles = {int(i["number"]): (i.get("title") or "") for i in items}
    deps: Dict[int, set] = {n: set() for n in present}
    for item in items:
        n = int(item["number"])
        text = f"{item.get('title', '')}\n{item.get('body', '')}"
        for r in _refs(text, fwd):
            if r in present and r != n:
                deps[n].add(r)
        for r in _refs(text, rev):
            if r in present and r != n:
                deps[r].add(n)

    depth_cache: Dict[int, int] = {}

    def depth(n: int, stack: frozenset) -> int:
        if n in depth_cache:
            return depth_cache[n]
        if n in stack:                       # cycle guard (bounds recursion)
            return 0
        d = 0
        for p in deps[n]:
            d = max(d, 1 + depth(p, stack | {n}))
        depth_cache[n] = d
        return d

    def _on_cycle(start: int) -> bool:
        # `start` participates in a cycle iff it is reachable from itself via one
        # or more edges. Computed independently of depth()'s memoization, which
        # would otherwise mask all but the first-visited node of a cycle.
        seen: set = set()
        stack = list(deps[start])
        while stack:
            x = stack.pop()
            if x == start:
                return True
            if x in seen:
                continue
            seen.add(x)
            stack.extend(deps[x])
        return False

    numbers = sorted(present)
    depths = {n: depth(n, frozenset()) for n in numbers}
    deps_sorted = {n: sorted(deps[n]) for n in numbers}
    roots = [n for n in numbers if not deps_sorted[n]]
    cycles = [n for n in numbers if _on_cycle(n)]   # all participants, ascending
    return Graph(numbers, titles, deps_sorted, depths, roots, cycles)


# --------------------------------------------------------------------------
# Renderers — all deterministic, no timestamps.
# --------------------------------------------------------------------------
def to_json(graph: Graph, *, source: str = "") -> Dict[str, Any]:
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


def render_mermaid(graph: Graph) -> str:
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


def render_markdown_table(graph: Graph, *,
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
