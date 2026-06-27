#!/usr/bin/env python3
"""dispatch.py — the single Python entry point for the Unattended Engineering Pipeline.

The grounded replacement for the former shell launchers (the thin ``./pipeline`` /
``./dispatch`` passthroughs and ``deploy/run.sh``). The whole pipeline is Python:
the orchestration layer (``src/orchestration``, visitor-pattern stages) parses the
flags and runs the tick; the Engineer is the Claude Agent SDK engineer
(``src/orchestration/engineer_sdk.py``, Claude-subscription auth, multi-agent). No
shell anywhere in dispatch.

Engine status (read this before assuming): the GitHub lifecycle of every tick —
claim/labels/PR/closure — is owned by the ``src/orchestration`` visitor pipeline
(``src/orchestration/pipeline.py``), and the Engineer is ``engineer_sdk.py``. The
*authoring* engine for the workorder stage is BaseWorkflow:

  - the visitor's ``visit_workorder`` runs a BaseWorkflow (``engine/workflow`` +
    ``engine/actions`` + ``src/baseworkflow``, via
    ``src/orchestration/baseworkflow_bridge.py``) over the visitor-built Job
    Request and folds the authored ``orchestration_script`` / ``work_plan`` into it
    before the Engineer consumes it. Fail-safe: on any error the bridge returns the
    original request unchanged and logs a fallback line.

The historical ``DISPATCH_ENGINE=visitor`` authoring fallback (which skipped
BaseWorkflow and ran the visitor-built Job Request unchanged) is DEPRECATED and
DISABLED for release — it is no longer selectable; an explicit
``DISPATCH_ENGINE=visitor`` is ignored with a warning and BaseWorkflow runs anyway.
BaseWorkflow authors the spec/orchestration_script that feeds the Engineer — it
does not own the GitHub lifecycle and is not itself the Engineer.

  python3 dispatch.py [FLAGS]                  run a full tick
      -b/--bootstrap  -r/--repo OWNER/REPO  -l/--live  -e/--engineer BIN
      -f/--fixture FILE  -u/--until STAGE  --from STAGE  -a/--artifact FILE  -h/--help
  python3 dispatch.py devtools <component> …   inspect/run ONE component
      dispatch | intake-invoice | closure | fix | intake | labels | prep | engineer

Multi-issue per tick: set ``PIPELINE_CONCURRENCY=N`` (default 1). The default
Engineer is the SDK engineer; override with ``-e/--engineer`` or ``ENGINEER_BIN``.
"""
from __future__ import annotations

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))

# Grounded-replacement defaults (the entry owns policy; the orchestration core
# stays backend-agnostic): the SDK engineer (subscription auth, multi-agent) is the
# default Engineer, and bytecode writes are suppressed. setdefault, so an explicit
# ENGINEER_BIN / -e override still wins.
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
os.environ.setdefault(
    "ENGINEER_BIN", os.path.join(_ROOT, "src", "orchestration", "engineer_sdk.py")
)
os.environ["PYTHONPATH"] = _ROOT + (
    os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
)


def main(argv: list) -> int:
    """Run the Python orchestration tick with argv passed through unchanged."""
    python_bin = os.environ.get("PYTHON_BIN", sys.executable)
    return subprocess.call([python_bin, "-m", "src.orchestration", *argv], cwd=_ROOT, env=os.environ)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
