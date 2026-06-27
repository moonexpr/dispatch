#!/usr/bin/env python3
"""workplan_config.py — shared loader for the data-driven work-plan rules.

ALL rules/content (purpose taxonomy, keyword rules, harness names + rationales,
strategy layouts, …) live in ``app/config/workplan-rules.yml``, read through the
``engine.filesys`` facade. This module is the load seam ONLY — it holds no
editorial content, so the rules can be retuned without touching Python.
Consumed by purpose.py + strategy.py.

Resolution: ``$DISPATCH_WORKPLAN_RULES`` (override; honoured as-is by filesys
when absolute) -> the committed ``app/config/workplan-rules.yml`` -> a minimal,
CONTENT-FREE structural skeleton (``_SKELETON``) that only keeps the shape so
``.get()`` chains never crash. If the YAML is missing the system degrades to
"new-feature / sequential / no harness text" rather than carrying a hidden
second copy of the content. ``safe_load`` only — no arbitrary-tag exec.

Pure and deterministic: no network, no model, no wall-clock.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))  # repo root (src/architect -> ..)
# Make `from engine import filesys` importable when load() runs, regardless of
# how this module was reached. Path-only (no import side effects) so the
# degrade-to-skeleton contract below still holds if engine/PyYAML are absent.
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Default config location, relative to the app dir. An absolute
# DISPATCH_WORKPLAN_RULES override is honoured as-is by engine.filesys.resolve.
_RULES_REL = "config/workplan-rules.yml"

# CONTENT-FREE structural skeleton. Holds NO tunable content (no purposes,
# keywords, harness names, rationales, or summaries — those live ONLY in the
# YAML). Just enough shape that a missing/corrupt YAML degrades instead of
# crashing. `default` is a bare sentinel, not editorial content.
_SKELETON: Dict[str, Any] = {
    "purpose": {"labels": {}, "keyword_rules": [], "default": "new-feature", "harness": {}},
    "strategy": {"dynamic_purposes": [], "swarm_min_parallel_units": 2, "layouts": {}},
    "issues_affected": {"include_dag_neighbours": True},
}


def load() -> Dict[str, Any]:
    """Return the work-plan rules dict (override > YAML > content-free skeleton)."""
    try:
        from engine import filesys  # lazy: missing engine/PyYAML -> skeleton
        path = os.environ.get("DISPATCH_WORKPLAN_RULES") or _RULES_REL
        data = filesys.read_yaml(path)
        if not isinstance(data, dict):
            raise ValueError("workplan-rules.yml is not a mapping")
        # Shallow-merge top-level sections over the skeleton so a partial YAML
        # still resolves (the YAML supplies all real content).
        merged = {k: v for k, v in _SKELETON.items()}
        for key, val in data.items():
            merged[key] = val
        return merged
    except Exception:
        return _SKELETON
