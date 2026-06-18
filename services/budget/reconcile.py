#!/usr/bin/env python3
"""reconcile.py — per-job budget reconciliation: actual vs authorized (E5-2, #39).

Reads dispatch's append-only JSONL run-ledger (E4-1, services/ledger/ledger.py)
and reconciles what each job ACTUALLY cost (cost.tokens_in + cost.tokens_out)
against what the ARCHITECT AUTHORIZED for it (approval.Authorization.budget_tokens),
then rolls the result up by day or ISO-week. Pure and offline: it reads JSON,
computes arithmetic, prints JSON — no monitor call, no network, no model, no
~/.claude read. The oracle (E5-1) owns the global usage window; reconcile owns
the per-issue attribution the global monitor cannot break down.

Authorized-budget resolution, in order:
  1. the ledger line's own authorized field (`authorized_tokens` / `budget_tokens`)
     — when E4-1 records the authorization on the line, reconcile reads it;
  2. an `authorization` record passed via --authorization (a stored Authorization);
  3. fallback re-derivation via approval.approve() on the line's `scope`.

Only cost-bearing job lines are reconciled (stage `invoice`/`engineer-dispatch`,
or any line that carries actual tokens) — the zero-cost claimed/work-order/closure
transitions are skipped so each job is counted once.

CLI
---
  python3 services/budget/reconcile.py --ledger run-ledger.jsonl
  python3 services/budget/reconcile.py --ledger run-ledger.jsonl --by day
  python3 services/budget/reconcile.py --ledger run-ledger.jsonl --by week

Output is a JSON object: {"jobs": [...]} (always), plus "aggregate": [...] when
--by is given. Deterministic: jobs sorted by issue, buckets by period, sort_keys
on — identical ledger -> byte-identical stdout.

Exit codes: 0 success; 1 runtime error; 2 bad args.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # services/ — for tuning / architect

_AUTHORIZED_FIELDS = ("authorized_tokens", "authorized_budget", "budget_tokens")


def _int(v: Any) -> int:
    if isinstance(v, bool) or v in (None, ""):
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _actual_tokens(line: Dict[str, Any]) -> int:
    cost = line.get("cost") or {}
    return _int(cost.get("tokens_in")) + _int(cost.get("tokens_out"))


def _budget_for_scope(scope: str, issue: int) -> int:
    """Re-derive the authorized budget from a scope via approval.approve()."""
    from architect import approval  # lazy: keep the common path free of the dep
    job = {"issue": issue or 0, "scope": scope}
    triage = {"scope": scope}
    return int(approval.approve(job, triage).budget_tokens)


def _authorized_tokens(line: Dict[str, Any], authorization: Optional[Dict[str, Any]]) -> int:
    cost = line.get("cost") or {}
    # 1. explicit on the ledger line (top level or under cost.*)
    for k in _AUTHORIZED_FIELDS:
        if line.get(k) not in (None, ""):
            return _int(line.get(k))
        if cost.get(k) not in (None, ""):
            return _int(cost.get(k))
    # 2. a passed-in authorization record
    if authorization:
        for k in _AUTHORIZED_FIELDS:
            if authorization.get(k) not in (None, ""):
                return _int(authorization.get(k))
    # 3. re-derive from scope via approval.approve()
    scope = line.get("scope") or (authorization or {}).get("scope")
    if scope:
        return _budget_for_scope(str(scope), _int(line.get("issue")))
    return 0


def reconcile_job(line: Dict[str, Any],
                  authorization: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Reconcile one ledger line: actual vs authorized -> verdict dict."""
    actual = _actual_tokens(line)
    authorized = _authorized_tokens(line, authorization)
    delta = actual - authorized
    cost = line.get("cost") or {}
    return {
        "issue": line.get("issue"),
        "route": line.get("route") or cost.get("route"),
        "model": cost.get("model") or line.get("model"),
        "actual_tokens": actual,
        "authorized_tokens": authorized,
        "delta": delta,
        "overspent": actual > authorized,
        "overspend_pct": round(100.0 * delta / authorized, 2) if authorized > 0 else 0.0,
    }


def _is_job_line(line: Dict[str, Any]) -> bool:
    stage = line.get("stage")
    if stage in ("invoice", "engineer-dispatch"):
        return True
    return stage is None and _actual_tokens(line) > 0


def _parse_ts(ts: str) -> datetime.datetime:
    s = (ts or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def _period_key(ts: str, by: str) -> str:
    dt = _parse_ts(ts)
    if by == "week":
        year, week, _ = dt.isocalendar()
        return f"{year}-W{week:02d}"
    return dt.strftime("%Y-%m-%d")


def aggregate(ledger_lines: List[Dict[str, Any]], *, by: str = "day",
              authorization: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Roll reconciled jobs into day / ISO-week period buckets."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for line in ledger_lines:
        if not _is_job_line(line):
            continue
        rec = reconcile_job(line, authorization)
        key = _period_key(line.get("timestamp") or "", by)
        b = buckets.setdefault(key, {
            "period": key, "sum_actual_tokens": 0, "sum_authorized_tokens": 0,
            "job_count": 0, "overspent_count": 0,
        })
        b["sum_actual_tokens"] += rec["actual_tokens"]
        b["sum_authorized_tokens"] += rec["authorized_tokens"]
        b["job_count"] += 1
        if rec["overspent"]:
            b["overspent_count"] += 1
    return [buckets[k] for k in sorted(buckets)]


def reconcile_ledger(ledger_lines: List[Dict[str, Any]],
                     authorization: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    jobs = [reconcile_job(ln, authorization) for ln in ledger_lines if _is_job_line(ln)]
    return sorted(jobs, key=lambda j: (_int(j.get("issue")), str(j.get("issue"))))


def _read_ledger(path: str) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))
    return lines


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Reconcile actual vs authorized job cost from the run-ledger (offline).")
    p.add_argument("--ledger", required=True, metavar="FILE", help="JSONL run-ledger (E4-1)")
    p.add_argument("--by", choices=("day", "week"), default=None,
                   help="also emit a day/ISO-week aggregate")
    p.add_argument("--authorization", default="", metavar="FILE",
                   help="optional Authorization JSON for re-derivation")
    args = p.parse_args(argv)

    try:
        lines = _read_ledger(args.ledger)
    except (OSError, ValueError) as exc:
        print(f"reconcile: cannot read ledger {args.ledger}: {exc}", file=sys.stderr)
        return 1

    authorization = None
    if args.authorization:
        try:
            authorization = _load_json(args.authorization)
        except (OSError, ValueError) as exc:
            print(f"reconcile: cannot read authorization {args.authorization}: {exc}", file=sys.stderr)
            return 1

    out: Dict[str, Any] = {"jobs": reconcile_ledger(lines, authorization)}
    if args.by:
        out["aggregate"] = aggregate(lines, by=args.by, authorization=authorization)

    sys.stdout.write(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
