#!/usr/bin/env python3
"""oracle.py — usage-limit oracle adapter (E5-1 / Pillar 3, issue #38).

Per locked decision **D2**, dispatch does NOT re-implement usage tracking.
claude-monitor (Claude-Code-Usage-Monitor) already reads Claude Code's local
session JSONL under ``~/.claude`` and knows the per-5h-window token caps
(Pro 44k / Max5 88k / Max20 220k / Custom = P90 of the last 192h). This module
is a thin adapter returning a normalized **window-state** record that E5-2
(reconciliation) and E5-3 (soft-cap guard) read from.

    window_state() -> {plan, limit_tokens, used_tokens, fraction, window_start, source}

``fraction == used_tokens / limit_tokens`` clamped to ``[0.0, 1.0]`` (``0.0`` when
``limit_tokens`` is 0), rounded to 4 decimal places.

Two sources, chosen by the ``BUDGET_ORACLE_FIXTURE`` env var:

  * **fixture** (``BUDGET_ORACLE_FIXTURE`` points at a JSON file) — read the
    window-state from that fixture; ``source == "fixture"``. Makes NO network
    call, NO subprocess spawn, and does NOT read ``~/.claude``. Pure and
    deterministic: same fixture in -> byte-identical record out (same contract
    as ``approval.py``). This is the offline/smoke path.
  * **claude-monitor** (no fixture) — consume claude-monitor for the live
    global usage window; ``source == "claude-monitor"``. Plan/limit come from the
    operator-committed ``budget.window`` block in ``src/tuning.json``. Not
    exercised in smoke (no monitor installed there).

This adapter does NO reconciliation, overspend math, or P90 re-implementation
(D2) — only the custom-P90 passthrough is wired, not reimplemented.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # src/ — for tuning
import tuning  # noqa: E402

_FRACTION_DP = 4


def _clamp_fraction(used: float, limit: float) -> float:
    """fraction = used/limit, clamped to [0,1], 0.0 when limit is 0."""
    if not limit or limit <= 0:
        return 0.0
    frac = used / limit
    if frac < 0.0:
        return 0.0
    if frac > 1.0:
        return 1.0
    return round(frac, _FRACTION_DP)


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _budget_cfg() -> Dict[str, Any]:
    """The operator-committed budget block (window + plan_limits), override-aware."""
    return tuning.load().get("budget", {}) or {}


def _resolve_limit(plan: str, cfg: Dict[str, Any]) -> int:
    """Resolve the 5h-window token cap for a plan tier. The committed
    ``budget.window.window_token_limit`` (the operator's decided #48 value) wins;
    otherwise fall back to the plan_limits table, or custom_limit_tokens for the
    custom tier."""
    window = cfg.get("window", {}) or {}
    committed = window.get("window_token_limit")
    if isinstance(committed, int) and committed > 0:
        return committed
    if plan == "custom":
        return _to_int(cfg.get("custom_limit_tokens"), 0)
    limits = cfg.get("plan_limits", {}) or {}
    return _to_int(limits.get(plan), 0)


def _from_fixture(path: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        fx = json.load(fh)
    window = cfg.get("window", {}) or {}
    plan = str(fx.get("plan") or window.get("plan_tier") or "custom")
    # Fixture-provided limit wins (a complete window-state substitute); otherwise
    # resolve from the committed config so partial fixtures still work.
    limit = fx.get("limit_tokens")
    limit = _to_int(limit, _resolve_limit(plan, cfg)) if limit is not None else _resolve_limit(plan, cfg)
    used = _to_int(fx.get("used_tokens"), 0)
    return {
        "plan": plan,
        "limit_tokens": limit,
        "used_tokens": used,
        "fraction": _clamp_fraction(used, limit),
        "window_start": str(fx.get("window_start") or ""),
        "source": "fixture",
    }


def _from_claude_monitor(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Live path: consume claude-monitor for the global usage window. subprocess
    is imported lazily so the fixture path never touches it and module import is
    exec-free. Not exercised in smoke (no monitor there)."""
    import subprocess  # noqa: E402  (lazy: live path only — D2 reuse, not reimplement)

    window = cfg.get("window", {}) or {}
    plan = str(window.get("plan_tier") or "custom")
    limit = _resolve_limit(plan, cfg)
    out = subprocess.run(
        [os.environ.get("CLAUDE_MONITOR_BIN", "claude-monitor"), "--json"],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    # claude-monitor is the authority for the live used-token tally and window
    # start; defensive .get() so a schema drift degrades rather than crashes.
    used = _to_int(data.get("used_tokens") or data.get("tokens_used"), 0)
    if data.get("limit_tokens"):
        limit = _to_int(data.get("limit_tokens"), limit)
    return {
        "plan": plan,
        "limit_tokens": limit,
        "used_tokens": used,
        "fraction": _clamp_fraction(used, limit),
        "window_start": str(data.get("window_start") or ""),
        "source": "claude-monitor",
    }


def window_state() -> Dict[str, Any]:
    """Return the normalized usage-window state (see module docstring)."""
    cfg = _budget_cfg()
    fixture = os.environ.get("BUDGET_ORACLE_FIXTURE")
    if fixture:
        return _from_fixture(fixture, cfg)
    return _from_claude_monitor(cfg)


def main(argv) -> int:
    state = window_state()
    # sort_keys + fixed separators => byte-identical stdout for one fixture.
    print(json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
