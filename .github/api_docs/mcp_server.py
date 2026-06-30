#!/usr/bin/env python3
"""mcp_server.py — an MCP server exposing the dispatch API to agent developers.

This is the "agent developer" half of the documentation system. Where the HTML
site (``htmlgen``) serves humans with a browser, this server gives a coding agent
*tools* to interrogate the same API surface without burning context on raw file
reads:

  * ``list_index``  — browse the index of packages, modules, classes, functions.
  * ``lookup``      — pydoc-style help (signature + docstring + location) for a
                      dotted symbol path or a bare name.
  * ``find_usage``  — every reference to a symbol across the source tree.
  * ``get_source``  — the exact source lines of a definition.

All four are backed by :mod:`api_docs.introspect`, so the server imports nothing
from the documented packages — it is safe to run under any Python version and on
any checkout, with no project dependencies installed.

Run it over stdio (the form ``.mcp.json`` uses)::

    PYTHONPATH=.github python3 -m api_docs mcp

Requires the ``mcp`` package (see ``requirements-docs.txt``). The index is built
once at startup and cached for the process lifetime; restart the server to pick
up source changes.
"""
from __future__ import annotations

import ast
from pathlib import Path

from .introspect import (
    DEFAULT_ROOTS,
    build_index,
    find_usage,
    repo_root_from_here,
    resolve,
    summary,
)
from .introspect import lookup as _lookup

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover - surfaced at runtime
    raise SystemExit(
        "the 'mcp' package is required to run the api_docs MCP server.\n"
        "Install it with:  python3 -m pip install -r requirements-docs.txt"
    ) from exc


_REPO_ROOT = repo_root_from_here()
_INDEX = build_index(DEFAULT_ROOTS, _REPO_ROOT)

mcp = FastMCP("dispatch-apidocs")


@mcp.tool()
def list_index(package: str = "") -> str:
    """List the documented API as an index of dotted paths.

    Args:
        package: optional filter — pass a package or module prefix
            (e.g. ``foundation`` or ``foundation.workflow``) to scope the listing.
            Empty lists every documented module.

    Returns a compact, line-oriented index: one line per module with its class and
    function counts, suitable for choosing what to ``lookup`` next.
    """
    mods = _INDEX.module_names()
    if package:
        mods = [m for m in mods if m == package or m.startswith(package + ".")]
    if not mods:
        avail = ", ".join(_INDEX.packages())
        return f"No modules under {package!r}. Documented packages: {avail}."
    lines = [f"dispatch API index — {len(mods)} module(s)"
             + (f" under {package!r}" if package else "") + ":", ""]
    for q in mods:
        mod = _INDEX.modules[q]
        kind = "pkg" if mod.is_package else "mod"
        lines.append(f"[{kind}] {q}  ({len(mod.classes)} classes, {len(mod.functions)} functions)")
        s = summary(mod.docstring)
        if s:
            lines.append(f"      {s}")
        for cls in mod.classes:
            lines.append(f"      class {cls.qualname}")
        for fn in mod.functions:
            lines.append(f"      def   {fn.qualname}")
    lines.append("\nUse lookup('<dotted.path>') for signature + docstring.")
    return "\n".join(lines)


@mcp.tool()
def lookup(symbol: str) -> str:
    """Return pydoc-style help for a symbol: signature, docstring, and location.

    Args:
        symbol: a fully-qualified dotted path (``foundation.models.chat``,
            ``baseworkflow.BaseWorkflow``, ``Class.method``) or a bare name
            (``chat``). A bare name that is ambiguous returns a disambiguation
            list rather than guessing.
    """
    return _lookup(_INDEX, symbol)


@mcp.tool()
def find_usage(symbol: str, limit: int = 60) -> str:
    """Find every reference to a symbol across the documented source tree.

    Args:
        symbol: the identifier to search for. Matching is on the final dotted
            component (``models.chat`` and ``chat`` both search for ``chat``).
        limit: maximum number of results to return (default 60).

    Returns file:line locations with the source line, definition sites flagged
    with ``[def]``.
    """
    usages = find_usage(symbol, DEFAULT_ROOTS, _REPO_ROOT, limit=limit)
    if not usages:
        return f"No usages of {symbol!r} found in {', '.join(DEFAULT_ROOTS)}."
    lines = [f"{len(usages)} usage(s) of {symbol!r} (cap {limit}):", ""]
    for u in usages:
        flag = " [def]" if u.is_definition else ""
        lines.append(f"{u.file}:{u.lineno}{flag}    {u.line}")
    return "\n".join(lines)


@mcp.tool()
def get_source(symbol: str) -> str:
    """Return the exact source lines of a function, method, or class definition.

    Args:
        symbol: a dotted path or bare name resolving to a single definition.

    Use this when ``lookup`` (docstring + signature) is not enough and you need
    to read the implementation itself.
    """
    matches = resolve(_INDEX, symbol)
    if not matches:
        return f"No symbol matching {symbol!r}."
    if len(matches) > 1 and symbol not in _INDEX.symbols:
        return "Ambiguous — candidates:\n" + "\n".join("  " + m for m in matches)
    dotted = matches[0]
    kind, mod_q = _INDEX.symbols[dotted]
    if kind in ("module",):
        path = Path(_REPO_ROOT) / _INDEX.modules[dotted].file
        return path.read_text(encoding="utf-8")
    # Re-parse the owning module and pull the def's source segment.
    mod = _INDEX.modules[mod_q]
    path = Path(_REPO_ROOT) / mod.file
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    leaf = dotted.split(".")[-1]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == leaf:
            seg = ast.get_source_segment(src, node)
            if seg:
                return f"# {mod.file}:{node.lineno}\n{seg}"
    return f"Could not extract source for {dotted}."


def main() -> None:
    """Entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
