#!/usr/bin/env python3
"""test_slice_supervisor.py — the per-slice supervisor's control flow, verified
deterministically with a stub session-runner (no live model).

Run: ``python3 baseworkflow/test_slice_supervisor.py`` (exit 0 = pass), matching
the repo's pytest-free, self-asserting convention (cf. foundation/actions/test_statechart.py).
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)  # <root>/baseworkflow -> <root>
# Force <root> ahead of this script's own dir: that dir holds baseworkflow.py, which
# would otherwise shadow the `baseworkflow` PACKAGE when PYTHONPATH already lists it.
while _ROOT in sys.path:
    sys.path.remove(_ROOT)
sys.path.insert(0, _ROOT)

from foundation.actions.action import BudgetMeter, Context  # noqa: E402
from foundation.actions.shelf import MemoryShelf, Shelves  # noqa: E402
from baseworkflow.slice_supervisor import run_slices_supervised  # noqa: E402

_PASSED = 0


def check(cond: bool, msg: str) -> None:
    global _PASSED
    if cond:
        _PASSED += 1
        print(f"  ok  {msg}")
    else:
        print(f"  FAIL  {msg}", file=sys.stderr)
        raise SystemExit(1)


def _ctx() -> Context:
    shelves = Shelves(MemoryShelf("input"), MemoryShelf("deliverables"), MemoryShelf("shared"))
    return Context(shelves=shelves, meter=BudgetMeter(10 ** 12, label="test"), dry_run=True)


def test_retry_then_partial():
    """u1 succeeds first try; u2 fails twice then succeeds (supervised retries); u3
    always fails (exhausts retries → needs-human). Partial progress is preserved and
    a failing slice does not block its siblings."""
    units = [{"id": "u1"}, {"id": "u2"}, {"id": "u3"}]
    committed, histories_seen = [], {}

    def run_slice(unit, attempt, prior):
        uid = unit["id"]
        histories_seen.setdefault(uid, []).append(list(prior))
        if uid == "u1":
            return True, "ok"
        if uid == "u2":
            return (attempt >= 3), f"u2 try {attempt}"   # succeeds on the 3rd
        return False, f"u3 try {attempt}"                 # never succeeds

    def commit_slice(unit):
        committed.append(unit["id"])

    report = run_slices_supervised(
        units, run_slice=run_slice, commit_slice=commit_slice, ctx=_ctx(), retry_cap=2)

    check(report.landed == ["u1", "u2"], f"landed u1,u2 (u2 after retries); got {report.landed}")
    check(report.needs_human == ["u3"], f"u3 flagged needs-human; got {report.needs_human}")
    check(committed == ["u1", "u2"], f"only landed slices committed; got {committed}")
    check(report.attempts["u1"] == 1, f"u1 ran once; got {report.attempts.get('u1')}")
    check(report.attempts["u2"] == 3, f"u2 ran 3x (1+2 retries); got {report.attempts.get('u2')}")
    check(report.attempts["u3"] == 3, f"u3 ran 3x then gave up; got {report.attempts.get('u3')}")
    # History injection: u2's third call must have seen its two prior failed attempts.
    check(histories_seen["u2"][-1] == ["attempt 1: u2 try 1", "attempt 2: u2 try 2"],
          f"prior attempts injected into the retry; got {histories_seen['u2'][-1]}")
    check(not report.all_landed, "all_landed is False when any slice needs a human")


def test_all_succeed():
    units = [{"id": "a"}, {"id": "b"}]
    committed = []
    report = run_slices_supervised(
        units, run_slice=lambda u, a, p: (True, "ok"),
        commit_slice=lambda u: committed.append(u["id"]), ctx=_ctx(), retry_cap=2)
    check(report.landed == ["a", "b"] and not report.needs_human, "all slices land, none need human")
    check(report.all_landed, "all_landed is True when every slice committed")
    check(committed == ["a", "b"], f"both committed in order; got {committed}")


def main() -> int:
    test_retry_then_partial()
    test_all_succeed()
    print(f"\nslice_supervisor: {_PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
