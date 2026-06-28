#!/usr/bin/env python3
"""ledger.py — append-only JSONL run-ledger (E4 / Pillar 4, issue #36).

Writes ONE JSON object per stage transition of a tick (newline-terminated, no
embedded newlines), so a tick that ran leaves a durable, machine-readable trace
instead of stderr lines that are lost. This is the RAW record only — it performs
no reconciliation, overspend, or aggregation math (that is E5); it merely records
the `cost.*` fields E5 later sums.

Stage vocabulary (fixed; extend only as the pipeline transition seam requires):

    claimed | work-order | engineer-dispatch | invoice | closure

Each line carries:
    tick_id, issue, stage,
    cost: {tokens_in, tokens_out, duration_seconds, model},
    label_before, label_after, timestamp (ISO-8601 UTC), dry_run

Missing optional fields serialize as ``null`` — the keys are always present,
never absent (so the file is uniformly `jq`-aggregatable).

Ledger path resolves from ``$DISPATCH_LEDGER_FILE``; otherwise
``${DISPATCH_ARTIFACTS_DIR:-./.artifacts}/run-ledger.jsonl``. The directory is
created if absent. This is a LOCAL-FILE write only — it never calls ``gh`` and is
NOT gated by dry-run (a dry-run tick still produced a transition worth recording);
instead every line records ``dry_run: true|false``.

OTel / hosted span export can be a later upgrade behind this same emit seam.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, Optional

STAGES = ("claimed", "work-order", "engineer-dispatch", "invoice", "closure")


def ledger_path(override: Optional[str] = None) -> str:
    """Resolve the ledger file path (override > env > default)."""
    if override:
        return override
    env = os.environ.get("DISPATCH_LEDGER_FILE")
    if env:
        return env
    base = os.environ.get("DISPATCH_ARTIFACTS_DIR") or "./.artifacts"
    return os.path.join(base, "run-ledger.jsonl")


def _iso_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_int(v: Any) -> Optional[int]:
    if isinstance(v, bool) or v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_num(v: Any):
    if isinstance(v, bool) or v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def _to_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _cost_from_invoice(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return (json.load(fh).get("cost") or {})
    except (OSError, ValueError):
        return {}


def build_record(stage: str, issue: Any, tick_id: str, dry_run: Any,
                 fields: Optional[Dict[str, Any]] = None,
                 timestamp: Optional[str] = None) -> Dict[str, Any]:
    """Assemble one ledger record. `fields` may carry label_before/label_after,
    explicit cost values (tokens_in/tokens_out/duration_seconds/model), and/or an
    `invoice` path whose cost.* block populates the engineer-stage cost fields.
    Explicit cost values in `fields` win over the invoice file."""
    f = dict(fields or {})
    inv_cost = _cost_from_invoice(f["invoice"]) if f.get("invoice") else {}

    def pick(key: str) -> Any:
        if f.get(key) not in (None, ""):
            return f.get(key)
        return inv_cost.get(key)

    model = pick("model")
    record = {
        "tick_id": tick_id or "tick-unknown",
        "issue": _to_int(issue),
        "stage": stage,
        "cost": {
            "tokens_in": _to_int(pick("tokens_in")),
            "tokens_out": _to_int(pick("tokens_out")),
            "duration_seconds": _to_num(pick("duration_seconds")),
            "model": model or None,
        },
        "label_before": f.get("label_before") or None,
        "label_after": f.get("label_after") or None,
        "timestamp": timestamp or _iso_now(),
        "dry_run": _to_bool(dry_run),
    }
    _validate_boundary(record)
    return record


def _validate_boundary(record: Dict[str, Any]) -> None:
    """Fail-soft ledger-record.v1 boundary guard (#145).

    Surfaces a producer↔reconcile drift (the cost.* field names reconcile.py
    depends on) on the live path without ever breaking a tick — the import and
    the validation are both best-effort. The on-disk line is the SAME object
    minus ``schema_version``; we validate a stamped copy additively.
    """
    try:
        _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from src.orchestration import boundaries
    except Exception:
        return
    try:
        boundaries.warn_if_invalid(record, boundaries.LEDGER_RECORD_SCHEMA,
                                   label="ledger-record")
    except Exception:
        pass


def emit(record: Dict[str, Any], path: Optional[str] = None) -> str:
    """Append one record as a single JSONL line. Returns the serialized line."""
    path = ledger_path(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line


def _parse_args(argv) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Append one stage transition to the run-ledger.")
    p.add_argument("--stage", required=True, help="one of: " + " | ".join(STAGES))
    p.add_argument("--issue", default="", help="issue number (empty -> null)")
    p.add_argument("--tick-id", default=os.environ.get("DISPATCH_TICK_ID", "tick-unknown"))
    p.add_argument("--dry-run", default=os.environ.get("PIPELINE_DRY_RUN", "1"),
                   help="true|false|1|0 — recorded verbatim as a boolean")
    p.add_argument("--fields", default="{}",
                   help="JSON object: label_before/label_after, cost values, "
                        "or an `invoice` path to read cost.* from")
    p.add_argument("--file", default=None, help="ledger path override (else env)")
    p.add_argument("--quiet", action="store_true", help="do not echo the line to stdout")
    return p.parse_args(argv)


def main(argv) -> int:
    args = _parse_args(argv)
    if args.stage not in STAGES:
        print(f"ledger: unknown stage {args.stage!r}; valid: {', '.join(STAGES)}",
              file=sys.stderr)
        return 2
    try:
        fields = json.loads(args.fields) if args.fields else {}
        if not isinstance(fields, dict):
            raise ValueError("--fields must be a JSON object")
    except ValueError as exc:
        print(f"ledger: bad --fields JSON: {exc}", file=sys.stderr)
        return 2
    record = build_record(args.stage, args.issue, args.tick_id, args.dry_run, fields)
    line = emit(record, path=args.file)
    if not args.quiet:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
