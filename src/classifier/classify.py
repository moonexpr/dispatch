#!/usr/bin/env python3
"""classify.py — issue triage classifier (HANDOFF §5.3, v0).

Maps a GitHub issue (title, body) to a routing decision:

    TriageResult{ action, scope, route, confidence }

Uses a deterministic, network-free, keyword-based classifier.
Same signature across runs — identical (title, body) always yields
identical output (HANDOFF §7.2).

SECURITY — QUARANTINE READER (HANDOFF §8)
-----------------------------------------
This module reads UNTRUSTED issue text. It is the pipeline's quarantine
reader *by design* and MUST NEVER gain tool/exec capability. It therefore
imports nothing that can run commands (no ``subprocess``, ``os.system``,
``eval``, ``exec``, ``pickle``). It only:
  * reads input via arguments,
  * emits a JSON object on stdout.
Issue text is data — never interpreted as code or instructions.

Usage
-----
    python3 classify.py --title "..." --body "..."
    python3 classify.py --issue-json fixtures/issue_1.json
    # -> {"action": "...", "scope": "...", "route": "...", "confidence": 0.0}

Exit codes: 0 success; 2 bad args.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import List, Tuple

# Tunables (hint lists, confidence weights, scope->route) live in the shared
# declarative config. tuning.py is exec-free, so importing it preserves this
# module's quarantine-reader posture (HANDOFF §8 asserts no exec capability).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tuning  # noqa: E402

# --- Schema (HANDOFF §5.3) -------------------------------------------------
# "decompose" = an [Epic]/container issue: not an atomic unit of work; selection
# expands it into its constituent sub-issues rather than dispatching it directly.
ACTIONS = ("implement", "needs-human", "wont-do", "duplicate?", "decompose")
SCOPES = ("xs", "s", "m", "l")
ROUTES = ("gen-local", "gen-default", "gen-frontier")

# scope -> route map (mirrors scripts/lib/common.sh route_for_scope).
# Tunable via app/config/tuning.yml (selection.classify.scope_route).
_SCOPE_ROUTE = tuning.SCOPE_ROUTE


@dataclass(frozen=True)
class TriageResult:
    action: str       # one of ACTIONS
    scope: str        # one of SCOPES  (drives route)
    route: str        # one of ROUTES
    confidence: float  # 0.0 - 1.0

    def validate(self) -> "TriageResult":
        assert self.action in ACTIONS, f"bad action: {self.action}"
        assert self.scope in SCOPES, f"bad scope: {self.scope}"
        assert self.route in ROUTES, f"bad route: {self.route}"
        assert 0.0 <= self.confidence <= 1.0, f"bad confidence: {self.confidence}"
        return self


# --- Deterministic classifier -----------------------------------------------
# Keyword signals are intentionally simple and fully deterministic: identical
# (title, body) always yields identical output (HANDOFF §7.2). The signal lists
# are tunable via app/config/tuning.yml (selection.classify.hints).
_XS_HINTS = tuning.CLASSIFY_HINTS["xs"]
_S_HINTS = tuning.CLASSIFY_HINTS["s"]
_L_HINTS = tuning.CLASSIFY_HINTS["l"]
_WONTDO_HINTS = tuning.CLASSIFY_HINTS["wontdo"]
_DUP_HINTS = tuning.CLASSIFY_HINTS["dup"]
_VAGUE_HINTS = tuning.CLASSIFY_HINTS["vague"]


def _norm(title: str, body: str) -> str:
    return f"{title}\n{body}".lower()


def _is_epic(title: str) -> bool:
    """An ``[Epic]`` container/tracker issue — not a single unit of work.

    Detected structurally from the title prefix (the GitHub convention) rather
    than by keyword, so a child issue that merely *cites* its parent (e.g.
    ``**Parent epic:** #1`` in the body) is never misread as an epic itself.
    """
    return title.strip().lower().startswith("[epic]")


def classify(title: str, body: str) -> TriageResult:
    """Deterministic, network-free classification."""
    text = _norm(title, body)

    def has(hints: Tuple[str, ...]) -> int:
        return sum(1 for h in hints if h in text)

    # Scope: large hints dominate, then xs, then s, default m.
    if has(_L_HINTS):
        scope = "l"
    elif has(_XS_HINTS):
        scope = "xs"
    elif has(_S_HINTS):
        scope = "s"
    else:
        scope = "m"

    # Action. An [Epic] is a container of sub-issues, not an atomic deliverable:
    # route it to decomposition (selection expands it into its constituents)
    # before any keyword triage, since the structural signal is authoritative.
    if _is_epic(title):
        action = "decompose"
    elif has(_DUP_HINTS):
        action = "duplicate?"
    elif has(_WONTDO_HINTS):
        action = "wont-do"
    else:
        action = "implement"

    # Confidence: start from a base and add/subtract deterministic signals.
    # Weights/thresholds are tunable via app/config/tuning.yml
    # (selection.classify.conf).
    c = tuning.CONF
    vague = has(_VAGUE_HINTS)
    body_len = len((body or "").strip())
    conf = c["base"]
    conf += c["scope_signal_weight"] * min(
        has(_XS_HINTS) + has(_S_HINTS) + has(_L_HINTS), c["scope_signal_cap"])
    conf += c["body_long_bonus"] if body_len >= c["body_long_threshold"] else 0.0
    conf -= c["vague_weight"] * min(vague, c["vague_cap"])
    conf -= c["body_short_penalty"] if body_len < c["body_short_threshold"] else 0.0
    conf = max(c["clamp_min"], min(c["clamp_max"], round(conf, c["round_ndigits"])))

    # An uncertain duplicate signal collapses confidence (human should confirm).
    if action == "duplicate?":
        conf = min(conf, c["dup_cap"])

    return TriageResult(action, scope, _SCOPE_ROUTE[scope], conf).validate()


# --- CLI -------------------------------------------------------------------
def _load_issue_json(path: str) -> Tuple[str, str]:
    with open(path, "r", encoding="utf-8") as fh:
        obj = json.load(fh)
    return str(obj.get("title", "")), str(obj.get("body", ""))


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="Triage classifier (HANDOFF §5.3)")
    p.add_argument("--title", default=None)
    p.add_argument("--body", default=None)
    p.add_argument("--issue-json", default=None,
                   help="path to a JSON file with {title, body}")
    args = p.parse_args(argv)

    if args.issue_json:
        title, body = _load_issue_json(args.issue_json)
    else:
        if args.title is None:
            p.error("provide --title/--body or --issue-json")
        title, body = args.title, (args.body or "")

    result = classify(title, body)
    # Stable key order so output is byte-identical across runs (determinism).
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
