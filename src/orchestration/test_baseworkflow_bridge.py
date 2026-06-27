#!/usr/bin/env python3
"""test_baseworkflow_bridge.py — Phase-2 seam unit tests.

Proves the additive, feature-flagged, fail-safe BaseWorkflow authoring seam
(``src/orchestration/baseworkflow_bridge.py`` + the ``DISPATCH_ENGINE`` flag
wired into ``visitors.ExecutionVisitor.visit_workorder``):

  (a) with DISPATCH_ENGINE=baseworkflow + dry-run, ``author_via_baseworkflow``
      enriches the request with ``orchestration_script`` + ``work_plan`` and
      makes NO model call;
  (b) the fail-safe returns the ORIGINAL request unchanged on any error;
  (c) the default flag (visitor) leaves ``ctx.job_request`` untouched.

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


def test_default_flag_off_untouched() -> None:
    """(c) DISPATCH_ENGINE unset/visitor leaves ctx.job_request untouched."""
    os.environ["PIPELINE_DRY_RUN"] = "1"
    os.environ.pop("DISPATCH_ENGINE", None)  # default is visitor

    from src.orchestration.visitors import ExecutionVisitor, TickContext
    from src.orchestration import baseworkflow_bridge as bridge

    # Tripwire: if the default path ever calls the bridge, the test fails loudly.
    called = {"n": 0}
    orig = bridge.author_via_baseworkflow

    def _tripwire(req):  # pragma: no cover — default path must not reach here
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

    built = json.loads(ctx.job_request)
    _ok(called["n"] == 0, "default (visitor) path does not call the baseworkflow bridge")
    _ok("orchestration_script" not in built and "work_plan" not in built,
        "default path leaves the Job Request unenriched")
    _ok(built.get("issue") == 42 and built.get("route") == "gen-default",
        "default path builds the visitor Job Request as before")


def main() -> int:
    print("== test_baseworkflow_bridge ==")
    test_enrich_dry_run_no_model_call()
    test_failsafe_returns_original_on_error()
    test_default_flag_off_untouched()
    print(f"-- bridge tests: PASS={_PASS} FAIL={_FAIL} --")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
