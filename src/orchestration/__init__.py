"""Orchestration layer for the Unattended Engineering Pipeline (v0).

Python port of the former scripts/*.sh orchestration glue. The shell entry
points (scripts/pipeline.sh, scripts/dispatch.sh, ...) are now thin launchers
that exec ``python3 -m src.orchestration.<name>``; all behaviour lives
here.

The six canonical tick stages (intake -> workorder -> prep -> engineer ->
intake-invoice -> closure) are modelled with the visitor pattern: each stage is
an element (``stages.py``) that accepts a ``StageVisitor`` (``visitors.py``).
``common.py`` is the shared contract ported from lib/common.sh.
"""
