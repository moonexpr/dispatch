# api_docs — automatic API documentation for dispatch

> Lives under `.github/api_docs/` to keep the repo root clean. That directory is
> not on `sys.path`, so the `python3 -m api_docs` commands below run with
> `PYTHONPATH=.github` (the `.mcp.json` server and the docs workflow set it for
> you).

Auto-generated API reference for the live dispatch packages
(`foundation`, `baseworkflow`, `websitewf`, `agents`), built for two audiences:

| Audience | Surface | How |
|----------|---------|-----|
| **Developers** | A static HTML site | Published to GitHub Pages by `.github/workflows/docs.yml` on every push to `main`. |
| **Agent developers** | An MCP server with tools | `dispatch-apidocs` (registered in `.mcp.json`): `list_index`, `lookup`, `find_usage`, `get_source`. |

Both are powered by one import-free core, [`introspect.py`](./introspect.py),
which derives the API **statically with `ast`** — it never imports the code it
documents. That is deliberate:

- Several submodules use bare sibling imports (`import workplan_config`) that
  only resolve with the package dir on `sys.path`, so import-based tools
  (pdoc/Sphinx) choke on them.
- `foundation.agent_sdk` drives the Claude Agent SDK, which can hang the
  interpreter under Python 3.14 (see `PROJECT.md`).

Static parsing sidesteps both: deterministic, version-independent, no runtime
deps, no module-level side effects. The cost is that the docs describe the API
*as written* (signatures + docstrings + locations) — exactly right for reference
docs.

## CLI

```bash
PYTHONPATH=.github python3 -m api_docs build [--out site]   # static HTML site
PYTHONPATH=.github python3 -m api_docs index [PACKAGE]      # text index (scoped)
PYTHONPATH=.github python3 -m api_docs lookup SYMBOL        # pydoc for a symbol
PYTHONPATH=.github python3 -m api_docs usage  SYMBOL        # find references
PYTHONPATH=.github python3 -m api_docs mcp                  # MCP server over stdio
```

`SYMBOL` accepts a fully-qualified path (`foundation.models.chat`,
`baseworkflow.BaseWorkflow`, `Class.method`) or a bare leaf (`chat`); an
ambiguous bare name returns a disambiguation list.

## MCP server (for agents)

The server is registered in the repo's `.mcp.json`, so a Claude Code session
opened in this repo picks it up automatically. It needs the `mcp` package:

```bash
python3 -m pip install -r requirements-docs.txt
```

Tools:

- **`list_index(package="")`** — browse the index of modules/classes/functions.
- **`lookup(symbol)`** — signature + full docstring + source location.
- **`find_usage(symbol, limit=60)`** — every reference, definition sites flagged.
- **`get_source(symbol)`** — the exact source of a definition.

The index is built once at startup; restart the server to pick up source edits.

## Tests

```bash
python3 .github/api_docs/test_apidocs.py   # exit 0 = pass (repo convention, no pytest)
```

Runs offline against a synthetic fixture and the real packages. The docs
workflow runs this as a gate before building the site.

## What is documented

`introspect.DEFAULT_ROOTS` lists the live packages. `visitor/` (dead graveyard)
and the empty `engine/` / `src/` husks are intentionally excluded. Keep that
tuple in sync with the canonical tree in `PROJECT.md` if packages are added or
renamed.
