#!/usr/bin/env python3
"""tuning.py — single, declarative tuning surface for the dispatch pipeline.

The values that govern WORK SELECTION (which issue is picked) and PROMPT
GENERATION (how the work order reads) used to be hardcoded constants scattered
across six modules. They now live in one editable JSON file (``tuning.json``,
next to this module) so they can be fine-tuned without touching Python.

This loader reads that file and deep-merges it over the in-code ``DEFAULTS``
below, so a missing or partial file still works and reproduces current
behavior (the committed ``tuning.json`` is identical to ``DEFAULTS``; the file
is the admin's editable override). Override the path with ``DISPATCH_TUNING_FILE``.

Deliberately exec-free — imports only ``json`` and ``os`` — so the classifier
(a quarantine reader, HANDOFF §8) can import it without gaining any capability.
Deterministic: identical config always yields identical constants.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_PATH = os.path.join(_HERE, "tuning.json")

# In-code defaults — the canonical fallback. The committed tuning.json mirrors
# these values and is the surface an admin edits; the file overrides these.
DEFAULTS: Dict[str, Any] = {
    "selection": {
        "classify": {
            "hints": {
                "xs": ["typo", "rename", "comment", "one-line", "one line",
                       "wording", "docstring", "lint", "format"],
                "s": ["add a flag", "small", "minor", "tweak", "adjust", "bump version"],
                "l": ["refactor", "redesign", "architecture", "migration",
                      "rewrite", "overhaul", "multi-service", "breaking change"],
                "wontdo": ["wontfix", "won't do", "wont do", "by design", "not planned"],
                "dup": ["duplicate", "dupe", "already reported", "same as #"],
                "vague": ["not sure", "maybe", "somehow", "investigate", "unclear",
                          "?", "thoughts", "discuss"],
            },
            "conf": {
                "base": 0.60,
                "scope_signal_weight": 0.10,
                "scope_signal_cap": 3,
                "body_long_bonus": 0.10,
                "body_long_threshold": 80,
                "vague_weight": 0.12,
                "vague_cap": 3,
                "body_short_penalty": 0.15,
                "body_short_threshold": 25,
                "clamp_min": 0.05,
                "clamp_max": 0.99,
                "round_ndigits": 4,
                "dup_cap": 0.50,
            },
            "scope_route": {"xs": "gen-local", "s": "gen-local",
                            "m": "gen-default", "l": "gen-frontier"},
        },
        "ranker": {
            "fwd_patterns": [
                r"blocked\s+by\s+#(\d+)",
                r"depends?\s+on\s+#(\d+)",
                r"requires?\s+#(\d+)",
                r"needs\s+#(\d+)",
                r"follow[-\s]?up\s+to\s+#(\d+)",
                r"prerequisite:?\s*#(\d+)",
                r"after\s+#(\d+)",
                r"^\s*[-*]\s*\[[ xX]\]\s*#(\d+)",
            ],
            "rev_patterns": [
                r"unblocks?:?\s*(?:issue\s*)?#(\d+)",
                r"blocks?\s+#(\d+)",
                r"prerequisite\s+for\s+#(\d+)",
                r"parent\s+epic:?\s*#(\d+)",
            ],
        },
    },
    "generation": {
        "approval": {
            "scope_budget": {"xs": 20000, "s": 40000, "m": 80000, "l": 160000},
            "phase_split": [["READ", 0.15], ["IMPLEMENT", 0.45],
                            ["VERIFY", 0.12], ["COMMIT & PR", 0.08]],
        },
        "decompose": {
            "swarm_max": 15,
            "specialization_rules": [
                {"any": [{"basename_contains": "smoke"}, {"path_contains": "test"},
                         {"path_contains": "/fixtures/"}],
                 "function": "engineer", "domain": "QA / test automation"},
                {"any": [{"basename_endswith": ".sh"}, {"path_startswith": "scripts/"}],
                 "function": "engineer", "domain": "developer tooling (shell)"},
                {"any": [{"path_startswith": "schemas/"},
                         {"all_of": [{"basename_endswith": ".json"}, {"path_contains": "schema"}]}],
                 "function": "engineer", "domain": "data contracts / schema"},
                {"any": [{"path_startswith": ".github/"}, {"basename_startswith": "ci"}],
                 "function": "engineer", "domain": "CI/CD"},
                {"any": [{"basename_endswith": ".md"}, {"basename_equals": "claude.md"}],
                 "function": "writer", "domain": "developer documentation"},
                {"any": [{"path_startswith": "services/intake/"}],
                 "function": "engineer", "domain": "data-pipeline (Python)"},
                {"any": [{"path_startswith": "services/classifier/"}],
                 "function": "engineer", "domain": "ML / classification (Python)"},
                {"any": [{"path_startswith": "services/models/"}],
                 "function": "engineer", "domain": "LLM integration (Python)"},
                {"any": [{"path_startswith": "services/architect/"}],
                 "function": "engineer", "domain": "orchestration (Python)"},
                {"any": [{"basename_endswith": ".py"}, {"path_startswith": "services/"}],
                 "function": "engineer", "domain": "backend (Python)"},
                {"default": True, "function": "engineer", "domain": "general software"},
            ],
        },
        "resources": {"max_files": 6, "cap_bytes": 6000,
                      "contract_cap": 4000, "schema_cap": 4000},
        "workorder": {
            "route_alias": {"gen-local": "local", "gen-default": "sonnet",
                            "gen-frontier": "opus"},
            "box_width": 70,
        },
    },
    # Budget oracle / soft-cap config (Pillar 3). The committed tuning.json holds
    # the operator-decided values (#48 via #68); these are the safe fallbacks so a
    # missing budget block still deep-merges to a usable shape.
    "budget": {
        "window": {
            "plan_tier": "max20",
            "window_token_limit": 220000,
            "soft_cap_fraction": 0.80,
        },
        "plan_limits": {"pro": 44000, "max5": 88000, "max20": 220000},
        "custom_limit_tokens": 0,
    },
}


def _merge(base: Any, ovr: Any) -> Any:
    """Deep-merge ``ovr`` over ``base``. Dicts merge; lists/scalars replace.
    Keys starting with ``_`` (documentation) are dropped from the result."""
    if isinstance(base, dict) and isinstance(ovr, dict):
        out: Dict[str, Any] = {}
        for k in base:
            if k.startswith("_"):
                continue
            out[k] = base[k] if k not in ovr else _merge(base[k], ovr[k])
        for k in ovr:                       # override-only keys
            if k.startswith("_") or k in out:
                continue
            out[k] = ovr[k]
        return out
    return ovr


def _config_path() -> str:
    return os.environ.get("DISPATCH_TUNING_FILE") or _DEFAULT_PATH


def load(path: str = "") -> Dict[str, Any]:
    """Return the merged config (file over DEFAULTS). Missing/corrupt file -> DEFAULTS."""
    path = path or _config_path()
    data: Dict[str, Any] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):           # ValueError covers JSONDecodeError
        data = {}
    return _merge(DEFAULTS, data)


# --------------------------------------------------------------------------
# Computed constants — consumers do `from tuning import SCOPE_BUDGET`, the same
# idiom as the old module-level constants, just sourced from the config.
# --------------------------------------------------------------------------
_CFG = load()
_SEL = _CFG["selection"]
_GEN = _CFG["generation"]

# selection
CLASSIFY_HINTS: Dict[str, Tuple[str, ...]] = {
    k: tuple(v) for k, v in _SEL["classify"]["hints"].items()}
CONF: Dict[str, Any] = dict(_SEL["classify"]["conf"])
SCOPE_ROUTE: Dict[str, str] = dict(_SEL["classify"]["scope_route"])
DEP_FWD: Tuple[str, ...] = tuple(_SEL["ranker"]["fwd_patterns"])
DEP_REV: Tuple[str, ...] = tuple(_SEL["ranker"]["rev_patterns"])

# generation
SCOPE_BUDGET: Dict[str, int] = {k: int(v) for k, v in _GEN["approval"]["scope_budget"].items()}
PHASE_SPLIT: Tuple[Tuple[str, float], ...] = tuple(
    (name, frac) for name, frac in _GEN["approval"]["phase_split"])
SWARM_MAX: int = int(_GEN["decompose"]["swarm_max"])
SPEC_RULES: List[Dict[str, Any]] = list(_GEN["decompose"]["specialization_rules"])
RES_CAPS: Dict[str, int] = {k: int(v) for k, v in _GEN["resources"].items()}
ROUTE_ALIAS: Dict[str, str] = dict(_GEN["workorder"]["route_alias"])
BOX_W: int = int(_GEN["workorder"]["box_width"])

# budget (Pillar 3 oracle / soft-cap) — exposed the same way as the above.
# NOTE: deliberately NOT named *_TOKEN(S) — these are token-count limits, not
# secrets, and the smoke §7.5 secret-scan keys off a *_TOKEN/_KEY/_SECRET regex.
_BUD = _CFG.get("budget", {})
_BUD_WINDOW = _BUD.get("window", {})
BUDGET_PLAN_TIER: str = str(_BUD_WINDOW.get("plan_tier", "max20"))
BUDGET_WINDOW_LIMIT: int = int(_BUD_WINDOW.get("window_token_limit") or 0)
BUDGET_SOFT_CAP_FRACTION: float = float(_BUD_WINDOW.get("soft_cap_fraction", 0.80))
BUDGET_PLAN_LIMITS: Dict[str, int] = {k: int(v) for k, v in (_BUD.get("plan_limits") or {}).items()}
BUDGET_CUSTOM_LIMIT: int = int(_BUD.get("custom_limit_tokens") or 0)


# --------------------------------------------------------------------------
# Specialization rule evaluator (used by decompose). First matching rule wins;
# conditions in a rule's "any" list are OR'd; an "all_of" condition is an AND.
# Matching is against the lowercased repo-relative path and its basename.
# --------------------------------------------------------------------------
def _cond(cond: Dict[str, Any], path: str, base: str) -> bool:
    for k, v in cond.items():
        if k == "all_of":
            return all(_cond(c, path, base) for c in v)
        if k == "basename_contains":
            return v in base
        if k == "basename_endswith":
            return base.endswith(v)
        if k == "basename_startswith":
            return base.startswith(v)
        if k == "basename_equals":
            return base == v
        if k == "path_contains":
            return v in path
        if k == "path_startswith":
            return path.startswith(v)
    return False


def spec_for(path: str) -> Tuple[str, str]:
    """Map a file/area to a (function, domain) specialization per SPEC_RULES."""
    p = path.lower()
    base = os.path.basename(p)
    for rule in SPEC_RULES:
        if rule.get("default"):
            return rule["function"], rule["domain"]
        if any(_cond(c, p, base) for c in rule.get("any", [])):
            return rule["function"], rule["domain"]
    return "engineer", "general software"
