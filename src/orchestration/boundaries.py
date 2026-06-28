#!/usr/bin/env python3
"""boundaries.py — the remaining component-boundary contracts (issue #145).

Companion to ``seam.py``. Where ``seam.py`` freezes the architect↔worker seam
(WorkOrder / JobRequest / Invoice), this module freezes the five *internal*
component boundaries the #141 inventory flagged — the implicit dicts that cross
a producer→consumer line without a versioned contract:

  * ``schemas/issue.v1.json``          — the normalized intake item (``IntakeItem``,
    extended per #131/#132), produced by ``src/intake/intake.py``.
  * ``schemas/classification.v1.json`` — the triage verdict
    ``{action,scope,route,confidence}`` from ``src/classifier/classify.py``.
  * ``schemas/ranked-queue.v1.json``   — the ranker's ordered issues + rationale
    (``src/intake/ranker.py``).
  * ``schemas/pr-event.v1.json``       — the UNIFIED PR-event payload both
    ``closure.py`` (success) and ``fix_dispatch.py`` (fix ladder) read; ``attempt``
    has ONE source of truth (payload field wins over ``fix-attempt-N`` labels).
  * ``schemas/ledger-record.v1.json``  — the run-ledger record pinning the
    ``ledger.py build_record`` producer ↔ ``reconcile.py`` consumer pair (the
    exact ``cost.*`` field names reconcile depends on).

Validation is intentionally **fail-soft on the live path**: producers call
``warn_if_invalid`` (returns the error string and logs to stderr; never raises),
so a contract drift is *surfaced* without breaking a running tick. The strict,
raising ``validate_*`` helpers are what the unit suite (``test_boundaries.py``)
and any caller that wants hard enforcement use.

It reuses ``seam``'s ``validate`` / ``is_valid`` engine (full ``jsonschema``
Draft 2020-12 when present, structural fallback otherwise) so there is one
validation implementation across both seam and boundary contracts.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import seam

# The boundary contract version — every boundary artifact carries this as
# ``schema_version`` (same value family as the seam, kept independent so a
# boundary can rev without forcing a seam bump).
BOUNDARY_SCHEMA_VERSION = 1

SCHEMAS_DIR = seam.SCHEMAS_DIR
ISSUE_SCHEMA = SCHEMAS_DIR / "issue.v1.json"
CLASSIFICATION_SCHEMA = SCHEMAS_DIR / "classification.v1.json"
RANKED_QUEUE_SCHEMA = SCHEMAS_DIR / "ranked-queue.v1.json"
PR_EVENT_SCHEMA = SCHEMAS_DIR / "pr-event.v1.json"
LEDGER_RECORD_SCHEMA = SCHEMAS_DIR / "ledger-record.v1.json"


# Reuse the seam validation engine + error type, so there is one implementation.
BoundaryValidationError = seam.SeamValidationError


def validate(artifact: Dict[str, Any], schema_path: Path) -> None:
    """Strict validate (raises BoundaryValidationError on first failure)."""
    seam.validate(artifact, schema_path)


def is_valid(artifact: Dict[str, Any], schema_path: Path) -> Optional[str]:
    """Non-raising: None when valid, else the error message."""
    return seam.is_valid(artifact, schema_path)


def validate_issue(item: Dict[str, Any]) -> None:
    validate(item, ISSUE_SCHEMA)


def validate_classification(verdict: Dict[str, Any]) -> None:
    validate(verdict, CLASSIFICATION_SCHEMA)


def validate_ranked_queue(queue: Dict[str, Any]) -> None:
    validate(queue, RANKED_QUEUE_SCHEMA)


def validate_pr_event(payload: Dict[str, Any]) -> None:
    validate(payload, PR_EVENT_SCHEMA)


def validate_ledger_record(record: Dict[str, Any]) -> None:
    validate(record, LEDGER_RECORD_SCHEMA)


def stamp(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``artifact`` with ``schema_version`` stamped first.

    Mirrors ``seam.stamp``; used to lift a v0 dict to its v1 boundary artifact
    additively (the v0 shape is the v1 shape minus this stamp)."""
    return {"schema_version": BOUNDARY_SCHEMA_VERSION, **artifact}


def warn_if_invalid(
    artifact: Dict[str, Any],
    schema_path: Path,
    *,
    label: str = "",
    stamp_version: bool = True,
) -> Optional[str]:
    """Fail-soft live-path guard: validate ``artifact`` against ``schema_path``
    and, if it fails, log a one-line warning to stderr and return the error.

    NEVER raises and NEVER mutates the caller's artifact — it validates a
    *stamped copy* by default (``stamp_version=True``) so a producer that has
    not yet adopted the ``schema_version`` field still validates additively. The
    point (per #145 / #95): surface a boundary drift on the live path without
    breaking a running tick. Hard enforcement is the strict ``validate_*`` path.
    """
    candidate = stamp(artifact) if stamp_version else artifact
    err = is_valid(candidate, schema_path)
    if err is not None:
        name = label or schema_path.stem
        print(f"boundary-validate: {name}: {err}", file=sys.stderr)
    return err
