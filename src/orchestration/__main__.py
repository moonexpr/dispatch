"""Single CLI entrypoint for the orchestration layer.

Invoked via the repo-root ``./pipeline`` launcher (``python -m
src.orchestration``). Two surfaces:

  ./pipeline [PIPELINE FLAGS]      run a full tick (was scripts/pipeline.sh):
                                   -b/-r/-l/-e/-f/-u/--from/-a/-h

  ./pipeline devtools <component>  debug entrypoints that "visit" / inspect ONE
                                   pipeline component in isolation (the per-stage
                                   .sh scripts collapsed into subcommands):

    devtools dispatch              the claim loop (was dispatch.sh)
    devtools intake-invoice IN     handle an Invoice (was architect-intake.sh)
    devtools closure       [FILE]  merge/close phases (was closure.sh)
    devtools fix           [FILE]  CI fix ladder (was fix-dispatch.sh)
    devtools intake        ...     gh ProjectV2/repo intake (was gh-intake.sh)
    devtools labels                provision label vocabulary (was bootstrap-labels.sh)
    devtools prep          JOBREQ  run just the prep stage on a Job Request
    devtools engineer      JOBREQ  run just the engineer stage on a Job Request

Every component routes through the same visitor machinery the full tick uses, so
a devtool is a faithful single-stage view of production behaviour, not a mock.
"""

from __future__ import annotations

import os
import sys

from . import (
    architect_intake,
    bootstrap_labels,
    closure,
    dispatch,
    fix_dispatch,
    gh_intake,
    pipeline,
)
from . import common


def _devtool_prep(argv) -> int:
    """Run only the prep stage on a captured Job Request (file or inline JSON)."""
    from .stages import PrepStage
    from .visitors import ExecutionVisitor, TickContext

    if not argv:
        common.die("devtools prep: needs a Job Request (file path or JSON string)")
    job = _read_arg(argv[0])
    ctx = TickContext(job_request=job)
    ok = PrepStage().accept(ExecutionVisitor(), ctx)
    return 0 if ok else 1


def _devtool_engineer(argv) -> int:
    """Run only the engineer stage on a captured Job Request (dispatch to ENGINEER_BIN)."""
    from .stages import EngineerStage
    from .visitors import ExecutionVisitor, TickContext

    if not argv:
        common.die("devtools engineer: needs a Job Request (file path or JSON string)")
    job = _read_arg(argv[0])
    ctx = TickContext(job_request=job)
    return EngineerStage().accept(ExecutionVisitor(), ctx) or 0


def _read_arg(arg: str) -> str:
    """An arg that is an existing file is read; otherwise treated as inline JSON."""
    import os

    if arg and os.path.isfile(arg):
        return open(arg).read()
    return arg


_DEVTOOLS = {
    "dispatch": lambda argv: dispatch.main(argv),
    "intake-invoice": lambda argv: architect_intake.main(argv),
    "closure": lambda argv: closure.main(argv),
    "fix": lambda argv: fix_dispatch.main(argv),
    "intake": lambda argv: gh_intake.main(argv),
    "labels": lambda argv: bootstrap_labels.main(argv),
    "prep": _devtool_prep,
    "engineer": _devtool_engineer,
}

# Subsystem label per component, so `[time] subsys: msg` stays attributable now
# that one process backs every former script (was the .sh basename).
_DEVTOOL_SUBSYS = {
    "dispatch": "dispatch",
    "intake-invoice": "architect-intake",
    "closure": "closure",
    "fix": "fix-dispatch",
    "intake": "gh-intake",
    "labels": "bootstrap-labels",
    "prep": "prep",
    "engineer": "engineer",
}


def _devtools(argv) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("devtools components: " + " ".join(_DEVTOOLS), file=sys.stderr)
        return 0 if argv[:1] in ([], ["-h"], ["--help"]) else 2
    component = argv[0]
    handler = _DEVTOOLS.get(component)
    if handler is None:
        common.err(f"unknown devtools component: {component}")
        common.err("valid components: " + " ".join(_DEVTOOLS))
        return 2
    os.environ.setdefault("DISPATCH_SUBSYS", _DEVTOOL_SUBSYS[component])
    return handler(argv[1:]) or 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "devtools":
        return _devtools(argv[1:])
    # Default surface: a full pipeline tick (pipeline.sh's flag set).
    os.environ.setdefault("DISPATCH_SUBSYS", "pipeline")
    return pipeline.main(argv)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except common.PipelineExit as exc:
        sys.exit(exc.code)
