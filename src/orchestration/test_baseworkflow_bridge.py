#!/usr/bin/env python3
"""test_baseworkflow_bridge.py — BaseWorkflow authoring seam unit tests.

Proves the feature-flagged, fail-safe BaseWorkflow authoring seam
(``src/orchestration/baseworkflow_bridge.py`` + the ``DISPATCH_ENGINE`` flag
wired into ``visitors.ExecutionVisitor.visit_workorder``). As of Phase 3,
``baseworkflow`` is the DEFAULT engine and ``visitor`` is the explicit fallback:

  (a) with DISPATCH_ENGINE=baseworkflow + dry-run, ``author_via_baseworkflow``
      enriches the request with ``orchestration_script`` + ``work_plan`` and
      makes NO model call;
  (b) the fail-safe returns the ORIGINAL request unchanged on any error;
  (c) DISPATCH_ENGINE=visitor (the explicit fallback) leaves ``ctx.job_request``
      untouched and never reaches the bridge;
  (d) with DISPATCH_ENGINE unset, the Phase-3 default (baseworkflow) drives
      visit_workorder through the bridge and enriches the request.

No pytest in this repo — a runnable, self-asserting module (exit 0 = pass),
matching the shell-harness convention.
Run: ``python3 src/orchestration/test_baseworkflow_bridge.py``.
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SRC)
for _p in (_ROOT, _SRC, _HERE):
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
    from src.orchestration import baseworkflow_bridge as bridge

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


def test_failsafe_returns_original_on_error() -> None:
    """(b) any error -> the ORIGINAL request is returned unchanged."""
    from src.orchestration import baseworkflow_bridge as bridge

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
    from src.orchestration.visitors import ExecutionVisitor, TickContext
    from src.orchestration import baseworkflow_bridge as bridge

    called = {"n": 0}
    orig = bridge.author_via_baseworkflow

    def _tripwire(req):
        called["n"] += 1
        return orig(req)

    bridge.author_via_baseworkflow = _tripwire
    # visit_workorder imports the symbol from the module at call time, so patch
    # there too.
    import src.orchestration.baseworkflow_bridge as bmod
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


def test_visitor_fallback_untouched() -> None:
    """(c) DISPATCH_ENGINE=visitor (the explicit fallback) leaves ctx.job_request
    untouched and never reaches the baseworkflow bridge."""
    os.environ["PIPELINE_DRY_RUN"] = "1"
    os.environ["DISPATCH_ENGINE"] = "visitor"
    try:
        n, built = _run_visit_workorder_with_tripwire()
    finally:
        os.environ.pop("DISPATCH_ENGINE", None)

    _ok(n == 0, "visitor-fallback path does not call the baseworkflow bridge")
    _ok("orchestration_script" not in built and "work_plan" not in built,
        "visitor-fallback path leaves the Job Request unenriched")
    _ok(built.get("issue") == 42 and built.get("route") == "gen-default",
        "visitor-fallback path builds the visitor Job Request as before")


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
    test_failsafe_returns_original_on_error()
    test_visitor_fallback_untouched()
    test_default_engine_enriches()
    print(f"-- bridge tests: PASS={_PASS} FAIL={_FAIL} --")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
