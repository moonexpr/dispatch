"""closure.py — success path (HANDOFF §5.8).

Python port of scripts/closure.sh. Two phases, selected by the payload:
Phase A (CI green + review approved, not yet merged) arms auto-merge and flips
in-review -> done-pending-merge; Phase B (merged event) flips
done-pending-merge -> done. The transitions live in
ExecutionVisitor.visit_closure (the closure stage); this module reads the
payload and drives that stage. NEVER bypasses branch protection.
"""

from __future__ import annotations

import json
import sys

from . import common
from .stages import ClosureStage
from .visitors import ExecutionVisitor, TickContext

_visitor = ExecutionVisitor()


def load_payload(arg=None) -> str:
    import os

    fixture = os.environ.get("PIPELINE_FIXTURE_PR")
    if fixture:
        return open(fixture).read()
    if arg and os.path.isfile(arg):
        return open(arg).read()
    return sys.stdin.read()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    payload = json.loads(load_payload(argv[0] if argv else None))
    pr = payload.get("pr")
    pr = "" if pr is None else str(pr)
    issue = payload.get("issue")
    issue = "" if issue is None else str(issue)
    conclusion = payload.get("conclusion") or "failure"
    review = payload.get("review_state") or "none"
    merged = payload.get("merged", False)
    merged = "true" if merged is True or merged == "true" else "false"
    if not pr:
        common.die("payload has no .pr")
    if not issue:
        common.die("payload has no .issue")

    ctx = TickContext(
        num=issue, pr=pr, conclusion=conclusion, review=review, merged=merged
    )
    ClosureStage().accept(_visitor, ctx)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except common.PipelineExit as exc:
        sys.exit(exc.code)
