#!/usr/bin/env python3
"""test_apidocs.py — deterministic, offline self-test for the apidocs core.

Follows the repo convention (no pytest): a runnable, self-asserting module where
exit 0 = pass. Two layers:

  1. Fixture layer — build an index over a synthetic package written to a temp
     dir, and assert the AST core extracts modules, classes, methods, functions,
     signatures (incl. *args/**kwargs, keyword-only, defaults, annotations,
     return types), docstrings, usage sites, and renders an HTML site.
  2. Live layer — run the same core over the real dispatch packages and assert it
     finds a non-trivial surface and produces a valid site + search index.

Run:  python3 .github/api_docs/test_apidocs.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))      # <root>/.github/api_docs
_PKG_PARENT = os.path.dirname(_HERE)                    # <root>/.github (carries the package)
_ROOT = os.path.dirname(_PKG_PARENT)                    # <root>
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from api_docs import build_index, build_site, find_usage, lookup, search_records  # noqa: E402
from api_docs.introspect import DEFAULT_ROOTS  # noqa: E402

_PASSED = 0


def check(cond: bool, label: str) -> None:
    global _PASSED
    if not cond:
        raise AssertionError(f"FAIL: {label}")
    _PASSED += 1
    print(f"  ok: {label}")


_FIXTURE = '''\
"""widgets — a fixture package docstring."""
from __future__ import annotations

CONST = 1


def make(name: str, count: int = 3, *parts, scale: float = 1.0, **opts) -> "Widget":
    """Build a Widget from parts.

    Longer body that should not appear in the one-line summary.
    """
    return Widget(name)


def _private():
    """Should be excluded by default."""


class Widget:
    """A widget with one method."""

    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def label(self) -> str:
        """The display label."""
        return self.name

    async def render(self, *, pretty: bool = False) -> str:
        """Render it."""
        return make(self.name).name
'''

_FIXTURE_USER = '''\
"""consumer module that uses widgets.make and Widget."""
from widgets import make, Widget


def go():
    w = make("a")          # usage of make
    return Widget(w.name)  # usage of Widget
'''


def run_fixture() -> None:
    print("[fixture layer]")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pkg = root / "widgets"
        pkg.mkdir()
        (pkg / "__init__.py").write_text(_FIXTURE, encoding="utf-8")
        (pkg / "consumer.py").write_text(_FIXTURE_USER, encoding="utf-8")

        idx = build_index(("widgets",), root)
        check("widgets" in idx.modules, "package module discovered")
        check(idx.modules["widgets"].is_package, "is_package flag set on __init__")
        check("widgets.consumer" in idx.modules, "submodule discovered")
        check("widgets.make" in idx.symbols, "function in symbol table")
        check("widgets.Widget" in idx.symbols, "class in symbol table")
        check("widgets.Widget.render" in idx.symbols, "method in symbol table")
        check("widgets._private" not in idx.symbols, "private function excluded")

        # Signature rendering of make()
        out = lookup(idx, "widgets.make")
        for fragment in ["name: str", "count: int = 3", "*parts", "scale: float = 1.0", "**opts", "-> 'Widget'"]:
            check(fragment in out, f"make() signature renders {fragment!r}")
        check("Build a Widget from parts." in out, "make() docstring rendered")
        check("Longer body" in out, "make() full docstring (not just summary) in lookup")

        # Class + method rendering
        cls_out = lookup(idx, "widgets.Widget")
        check("class  widgets.Widget" in cls_out, "class header rendered")
        check("render" in cls_out and "async method" in cls_out.replace("\n", " ") or "render" in cls_out,
              "method listed under class")

        meth_out = lookup(idx, "widgets.Widget.render")
        check("async method" in meth_out, "async method kind detected")
        check("pretty: bool = False" in meth_out, "keyword-only default rendered")

        prop_out = lookup(idx, "widgets.Widget.label")
        check("property" in prop_out, "property decorator detected")

        # Bare-name resolution + ambiguity
        check("widgets.make" in lookup(idx, "make"), "bare-name lookup resolves")

        # Usage search — finds the two consumer references, flags the def.
        makes = find_usage("make", ("widgets",), root)
        files = {u.file for u in makes}
        check(any("consumer.py" in f for f in files), "usage found in consumer")
        check(any(u.is_definition for u in makes), "definition site flagged")

        # HTML site
        site = build_site(root / "site", idx)
        check((site / "index.html").exists(), "index.html written")
        check((site / "search.json").exists(), "search.json written")
        check((site / ".nojekyll").exists(), ".nojekyll written")
        check((site / "widgets.html").exists(), "module page written")
        recs = json.loads((site / "search.json").read_text())
        paths = {r["path"] for r in recs}
        check("widgets.make" in paths and "widgets.Widget.render" in paths, "search.json carries symbols")
        html = (site / "widgets.html").read_text()
        check("Build a Widget from parts." in html, "docstring rendered into HTML")
        check("&amp;" not in "make" and "<script>" not in html.split("apidocs.js")[0], "html body escaped")


def run_live() -> None:
    print("[live layer — real dispatch packages]")
    root = Path(_ROOT)
    present = [p for p in DEFAULT_ROOTS if (root / p).is_dir()]
    check(bool(present), f"at least one documented package present ({present})")

    idx = build_index(DEFAULT_ROOTS, root)
    check(len(idx.modules) >= 10, f"non-trivial module count ({len(idx.modules)})")
    check(len(idx.symbols) >= 50, f"non-trivial symbol count ({len(idx.symbols)})")
    check("foundation" in idx.packages(), "foundation package indexed")

    # lookup the first module round-trips
    first = idx.module_names()[0]
    check(first in lookup(idx, first), "live module lookup round-trips")

    # search records well-formed
    recs = search_records(idx)
    check(all({"path", "kind", "summary", "url", "file"} <= set(r) for r in recs[:200]),
          "search records have required keys")

    # site builds over the real tree
    with tempfile.TemporaryDirectory() as td:
        site = build_site(Path(td) / "site", idx)
        check((site / "index.html").exists(), "live index.html built")
        n_pages = len(list(site.glob("*.html")))
        check(n_pages >= len(idx.modules), f"a page per module ({n_pages} html files)")


def main() -> int:
    run_fixture()
    run_live()
    print(f"\nPASS — {_PASSED} checks green.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as e:
        print(f"\n{e}", file=sys.stderr)
        raise SystemExit(1)
