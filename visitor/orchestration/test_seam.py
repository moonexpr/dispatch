#!/usr/bin/env python3
"""test_seam.py — the frozen architect↔worker seam contract (issue #143).

Proves the WorkOrder v1 / Invoice v1 / JobRequest v1 seam is frozen and honoured
on the live path:

  (a) all three v1 JSON schemas (``schemas/{work-order,job-request,invoice}.v1.json``)
      are valid Draft 2020-12 schemas;
  (b) the three golden fixtures (``schemas/fixtures/*.v1.golden.json``) validate
      against their schemas — the contract the harness + worker units build/test
      against in isolation;
  (c) ``job-request.v1`` is a STRICT subset/projection of ``work-order.v1``: the
      golden Job Request equals the projection of the golden Work Order (one
      version family, no worker churn);
  (d) a Work Order PRODUCED by the Architect on the live path (``dispatch.py
      --issue N --json``) validates against ``work-order.v1`` and carries
      ``schema_version == 1``;
  (e) an Invoice PRODUCED by the Engineer (offline path) validates against
      ``invoice.v1`` and carries ``schema_version == 1``;
  (f) the produced Job Request projection of a produced Work Order validates
      against ``job-request.v1`` — the seam, not just the replay bridge.

When ``jsonschema`` is importable a full Draft 2020-12 validation runs; otherwise
the test falls back to the seam module's structural check (so CI without the
optional dependency still exercises the contract shape).

No pytest in this repo — a runnable, self-asserting module (exit 0 = pass),
matching the shell-harness convention.
Run: ``python3 src/orchestration/test_seam.py``.
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

from engine import proc  # noqa: E402  (capturing subprocess wrapper)
from src.orchestration import seam  # noqa: E402

_FIX = os.path.join(_ROOT, "schemas", "fixtures")
_DISPATCH = os.path.join(_ROOT, "src", "architect", "dispatch.py")
_ENGINEER = os.path.join(_ROOT, "src", "orchestration", "engineer_sdk.py")
_PLAY_QUEUE = os.path.join(_ROOT, "src", "architect", "fixtures", "play-queue.json")

_PASSED = 0


def _ok(msg: str) -> None:
    global _PASSED
    _PASSED += 1
    print(f"  ok  {msg}")


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _has_jsonschema() -> bool:
    try:
        import jsonschema  # noqa: F401
        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------- #
# (a) the three v1 schemas are valid Draft 2020-12 schemas.
# --------------------------------------------------------------------------- #
def test_schemas_are_valid() -> None:
    if not _has_jsonschema():
        print("  -- jsonschema absent; skipping Draft-2020-12 schema validity check")
        return
    from jsonschema.validators import Draft202012Validator

    for path in (seam.WORK_ORDER_SCHEMA, seam.JOB_REQUEST_SCHEMA, seam.INVOICE_SCHEMA):
        Draft202012Validator.check_schema(_load(str(path)))
        _ok(f"{path.name} is a valid Draft 2020-12 schema")


# --------------------------------------------------------------------------- #
# (b) the golden fixtures validate against their schemas.
# --------------------------------------------------------------------------- #
def test_golden_fixtures_validate() -> None:
    wo = _load(os.path.join(_FIX, "work-order.v1.golden.json"))
    jr = _load(os.path.join(_FIX, "job-request.v1.golden.json"))
    inv = _load(os.path.join(_FIX, "invoice.v1.golden.json"))
    seam.validate_work_order(wo)
    _ok("work-order.v1.golden validates against work-order.v1")
    seam.validate_job_request(jr)
    _ok("job-request.v1.golden validates against job-request.v1")
    seam.validate_invoice(inv)
    _ok("invoice.v1.golden validates against invoice.v1")
    for art, name in ((wo, "work-order"), (jr, "job-request"), (inv, "invoice")):
        assert art.get("schema_version") == seam.SEAM_SCHEMA_VERSION, name
    _ok(f"all three goldens carry schema_version == {seam.SEAM_SCHEMA_VERSION}")


# --------------------------------------------------------------------------- #
# (c) job-request.v1 is a strict projection of work-order.v1 (golden invariant).
# --------------------------------------------------------------------------- #
def test_job_request_is_projection_of_work_order() -> None:
    wo = _load(os.path.join(_FIX, "work-order.v1.golden.json"))
    jr = _load(os.path.join(_FIX, "job-request.v1.golden.json"))
    projection = {k: wo[k] for k in seam.JOB_REQUEST_FIELDS if k in wo}
    assert projection == jr, (
        "golden Job Request must equal the projection of the golden Work Order; "
        f"diff keys: {sorted(set(projection) ^ set(jr))}"
    )
    # The projection is also a valid Job Request — proves the subset relationship
    # holds at the schema level, not just by key equality.
    seam.validate_job_request(projection)
    _ok("golden job-request.v1 == strict projection of golden work-order.v1")


# --------------------------------------------------------------------------- #
# (d) a PRODUCED Work Order validates against work-order.v1 (live path).
# --------------------------------------------------------------------------- #
def _produce_work_order() -> dict:
    env = {**os.environ, "PIPELINE_DRY_RUN": "1", "RANKER_OFFLINE": "1"}
    cp = proc.run(
        [sys.executable, _DISPATCH, "--fixture", _PLAY_QUEUE, "--issue", "2", "--json"],
        env=env, check=True,
    )
    return json.loads(cp.stdout)


def test_produced_work_order_validates() -> None:
    wo = _produce_work_order()
    seam.validate_work_order(wo)
    assert wo.get("schema_version") == seam.SEAM_SCHEMA_VERSION
    _ok("a Work Order produced by dispatch.py --json validates against work-order.v1")
    # The projected Job Request of a produced Work Order is itself seam-valid.
    jr = {k: wo[k] for k in seam.JOB_REQUEST_FIELDS if k in wo}
    seam.validate_job_request(jr)
    _ok("the Job Request projection of a produced Work Order validates against job-request.v1")


# --------------------------------------------------------------------------- #
# (e) a PRODUCED Invoice validates against invoice.v1.
# --------------------------------------------------------------------------- #
def _produce_invoice() -> dict:
    job = json.dumps({
        "schema_version": 1, "job_id": "issue-2", "issue": 2,
        "repo": "ReclaimByDesign/dispatch", "title": "t", "body": "b",
        "route": "gen-default", "scope": "m", "confidence": 0.8,
    })
    env = {**os.environ, "PIPELINE_DRY_RUN": "1"}
    cp = proc.run(
        [sys.executable, _ENGINEER],
        input=job, env=env, check=True,
    )
    return json.loads(cp.stdout)


def test_produced_invoice_validates() -> None:
    inv = _produce_invoice()
    seam.validate_invoice(inv)
    assert inv.get("schema_version") == seam.SEAM_SCHEMA_VERSION
    _ok("an Invoice produced by the (offline) Engineer validates against invoice.v1")


# --------------------------------------------------------------------------- #
# (negative) a tampered artifact is REJECTED — proves the schema actually bites.
# --------------------------------------------------------------------------- #
def test_invalid_artifacts_are_rejected() -> None:
    bad_wo = _load(os.path.join(_FIX, "work-order.v1.golden.json"))
    bad_wo["schema_version"] = 99  # wrong version
    if seam.is_valid(bad_wo, seam.WORK_ORDER_SCHEMA) is None:
        raise AssertionError("work-order.v1 wrongly accepted schema_version=99")
    bad_inv = _load(os.path.join(_FIX, "invoice.v1.golden.json"))
    del bad_inv["timestamp"]  # drop a required field
    if seam.is_valid(bad_inv, seam.INVOICE_SCHEMA) is None:
        raise AssertionError("invoice.v1 wrongly accepted a missing required field")
    _ok("tampered Work Order / Invoice are rejected (schema bites)")


def main() -> int:
    tests = [
        test_schemas_are_valid,
        test_golden_fixtures_validate,
        test_job_request_is_projection_of_work_order,
        test_produced_work_order_validates,
        test_produced_invoice_validates,
        test_invalid_artifacts_are_rejected,
    ]
    print(f"test_seam: {len(tests)} test(s) — seam contract (#143)")
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001 — a failed assert fails the suite
            print(f"  FAIL {t.__name__}: {type(exc).__name__}: {exc}")
            return 1
    print(f"test_seam: PASS ({_PASSED} assertion(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
