#!/usr/bin/env python3
"""dispatch.py — the single Python entry point for the Unattended Engineering Pipeline.

The grounded replacement for the former shell launchers (the thin ``./pipeline`` /
``./dispatch`` passthroughs and ``deploy/run.sh``). The whole pipeline is Python:
the orchestration layer (``src/orchestration``, visitor-pattern stages) parses the
flags and runs the tick; the Engineer is the Claude Agent SDK engineer
(``src/orchestration/engineer_sdk.py``, Claude-subscription auth, multi-agent); the
workflow it drives is the YAML ``baseworkflow`` engine (``engine/workflow``). No
shell anywhere in dispatch.

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
