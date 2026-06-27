#!/usr/bin/env python3
"""guard.py — pre-flight soft-cap throttle for the unattended cron (E5-3, #40).

Before a tick claims an issue, consult the E5-1 oracle's usage-window state and
decide whether to THROTTLE: when the window fraction is at or above the
operator-committed soft cap (tuning.budget.window.soft_cap_fraction), the tick
should flip to dry-run and skip claiming — it still runs (logs, artifacts,
ledger) but mutates nothing and spends no engineer budget. This is the throttle
half of D2 ("oracle = ceiling, ledger = attribution, pre-flight guard =
throttle"); a running engineer is never hard-stopped (pre-flight boundary only).

should_throttle() is a pure read of oracle.window_state().fraction vs the soft
cap. It FAILS OPEN: when no window data is available (no fixture, no
claude-monitor) the oracle raises, and the guard returns throttle=False so the
tick proceeds exactly as today — a missing oracle must never block the pipeline.

CLI: prints the decision JSON. With --record, a throttle decision also appends a
throttle line (event: throttled, fraction, soft_cap) to the E4-1 run-ledger via
its emit seam, so the operator digest (E4-2) can report skipped ticks.

Exit codes: 0 always — the decision is the stdout JSON, not the exit status.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # src/ — for tuning / ledger
sys.path.insert(0, _HERE)                    # src/budget — for oracle

import oracle  # noqa: E402
import tuning  # noqa: E402

_DEFAULT_SOFT_CAP = 0.85


def _soft_cap_fraction() -> float:
    """The operator-committed soft cap (tuning.budget.window.soft_cap_fraction)."""
    window = (tuning.load().get("budget", {}) or {}).get("window", {}) or {}
    try:
        return float(window.get("soft_cap_fraction"))
    except (TypeError, ValueError):
        return _DEFAULT_SOFT_CAP


def should_throttle() -> Dict[str, Any]:
    """Return {throttle, fraction, soft_cap, plan, reason}. throttle iff the
    window fraction is at/over the soft cap. Fails OPEN on missing window data."""
    soft_cap = _soft_cap_fraction()
    try:
        state = oracle.window_state()
        fraction = float(state.get("fraction") or 0.0)
        plan = str(state.get("plan") or "")
    except Exception:  # noqa: BLE001 — fail OPEN: a broken/absent oracle must never block the tick
        return {"throttle": False, "fraction": None, "soft_cap": soft_cap,
                "plan": "", "reason": "no window data (oracle unavailable) — proceeding"}
    throttle = fraction >= soft_cap
    reason = (f"window fraction {fraction} >= soft cap {soft_cap}" if throttle
              else f"window fraction {fraction} < soft cap {soft_cap}")
    return {"throttle": throttle, "fraction": fraction, "soft_cap": soft_cap,
            "plan": plan, "reason": reason}


def _record_throttle(decision: Dict[str, Any]) -> None:
    """Append a throttle line to the E4-1 run-ledger via its emit seam."""
    from ledger.ledger import emit  # lazy: only the throttle path touches the ledger
    emit({
        "tick_id": os.environ.get("DISPATCH_TICK_ID", "tick-unknown"),
        "issue": None,
        "stage": "throttled",
        "event": "throttled",
        "fraction": decision.get("fraction"),
        "soft_cap": decision.get("soft_cap"),
        "plan": decision.get("plan"),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dry_run": True,
    })


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Pre-flight soft-cap throttle decision (offline-capable).")
    p.add_argument("--record", action="store_true",
                   help="on throttle, also append a throttle line to the run-ledger")
    args = p.parse_args(argv)

    decision = should_throttle()
    if args.record and decision.get("throttle"):
        try:
            _record_throttle(decision)
        except Exception:  # noqa: BLE001 — ledger is best-effort observability, never break the tick
            pass
    sys.stdout.write(json.dumps(decision, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
