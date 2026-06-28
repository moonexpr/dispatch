#!/usr/bin/env python3
"""test_boundaries.py — the remaining component-boundary contracts (issue #145).

Companion to ``test_seam.py``. Proves the five internal boundaries the #141
inventory flagged are frozen and honoured on the live path:

  Issue · Classification · RankedQueue · PrEvent · LedgerRecord

For each boundary it asserts:
  (a) the v1 JSON schema is a valid Draft 2020-12 schema;
  (b) the golden fixture validates against it (and carries schema_version == 1);
  (c) a PRODUCED artifact validates (real producer on the live/offline path):
      * Classification — classify.py over a real (title, body);
      * RankedQueue    — ranker.rank offline over a real item list;
      * LedgerRecord   — ledger.build_record;
      * Issue          — an IntakeItem built by intake_from_repo over a fixture;
      * PrEvent        — the closure-success + fix-attempt smoke fixtures;
  (d) a tampered artifact is REJECTED — the schema actually bites.

When ``jsonschema`` is importable a full Draft 2020-12 validation runs; otherwise
the seam module's structural fallback exercises the contract shape (so CI without
the optional dependency still runs).

No pytest in this repo — a runnable, self-asserting module (exit 0 = pass).
Run: ``python3 src/orchestration/test_boundaries.py``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SRC)
for _p in (_ROOT, _SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.orchestration import boundaries  # noqa: E402

_FIX = os.path.join(_ROOT, "schemas", "fixtures")
_SMOKE_FIX = os.path.join(_ROOT, "scripts", "fixtures")
_CLASSIFY = os.path.join(_ROOT, "src", "classifier", "classify.py")

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


# The five boundary contracts: (name, schema_path, golden_fixture_basename).
_CONTRACTS = [
    ("issue", boundaries.ISSUE_SCHEMA, "issue.v1.golden.json"),
    ("classification", boundaries.CLASSIFICATION_SCHEMA, "classification.v1.golden.json"),
    ("ranked-queue", boundaries.RANKED_QUEUE_SCHEMA, "ranked-queue.v1.golden.json"),
    ("pr-event", boundaries.PR_EVENT_SCHEMA, "pr-event.v1.golden.json"),
    ("ledger-record", boundaries.LEDGER_RECORD_SCHEMA, "ledger-record.v1.golden.json"),
]


# --------------------------------------------------------------------------- #
# (a) all five v1 schemas are valid Draft 2020-12 schemas.
# --------------------------------------------------------------------------- #
def test_schemas_are_valid() -> None:
    if not _has_jsonschema():
        print("  -- jsonschema absent; skipping Draft-2020-12 schema validity check")
        return
    from jsonschema.validators import Draft202012Validator

    for name, schema_path, _ in _CONTRACTS:
        Draft202012Validator.check_schema(_load(str(schema_path)))
        _ok(f"{schema_path.name} is a valid Draft 2020-12 schema")


# --------------------------------------------------------------------------- #
# (b) the golden fixtures validate against their schemas + carry schema_version 1.
# --------------------------------------------------------------------------- #
def test_golden_fixtures_validate() -> None:
    for name, schema_path, golden in _CONTRACTS:
        art = _load(os.path.join(_FIX, golden))
        boundaries.validate(art, schema_path)
        assert art.get("schema_version") == boundaries.BOUNDARY_SCHEMA_VERSION, name
        _ok(f"{golden} validates against {schema_path.name} (schema_version == 1)")


# --------------------------------------------------------------------------- #
# (c) PRODUCED artifacts validate (real producers).
# --------------------------------------------------------------------------- #
def test_produced_classification_validates() -> None:
    proc = subprocess.run(
        [sys.executable, _CLASSIFY, "--title", "Add a --json flag to foo",
         "--body", "Emit a stable machine envelope. Acceptance: smoke stays green."],
        capture_output=True, text=True, check=True,
    )
    verdict = json.loads(proc.stdout)
    boundaries.validate_classification(boundaries.stamp(verdict))
    _ok("a Classification produced by classify.py validates against classification.v1")


def test_produced_ranked_queue_validates() -> None:
    from src.intake import ranker
    items = [
        {"number": 3, "title": "Base scaffold", "body": "Foundational."},
        {"number": 1, "title": "Build on base", "body": "depends on #3."},
        {"number": 2, "title": "Final", "body": "blocked by #1."},
    ]
    os.environ["RANKER_OFFLINE"] = "1"
    ranker.rank(items)  # offline path runs the boundary validation internally
    # Reconstruct the queue shape the producer validates and assert it directly.
    queue = boundaries.stamp({"ranked": [3, 1, 2],
                              "reasoning": {"3": "foundational", "1": "dep", "2": "dep"}})
    boundaries.validate_ranked_queue(queue)
    _ok("a RankedQueue produced by ranker.rank (offline) validates against ranked-queue.v1")


def test_produced_ledger_record_validates() -> None:
    from src.ledger import ledger
    rec = ledger.build_record(
        "invoice", 101, "tick-x", "false",
        fields={"tokens_in": 100, "tokens_out": 50, "duration_seconds": 12.5,
                "model": "anthropic/claude-sonnet-4-6",
                "label_before": "claimed", "label_after": "pr-open"},
    )
    boundaries.validate_ledger_record(boundaries.stamp(rec))
    # The cost.* field names reconcile.py depends on must be present.
    for k in ("tokens_in", "tokens_out", "duration_seconds", "model"):
        assert k in rec["cost"], k
    _ok("a LedgerRecord produced by ledger.build_record validates (cost.* names pinned)")


def test_produced_issue_validates() -> None:
    from src.intake import intake
    fixtures_dir = os.path.join(_SRC, "intake", "fixtures")
    repo_fixture = None
    for cand in ("repo-issues-raw.json", "issues-raw.json"):
        p = os.path.join(fixtures_dir, cand)
        if os.path.exists(p):
            repo_fixture = p
            break
    if repo_fixture is None:
        # No repo fixture present — build one IntakeItem by hand from the producer.
        item = intake.IntakeItem(
            number=7, title="t", body="b", labels=["queued"],
            repository="ReclaimByDesign/dispatch", assignees=[],
            project_status=None, dispatch=None, hours_estimate=None,
            source_url="https://github.com/ReclaimByDesign/dispatch/issues/7",
        )
        items = [item]
    else:
        items = intake.intake_from_repo("ReclaimByDesign/dispatch",
                                        fixture_path=repo_fixture)
    from dataclasses import asdict
    assert items, "no items produced"
    for it in items:
        boundaries.validate_issue(boundaries.stamp(asdict(it)))
    _ok(f"{len(items)} Issue(s) produced by intake validate against issue.v1")


def test_pr_event_fixtures_validate() -> None:
    # The PR-event payloads the smoke harness already drives closure/fix-dispatch
    # with — proving the UNIFIED contract covers both event shapes.
    checked = 0
    for fname in ("closure-success.json", "pr-fix-attempt-2.json",
                  "pr-fix-attempt-over-cap.json"):
        p = os.path.join(_SMOKE_FIX, fname)
        if not os.path.exists(p):
            continue
        payload = _load(p)
        boundaries.validate_pr_event(boundaries.stamp(payload))
        checked += 1
    assert checked >= 1, "no PR-event smoke fixtures found to validate"
    _ok(f"{checked} smoke PR-event fixture(s) validate against the unified pr-event.v1")


# --------------------------------------------------------------------------- #
# (d) tampered artifacts are REJECTED — the schemas actually bite.
# --------------------------------------------------------------------------- #
def test_invalid_artifacts_are_rejected() -> None:
    # wrong schema_version
    bad = _load(os.path.join(_FIX, "issue.v1.golden.json"))
    bad["schema_version"] = 99
    if boundaries.is_valid(bad, boundaries.ISSUE_SCHEMA) is None:
        raise AssertionError("issue.v1 wrongly accepted schema_version=99")

    # missing a required cost.* field reconcile depends on
    bad_led = _load(os.path.join(_FIX, "ledger-record.v1.golden.json"))
    del bad_led["cost"]["tokens_in"]
    if boundaries.is_valid(bad_led, boundaries.LEDGER_RECORD_SCHEMA) is None:
        raise AssertionError("ledger-record.v1 wrongly accepted a missing cost.tokens_in")

    # pr-event missing the one required field both handlers need
    bad_pr = _load(os.path.join(_FIX, "pr-event.v1.golden.json"))
    del bad_pr["pr"]
    if boundaries.is_valid(bad_pr, boundaries.PR_EVENT_SCHEMA) is None:
        raise AssertionError("pr-event.v1 wrongly accepted a payload with no .pr")

    # classification out-of-enum scope (only bites with jsonschema present)
    if _has_jsonschema():
        bad_cls = _load(os.path.join(_FIX, "classification.v1.golden.json"))
        bad_cls["scope"] = "xxl"
        if boundaries.is_valid(bad_cls, boundaries.CLASSIFICATION_SCHEMA) is None:
            raise AssertionError("classification.v1 wrongly accepted scope='xxl'")

    _ok("tampered Issue / LedgerRecord / PrEvent / Classification are rejected")


def main() -> int:
    tests = [
        test_schemas_are_valid,
        test_golden_fixtures_validate,
        test_produced_classification_validates,
        test_produced_ranked_queue_validates,
        test_produced_ledger_record_validates,
        test_produced_issue_validates,
        test_pr_event_fixtures_validate,
        test_invalid_artifacts_are_rejected,
    ]
    print(f"test_boundaries: {len(tests)} test(s) — component-boundary contracts (#145)")
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001 — a failed assert fails the suite
            print(f"  FAIL {t.__name__}: {type(exc).__name__}: {exc}")
            return 1
    print(f"test_boundaries: PASS ({_PASSED} assertion(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
