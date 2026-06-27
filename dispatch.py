#!/usr/bin/env python3
"""dispatch.py — the single Python entry point for the Unattended Engineering Pipeline.

The grounded replacement for the former shell launchers (the thin ``./pipeline`` /
``./dispatch`` passthroughs and ``deploy/run.sh``). The whole pipeline is Python:
the orchestration layer (``src/orchestration``, visitor-pattern stages) parses the
flags and runs the tick; the Engineer is the Claude Agent SDK engineer
(``src/orchestration/engineer_sdk.py``, Claude-subscription auth, multi-agent). No
shell anywhere in dispatch.

Engine status (read this before assuming): the live tick is driven TODAY by the
``src/orchestration`` visitor pipeline (``src/orchestration/pipeline.py``), which
owns the GitHub lifecycle (claim/labels/PR/closure) and invokes the Engineer. The
BaseWorkflow engine (``engine/workflow`` + ``engine/actions`` + ``src/baseworkflow``)
is a separate, additive automata substrate that does NOT yet drive the tick. Its
foundational live runner — ``src/baseworkflow.run_live(...)`` against
``RealActionFactory`` — exists as of Phase 1, but is not wired into this entry
point. Wiring it behind a feature flag (e.g. ``DISPATCH_ENGINE=baseworkflow``),
bridging BaseWorkflow's engineering Action to ``engineer_sdk``, and the real
GitHub bindings are Phase 2+ work — not done here. Do not claim BaseWorkflow
drives the tick until that wiring lands.

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
