#!/usr/bin/env python3
"""engineer_sdk.py — the ``ENGINEER_BIN`` entrypoint for the SDK Engineer.

This is the thin shell around the YAML-expressed engineer unit-of-work. The
lifecycle itself — clone, run the (agentic) engineering pass, judge + commit, the
context-sanity gate, push, open ONE PR — lives in ``app/config/engineer.yml`` (the
mutable composition) and its per-action interface files, compiled by
``engine.workflow``; the implemented step bodies live in
``src/orchestration/engineer_actions.py`` (the bind layer). This file only does the
``ENGINEER_BIN`` I/O contract:

  Input:  a Job Request JSON (schemas/job-request.json) as ``argv[1]`` (a file
          path OR a literal JSON string) OR on stdin.
  Output: a schema-valid Invoice JSON (schemas/invoice.json) on stdout — and
          NOTHING else on stdout (logs go to stderr).
  Exit:   0 when a well-formed Invoice was produced (status completed |
          needs-human | failed | partial are all "handled"); 1 only on bad input.

It is a *drop-in* ``ENGINEER_BIN``: same interface as ``scripts/claude-engineer.sh``
/ ``scripts/mock-engineer.sh``, so the Architect
(``src/orchestration/common.py::engineer_dispatch``) is blind to which Engineer
backs the call.

Subscription auth (critical)
----------------------------
The engineering backend spawns the ``claude`` CLI/SDK under the hood; to bill
against the logged-in **Claude subscription** rather than API credits the spawn env
has ``ANTHROPIC_API_KEY`` removed so the CLI falls back to its OAuth subscription
session. The target clone is also run with an isolated ``CLAUDE_CONFIG_DIR`` so the
operator's ``~/.claude`` never poisons the deliverable (issue #159). See
``engineer_actions.run_cli`` for both. No API key is ever read on the live path.

OFFLINE / dry-run path (PIPELINE_DRY_RUN!=0, or ENGINEER_OFFLINE=1): no workflow,
no SDK, no gh, no git, no network — emit a deterministic, schema-valid ``completed``
Invoice (parity with mock-engineer.sh's role) so the pipeline wiring can be
exercised at zero cost and with zero mutations.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# sys.path bootstrap — make ``engine.*`` and the sibling ``engineer_actions``
# importable whether this file is run as a script (ENGINEER_BIN points straight at
# it) or imported. This file lives at <root>/src/orchestration/engineer_sdk.py.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))  # <root>
for _p in (_ROOT, os.path.join(_ROOT, "src"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import engineer_actions as ea  # noqa: E402  (sibling module; the bind layer + runner)

_log = ea._log


# ---------------------------------------------------------------------------
# Job Request parsing + Invoice emission (the ENGINEER_BIN I/O contract)
# ---------------------------------------------------------------------------
def _read_job_request(argv: List[str]) -> str:
    """Job Request from argv[1] (a file path OR a literal JSON string) or stdin."""
    if len(argv) >= 2 and argv[1].strip():
        arg = argv[1]
        if os.path.isfile(arg):
            with open(arg, "r", encoding="utf-8") as fh:
                return fh.read()
        return arg
    return sys.stdin.read()


def _parse_job(raw: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        return None


def _emit(invoice: Dict[str, Any]) -> None:
    """Print the Invoice JSON — and ONLY the Invoice — to stdout."""
    sys.stdout.write(json.dumps(invoice))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _is_offline() -> bool:
    """OFFLINE when dry-run is on (PIPELINE_DRY_RUN != "0") OR ENGINEER_OFFLINE=1.
    Mirrors common.is_dry_run() semantics (dry-run defaults ON)."""
    dry = os.environ.get("PIPELINE_DRY_RUN", "1") != "0"
    return dry or os.environ.get("ENGINEER_OFFLINE", "0") == "1"


def main(argv: List[str]) -> int:
    raw = _read_job_request(argv)
    job = _parse_job(raw)
    if not job or not job.get("issue") or not job.get("repo"):
        _log("could not parse required .issue/.repo from Job Request")
        return 1

    if _is_offline():
        _log(
            f"offline path (dry-run={os.environ.get('PIPELINE_DRY_RUN', '1')}, "
            f"ENGINEER_OFFLINE={os.environ.get('ENGINEER_OFFLINE', '0')}) — synthetic Invoice, "
            "no workflow/SDK/gh/git/network calls"
        )
        _emit(ea.offline_invoice(job))
        return 0

    _emit(ea.run_live(job))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
