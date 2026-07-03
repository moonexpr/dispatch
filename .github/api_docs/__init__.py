"""api_docs — automatic API documentation for dispatch, for humans and agents.

The package lives under ``.github/api_docs`` to keep the repo root clean; that
directory is not on ``sys.path``, so the ``python3 -m api_docs`` forms below run
with ``PYTHONPATH=.github`` (the ``.mcp.json`` server and the docs workflow set
it for you).

Two consumers, one import-free core:

  * **Humans** get a static HTML site (``api_docs.htmlgen``) published to GitHub
    Pages by ``.github/workflows/docs.yml``.
  * **Agent developers** get an MCP server (``api_docs.mcp_server``) with tools to
    list the index, look up pydoc for a symbol, and find its usages.

Both are built on :mod:`api_docs.introspect`, which derives the API surface from
the source text with :mod:`ast` — never importing the documented code, so it is
deterministic, version-independent, and free of the project's runtime deps and
the Python 3.14 SDK-import hang.

CLI (see :mod:`api_docs.__main__`)::

    PYTHONPATH=.github python3 -m api_docs build [--out site]   # the HTML site
    PYTHONPATH=.github python3 -m api_docs index [PACKAGE]      # text index
    PYTHONPATH=.github python3 -m api_docs lookup SYMBOL        # pydoc for a symbol
    PYTHONPATH=.github python3 -m api_docs usage  SYMBOL        # find references
    PYTHONPATH=.github python3 -m api_docs mcp                  # the MCP server
"""
from __future__ import annotations

from .htmlgen import build_site, search_records
from .introspect import (
    DEFAULT_ROOTS,
    Index,
    build_index,
    find_usage,
    lookup,
    resolve,
    summary,
)

__all__ = [
    "DEFAULT_ROOTS",
    "Index",
    "build_index",
    "build_site",
    "find_usage",
    "lookup",
    "resolve",
    "search_records",
    "summary",
]
