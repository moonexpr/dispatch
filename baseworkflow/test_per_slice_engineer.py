#!/usr/bin/env python3
"""test_per_slice_engineer.py — the per-slice engineer's DETERMINISTIC surface.

The live model session (run_slice -> a real EngineerAgent) cannot be unit-tested, but
everything around it is deterministic and is pinned here:

  * _per_slice_enabled() — the opt-out env parsing;
  * _slice_timeout(total) — the per-slice cap parsing + clamping (>=60, <=budget);
  * _slice_prompt(...) — the per-slice task, incl. the prior-attempt history a retry
    injects (and its absence on the first attempt);
  * the supervisor wiring properties the engineer relies on: commit fires exactly once
    per landed slice (never on a retry/failure), retry_cap=0 means no retries, an
    all-failing issue lands nothing, a first-slice failure doesn't strand the rest, and
    a re-run resets cleanly (idempotent across the outer monitor loop).

Run: ``python3 baseworkflow/test_per_slice_engineer.py`` (exit 0 = pass).
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
from baseworkflow.bindings import engineer as eng  # noqa: E402
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
    sh = Shelves(MemoryShelf("input"), MemoryShelf("deliverables"), MemoryShelf("shared"))
    return Context(shelves=sh, meter=BudgetMeter(10 ** 12, label="test"), dry_run=True)


def _with_env(key, value, fn):
    """Run fn() with os.environ[key]=value (or deleted when value is None), restored after."""
    prev = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        return fn()
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev


# -- _per_slice_enabled -----------------------------------------------------
def test_per_slice_enabled_parsing():
    K = "ENGINEER_PER_SLICE"
    check(_with_env(K, None, eng._per_slice_enabled), "default (unset) is enabled")
    for off in ("0", "false", "FALSE", "no", "off", " Off "):
        check(not _with_env(K, off, eng._per_slice_enabled), f"{off!r} disables per-slice")
    for on in ("1", "true", "yes", "anything"):
        check(_with_env(K, on, eng._per_slice_enabled), f"{on!r} keeps per-slice enabled")


# -- _slice_timeout ---------------------------------------------------------
def test_slice_timeout_clamping():
    K = "ENGINEER_SLICE_TIMEOUT_SECONDS"
    check(_with_env(K, None, lambda: eng._slice_timeout(900)) == 360, "default 360 within a 900s budget")
    check(_with_env(K, None, lambda: eng._slice_timeout(120)) == 120, "default capped to a smaller 120s budget")
    check(_with_env(K, None, lambda: eng._slice_timeout(30)) == 60, "floor of 60s even with a tiny budget")
    check(_with_env(K, "600", lambda: eng._slice_timeout(900)) == 600, "explicit 600 honored under budget")
    check(_with_env(K, "600", lambda: eng._slice_timeout(300)) == 300, "explicit value capped to the 300s budget")
    check(_with_env(K, "10", lambda: eng._slice_timeout(900)) == 60, "explicit below floor clamps up to 60")
    check(_with_env(K, "abc", lambda: eng._slice_timeout(900)) == 360, "non-numeric env falls back to 360")


# -- _slice_prompt ----------------------------------------------------------
def test_slice_prompt_first_attempt():
    s = {"issue": 42, "title": "Wire transactions"}
    unit = {"id": "u1", "specialization": "frontend", "deliverable": "the page",
            "acceptance": "renders + CRUD", "files": ["src/app/transactions/page.tsx", "src/lib/x.ts"]}
    p = eng._slice_prompt(s, unit, [])
    check("Implement ONE slice of issue #42 — Wire transactions." in p, "names the issue + title")
    check("Slice specialization: frontend." in p, "carries the specialization label")
    check("Deliverable: the page" in p, "carries the deliverable")
    check("Acceptance: renders + CRUD" in p, "carries the acceptance")
    check("Files: src/app/transactions/page.tsx, src/lib/x.ts" in p, "lists the files")
    check("Implement ONLY this slice" in p, "scopes the engineer to one slice")
    check("PRIOR ATTEMPTS" not in p, "no prior-attempts block on the first attempt")


def test_slice_prompt_injects_history_on_retry():
    s = {"issue": 7, "title": "T"}
    unit = {"id": "u2"}  # missing fields -> defaults exercised
    p = eng._slice_prompt(s, unit, ["attempt 1: boom", "attempt 2: kaboom"])
    check("Deliverable: see acceptance criteria" in p, "deliverable defaults when absent")
    check("the issue's acceptance criteria are met" in p, "acceptance defaults when absent")
    check("Files: n/a" in p, "files default to n/a when absent")
    check("PRIOR ATTEMPTS ON THIS SLICE FAILED" in p, "retry injects the prior-attempts header")
    check("attempt 1: boom" in p and "attempt 2: kaboom" in p, "every prior attempt is listed")


# -- supervisor wiring properties -------------------------------------------
def _run(units, outcomes, *, retry_cap=2):
    """Drive the supervisor with a scripted run_slice. ``outcomes[uid]`` is a callable
    attempt->(ok, summary). Records commits to prove commit-once semantics."""
    committed = []

    def run_slice(unit, attempt, prior):
        return outcomes[unit["id"]](attempt)

    rep = run_slices_supervised(
        units, run_slice=run_slice, commit_slice=lambda u: committed.append(u["id"]),
        ctx=_ctx(), retry_cap=retry_cap)
    return rep, committed


def test_commit_fires_once_per_landed_slice():
    units = [{"id": "a"}, {"id": "b"}]
    rep, committed = _run(units, {
        "a": lambda n: (n >= 2, f"a{n}"),   # fails once, then lands
        "b": lambda n: (True, "b"),         # lands first try
    })
    check(committed == ["a", "b"], f"commit once per landed slice, in order; got {committed}")
    check(committed.count("a") == 1, "no commit on the failed first attempt of 'a'")
    check(rep.landed == ["a", "b"] and not rep.needs_human, "both landed, none need a human")


def test_retry_cap_zero_means_no_retries():
    units = [{"id": "x"}]
    rep, committed = _run(units, {"x": lambda n: (False, f"x{n}")}, retry_cap=0)
    check(rep.attempts["x"] == 1, f"retry_cap=0 -> exactly one attempt; got {rep.attempts['x']}")
    check(rep.needs_human == ["x"] and not committed, "the single failure goes straight to needs-human")


def test_all_slices_fail_lands_nothing():
    units = [{"id": "a"}, {"id": "b"}]
    rep, committed = _run(units, {"a": lambda n: (False, "a"), "b": lambda n: (False, "b")})
    check(rep.landed == [] and not rep.all_landed, "nothing lands when every slice fails")
    check(rep.needs_human == ["a", "b"], f"all slices flagged needs-human; got {rep.needs_human}")
    check(committed == [], "no commits when nothing succeeds")
    check(rep.attempts == {"a": 3, "b": 3}, f"each slice ran 1+2 retries; got {rep.attempts}")


def test_first_slice_failure_does_not_strand_rest():
    units = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    rep, committed = _run(units, {
        "a": lambda n: (False, "a"),        # first slice exhausts -> needs-human
        "b": lambda n: (True, "b"),
        "c": lambda n: (True, "c"),
    })
    check(rep.needs_human == ["a"], "the failed FIRST slice is flagged")
    check(rep.landed == ["b", "c"], f"later slices still land after a first-slice failure; got {rep.landed}")
    check(committed == ["b", "c"], "and are committed")


def test_rerun_is_idempotent():
    """Re-running on the SAME ctx (as the outer monitor loop would) resets cursor/attempts
    /history — the second run starts clean, not mid-cursor."""
    units = [{"id": "a"}, {"id": "b"}]
    ctx = _ctx()
    calls = {"a": 0, "b": 0}

    def run_slice(unit, attempt, prior):
        calls[unit["id"]] += 1
        return True, "ok"

    for _ in range(2):
        run_slices_supervised(units, run_slice=run_slice, commit_slice=lambda u: None,
                              ctx=ctx, retry_cap=2)
    # If the second run had NOT reset the cursor, slice 'a' (index 0) would be skipped
    # and calls['a'] would be 1 — so calls=={a:2,b:2} is the proof of the reset.
    check(calls == {"a": 2, "b": 2}, f"each slice ran once per run, twice total; got {calls}")


def main() -> int:
    test_per_slice_enabled_parsing()
    test_slice_timeout_clamping()
    test_slice_prompt_first_attempt()
    test_slice_prompt_injects_history_on_retry()
    test_commit_fires_once_per_landed_slice()
    test_retry_cap_zero_means_no_retries()
    test_all_slices_fail_lands_nothing()
    test_first_slice_failure_does_not_strand_rest()
    test_rerun_is_idempotent()
    print(f"\nper_slice_engineer: {_PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
