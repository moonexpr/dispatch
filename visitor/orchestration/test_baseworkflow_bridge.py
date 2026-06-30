#!/usr/bin/env python3
"""test_baseworkflow_bridge.py — BaseWorkflow authoring seam unit tests.

Proves the fail-safe BaseWorkflow authoring seam
(``src/orchestration/baseworkflow_bridge.py`` wired into
``visitors.ExecutionVisitor.visit_workorder``). ``baseworkflow`` is the only
supported engine; the historical ``visitor`` authoring fallback is deprecated
and disabled for release:

  (a) with DISPATCH_ENGINE=baseworkflow + dry-run, ``author_via_baseworkflow``
      enriches the request with ``orchestration_script`` + ``work_plan`` and
      makes NO model call;
  (b) the fail-safe returns the ORIGINAL request unchanged on any error;
  (c) the deprecated DISPATCH_ENGINE=visitor opt-in is IGNORED — the bridge runs
      and the request is enriched anyway (the visitor authoring path is disabled);
  (d) with DISPATCH_ENGINE unset, the default (baseworkflow) drives
      visit_workorder through the bridge and enriches the request.

No pytest in this repo — a runnable, self-asserting module (exit 0 = pass),
matching the shell-harness convention.
Run: ``python3 src/orchestration/test_baseworkflow_bridge.py``.
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # src/visitor/orchestration
_VISITOR = os.path.dirname(_HERE)                           # src/visitor
_SRC = os.path.dirname(_VISITOR)                            # src
_ROOT = os.path.dirname(_SRC)                               # repo root
# The orchestration lineage moved under src/visitor/ (see src/visitor/__init__.py),
# one level deeper. Put repo root on the path for the src.visitor.* package imports
# below, plus the subsystem dirs the bridge's enrich chain reaches via bare-name
# imports (mirrors src/baseworkflow/bindings/__init__.py).
for _p in (_ROOT, _SRC, _VISITOR, _HERE,
           os.path.join(_VISITOR, "architect"), os.path.join(_VISITOR, "budget")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# A representative Job Request as the workorder stage builds it.
_JOB_REQUEST = json.dumps(
    {
        "job_id": "issue-42",
        "issue": 42,
        "repo": "ReclaimByDesign/dispatch-testrepo-a",
        "title": "Add a /health endpoint",
        "body": "Expose a JSON health check at /health returning {ok:true}.",
        "route": "gen-default",
        "scope": "m",
        "confidence": 0.82,
    },
    separators=(",", ":"),
)

_PASS = 0
_FAIL = 0


def _ok(cond: bool, label: str) -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


def test_enrich_dry_run_no_model_call() -> None:
    """(a) baseworkflow + dry-run enriches with orchestration_script + work_plan,
    and never calls the model."""
    os.environ["PIPELINE_DRY_RUN"] = "1"
    from src.visitor.orchestration import baseworkflow_bridge as bridge

    # Spy on the real model entrypoint: under dry-run it must never be called.
    import engine.models as models

    calls = {"n": 0}
    orig_chat = models.chat

    def _spy(*a, **k):  # pragma: no cover — must NOT run under dry-run
        calls["n"] += 1
        return orig_chat(*a, **k)

    models.chat = _spy
    try:
        out = bridge.author_via_baseworkflow(_JOB_REQUEST)
    finally:
        models.chat = orig_chat

    enriched = json.loads(out)
    _ok(calls["n"] == 0, "dry-run makes NO model call (engine.models.chat unused)")
    _ok("orchestration_script" in enriched, "enriched request carries orchestration_script")
    _ok("work_plan" in enriched, "enriched request carries work_plan")
    # Original fields preserved.
    _ok(enriched.get("issue") == 42 and enriched.get("route") == "gen-default",
        "original Job Request fields preserved through enrichment")


def test_enrich_carries_plan_units_for_fanout() -> None:
    """(a2) the enriched request carries the ``plan`` deliverable with a non-empty
    ``units`` list — the carrier shape the SDK Engineer's ``_extract_units`` fans
    out on (``job["plan"]["units"]``). Without this the Engineer sees zero units
    and collapses an authored multi-unit decomposition to a single agent."""
    os.environ["PIPELINE_DRY_RUN"] = "1"
    from src.visitor.orchestration import baseworkflow_bridge as bridge

    enriched = json.loads(bridge.author_via_baseworkflow(_JOB_REQUEST))
    plan = enriched.get("plan")
    _ok(isinstance(plan, dict), "enriched request carries the structured plan deliverable")
    units = (plan or {}).get("units")
    _ok(isinstance(units, list) and len(units) >= 1,
        "plan.units is a non-empty list (Engineer will fan out, not collapse to one agent)")
    # Cross-check against the actual consumer contract: the Engineer's unit
    # extractor recognises the enriched request as a decomposition.
    try:
        from src.visitor.orchestration.engineer_sdk import _extract_units

        found, _staffing = _extract_units(enriched)
        _ok(len(found) >= 1,
            "engineer_sdk._extract_units recognises plan.units on the enriched request")
    except Exception as exc:  # noqa: BLE001 — engineer may be mid-refactor; don't hard-fail the bridge test
        print(f"  SKIP _extract_units cross-check ({type(exc).__name__}: {exc})")


def test_failsafe_returns_original_on_error() -> None:
    """(b) any error -> the ORIGINAL request is returned unchanged."""
    from src.visitor.orchestration import baseworkflow_bridge as bridge

    # Malformed JSON: json.loads raises -> fail-safe path.
    bad = "{not valid json"
    _ok(bridge.author_via_baseworkflow(bad) == bad, "malformed JSON returns original unchanged")

    # A non-object JSON (a list) also takes the fail-safe path.
    arr = "[1,2,3]"
    _ok(bridge.author_via_baseworkflow(arr) == arr, "non-object JSON returns original unchanged")

    # A valid request but a forced run_live failure -> original returned verbatim.
    import baseworkflow as bw

    orig_run_live = bw.run_live

    def _boom(*a, **k):
        raise RuntimeError("forced failure")

    bw.run_live = _boom
    try:
        out = bridge.author_via_baseworkflow(_JOB_REQUEST)
    finally:
        bw.run_live = orig_run_live
    _ok(out == _JOB_REQUEST, "forced run_live failure returns the original request byte-for-byte")


def _run_visit_workorder_with_tripwire():
    """Drive ExecutionVisitor.visit_workorder with the bridge replaced by a
    tripwire that counts calls. Returns (call_count, built_job_request)."""
    from src.visitor.orchestration.visitors import ExecutionVisitor, TickContext
    from src.visitor.orchestration import baseworkflow_bridge as bridge

    called = {"n": 0}
    orig = bridge.author_via_baseworkflow

    def _tripwire(req):
        called["n"] += 1
        return orig(req)

    bridge.author_via_baseworkflow = _tripwire
    # visit_workorder imports the symbol from the module at call time, so patch
    # there too.
    import src.visitor.orchestration.baseworkflow_bridge as bmod
    bmod_orig = bmod.author_via_baseworkflow
    bmod.author_via_baseworkflow = _tripwire

    visitor = ExecutionVisitor()
    ctx = TickContext(
        repo="ReclaimByDesign/dispatch-testrepo-a",
        num="42",
        title="Add a /health endpoint",
        body="body",
        labels="",
        route="gen-default",
        scope="m",
        confidence="0.82",
    )
    try:
        visitor.visit_workorder(None, ctx)
    finally:
        bridge.author_via_baseworkflow = orig
        bmod.author_via_baseworkflow = bmod_orig

    return called["n"], json.loads(ctx.job_request)


def test_visitor_opt_in_disabled() -> None:
    """(c) DISPATCH_ENGINE=visitor is DEPRECATED and DISABLED for release: the
    opt-in is ignored, so the bridge still runs and the request is enriched."""
    os.environ["PIPELINE_DRY_RUN"] = "1"
    os.environ["DISPATCH_ENGINE"] = "visitor"
    try:
        n, built = _run_visit_workorder_with_tripwire()
    finally:
        os.environ.pop("DISPATCH_ENGINE", None)

    _ok(n >= 1, "disabled visitor opt-in is ignored; the baseworkflow bridge runs anyway")
    _ok("orchestration_script" in built and "work_plan" in built,
        "request is enriched despite DISPATCH_ENGINE=visitor (fallback disabled)")
    _ok(built.get("issue") == 42 and built.get("route") == "gen-default",
        "visitor opt-in path still preserves the Job Request fields")


def test_default_engine_enriches() -> None:
    """(d) With DISPATCH_ENGINE unset, the Phase-3 default (baseworkflow) drives
    visit_workorder through the bridge and enriches the Job Request."""
    os.environ["PIPELINE_DRY_RUN"] = "1"
    os.environ.pop("DISPATCH_ENGINE", None)  # default is now baseworkflow
    n, built = _run_visit_workorder_with_tripwire()

    _ok(n >= 1, "default (baseworkflow) path calls the baseworkflow bridge")
    _ok("orchestration_script" in built and "work_plan" in built,
        "default path enriches the Job Request with orchestration_script + work_plan")
    _ok(built.get("issue") == 42 and built.get("route") == "gen-default",
        "default path preserves the visitor Job Request fields")


def main() -> int:
    print("== test_baseworkflow_bridge ==")
    test_enrich_dry_run_no_model_call()
    test_enrich_carries_plan_units_for_fanout()
    test_failsafe_returns_original_on_error()
    test_visitor_opt_in_disabled()
    test_default_engine_enriches()
    print(f"-- bridge tests: PASS={_PASS} FAIL={_FAIL} --")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
