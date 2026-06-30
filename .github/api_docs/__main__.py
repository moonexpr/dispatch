#!/usr/bin/env python3
"""api_docs CLI — build the site, browse the index, or run the MCP server.

A thin dispatcher over the package's public functions so the same introspection
core is reachable from the shell (``PYTHONPATH=.github python3 -m api_docs ...``),
from CI (the docs workflow calls ``build``), and from a context-mode agent (which
runs ``index`` / ``lookup`` / ``usage`` and indexes the printed output). See the
package docstring for the full subcommand list.
"""
from __future__ import annotations

import argparse
import sys

from .introspect import DEFAULT_ROOTS, build_index, find_usage, lookup, repo_root_from_here


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="api_docs", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="generate the static HTML site")
    p_build.add_argument("--out", default="site", help="output directory (default: site)")

    p_index = sub.add_parser("index", help="print the text index")
    p_index.add_argument("package", nargs="?", default="", help="optional package/module prefix")

    p_lookup = sub.add_parser("lookup", help="pydoc for a dotted path or bare name")
    p_lookup.add_argument("symbol")

    p_usage = sub.add_parser("usage", help="find references to a symbol")
    p_usage.add_argument("symbol")
    p_usage.add_argument("--limit", type=int, default=100)

    sub.add_parser("mcp", help="run the MCP server over stdio")

    args = parser.parse_args(argv)
    root = repo_root_from_here()

    if args.cmd == "build":
        from .htmlgen import build_site

        idx = build_index(DEFAULT_ROOTS, root)
        out = build_site(args.out, idx)
        print(f"Wrote {len(idx.modules)} module pages + index to {out}/")
        return 0

    if args.cmd == "index":
        idx = build_index(DEFAULT_ROOTS, root)
        mods = idx.module_names()
        if args.package:
            mods = [m for m in mods if m == args.package or m.startswith(args.package + ".")]
        for q in mods:
            mod = idx.modules[q]
            print(f"{q}  ({len(mod.classes)} classes, {len(mod.functions)} functions)")
        print(f"\n{len(mods)} module(s).")
        return 0

    if args.cmd == "lookup":
        idx = build_index(DEFAULT_ROOTS, root)
        print(lookup(idx, args.symbol))
        return 0

    if args.cmd == "usage":
        usages = find_usage(args.symbol, DEFAULT_ROOTS, root, limit=args.limit)
        if not usages:
            print(f"No usages of {args.symbol!r}.")
            return 0
        for u in usages:
            flag = " [def]" if u.is_definition else ""
            print(f"{u.file}:{u.lineno}{flag}\t{u.line}")
        print(f"\n{len(usages)} usage(s).")
        return 0

    if args.cmd == "mcp":
        from .mcp_server import main as serve

        serve()
        return 0

    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
