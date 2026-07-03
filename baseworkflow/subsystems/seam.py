#!/usr/bin/env python3
"""seam.py — the frozen architect↔worker seam (issue #143).

This module is the single source of truth for the contract that crosses the
architect↔worker boundary. It pins the **version** every seam artifact carries
and exposes a thin validator over the v1 JSON schemas in ``schemas/``:

  * ``schemas/work-order.v1.json``  — the full ``dispatch --json`` envelope.
  * ``schemas/job-request.v1.json`` — the worker-facing strict subset/projection.
  * ``schemas/invoice.v1.json``     — the Engineer's return artifact.

The point of freezing the seam (per #141 / #95): the harness and the worker
units can build and test against these schemas + the golden fixtures in
``schemas/fixtures/`` *in isolation*, while the Architect's internals keep
evolving behind WorkOrder v1. Additive fields are a minor bump (consumers
ignore unknown fields — note ``additionalProperties: true`` on the evolving
nested objects in work-order.v1.json); a breaking change bumps
``SEAM_SCHEMA_VERSION`` and opens a compat window during which the Architect
emits both versions.

The validator degrades gracefully: when ``jsonschema`` is importable it does a
full Draft 2020-12 validation; otherwise it falls back to a minimal structural
check (``schema_version`` stamp + the schema's top-level ``required`` keys) so
the live path never hard-fails merely because an optional dependency is absent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# The seam contract version. Every artifact that crosses the seam — the Work
# Order envelope, the Job Request projection, and the Invoice — carries this as
# ``schema_version``.
SEAM_SCHEMA_VERSION = 1

# <root>/schemas/ — this file lives at <root>/visitor/orchestration/seam.py,
# so repo root is parents[3] (orchestration -> visitor -> src -> <root>).
SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"
WORK_ORDER_SCHEMA = SCHEMAS_DIR / "work-order.v1.json"
JOB_REQUEST_SCHEMA = SCHEMAS_DIR / "job-request.v1.json"
INVOICE_SCHEMA = SCHEMAS_DIR / "invoice.v1.json"

# The Job Request seam projection: the v1 contract fields the worker depends on.
# Used to project a (possibly architect-enriched) live request down to the frozen
# seam before validating, so additive internal fields don't reject the seam.
JOB_REQUEST_FIELDS = (
    "schema_version", "job_id", "issue", "repo", "title", "body", "route", "scope", "confidence",
)


class SeamValidationError(ValueError):
    """A seam artifact failed validation against its v1 schema."""


def load_schema(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _structural_check(artifact: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    """Minimal fallback when ``jsonschema`` is unavailable: enforce the
    ``schema_version`` const and the schema's top-level ``required`` keys."""
    errors: List[str] = []
    props = schema.get("properties", {})
    sv = props.get("schema_version", {})
    if "const" in sv and artifact.get("schema_version") != sv["const"]:
        errors.append(
            f"schema_version must be {sv['const']!r}, got {artifact.get('schema_version')!r}"
        )
    for key in schema.get("required", []):
        if key not in artifact:
            errors.append(f"missing required field: {key!r}")
    return errors


def validate(artifact: Dict[str, Any], schema_path: Path) -> None:
    """Validate ``artifact`` against the v1 schema at ``schema_path``.

    Raises :class:`SeamValidationError` on the first failure. Uses ``jsonschema``
    for a full Draft 2020-12 validation when present, else a structural fallback.
    """
    schema = load_schema(schema_path)
    try:
        import jsonschema  # type: ignore
    except ImportError:
        problems = _structural_check(artifact, schema)
        if problems:
            raise SeamValidationError(
                f"{schema_path.name}: " + "; ".join(problems)
            )
        return
    try:
        jsonschema.validate(instance=artifact, schema=schema)
    except jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
        raise SeamValidationError(f"{schema_path.name}: {exc.message}") from exc


def validate_work_order(envelope: Dict[str, Any]) -> None:
    validate(envelope, WORK_ORDER_SCHEMA)


def validate_job_request(job_request: Dict[str, Any]) -> None:
    validate(job_request, JOB_REQUEST_SCHEMA)


def validate_invoice(invoice: Dict[str, Any]) -> None:
    validate(invoice, INVOICE_SCHEMA)


def is_valid(artifact: Dict[str, Any], schema_path: Path) -> Optional[str]:
    """Non-raising variant: return None when valid, else the error message."""
    try:
        validate(artifact, schema_path)
        return None
    except SeamValidationError as exc:
        return str(exc)


# Schema-version stamp helper, so producers do not repeat the literal.
def stamp(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``artifact`` with ``schema_version`` stamped first."""
    return {"schema_version": SEAM_SCHEMA_VERSION, **artifact}
