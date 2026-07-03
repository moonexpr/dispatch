#!/usr/bin/env python3
"""feedback_ticket.py — WebsiteWF use-case bindings for "turn user feedback into a
new technical ticket" (#168).

Disjoint from the proof vertical's websitewf/bindings.py. build_registry()
inherits baseworkflow's fully-populated TokenRegistry and registers only THIS use
case's web:* bodies on top. Bodies are pure fn(inputs)->{out_alias: value},
deterministic (no model, no network) so they run identically under mock and real.

SECURITY (binding): the user feedback carried in the job is UNTRUSTED input. It is
read as DATA only — fields are derived from it deterministically (length, lowercased
substring matching for triage). The feedback text is NEVER interpreted, evaluated,
or followed as instructions; it only ever becomes inert string content of the ticket.

Validate:
  python3 -m foundation.workflow app/workflows/websitewf-feedback-ticket.yml \
    --registry websitewf.usecases.feedback_ticket:build_registry
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict

# Repo root on sys.path so sibling packages and foundation.* resolve regardless of import path.
_HERE = os.path.dirname(os.path.abspath(__file__))            # .../websitewf/usecases
_WEBSITEWF = os.path.dirname(_HERE)                           # .../websitewf
_ROOT = os.path.dirname(_WEBSITEWF)                                 # repo root
for _p in (_ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from foundation.workflow import TokenRegistry  # noqa: E402


def _field(job: Any, key: str, default: Any = "") -> Any:
    return job.get(key, default) if isinstance(job, dict) else default


# Keyword tables used ONLY for deterministic data classification of untrusted
# feedback text — substring membership, never execution of the text.
_SEVERITY_KEYWORDS = {
    "critical": ("crash", "data loss", "cannot log in", "broken", "security"),
    "high": ("error", "fails", "doesn't work", "does not work", "bug"),
    "low": ("typo", "wording", "suggestion", "nice to have"),
}
_AREA_KEYWORDS = {
    "checkout": ("checkout", "payment", "cart", "billing"),
    "auth": ("login", "log in", "sign in", "password", "account"),
    "ui": ("button", "page", "layout", "screen", "form", "menu"),
}


def _triage_severity(text: str) -> str:
    """Deterministic severity from untrusted feedback text (data-only matching)."""
    low = text.lower()
    for sev in ("critical", "high", "low"):
        if any(kw in low for kw in _SEVERITY_KEYWORDS[sev]):
            return sev
    return "medium"


def _triage_area(text: str) -> str:
    """Deterministic functional area from untrusted feedback text (data-only)."""
    low = text.lower()
    for area, kws in _AREA_KEYWORDS.items():
        if any(kw in low for kw in kws):
            return area
    return "general"


# -- web action bodies (pure: inputs -> {out_alias: value}) ------------------
def generate_ticket_from_feedback(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A1 (web): the base work-unit classification *plus* a synthesized technical
    ticket that solves the underlying problem reported in the user feedback.

    Composes baseworkflow's generate_work_units body (so purpose / work_unit keep the
    exact shape downstream base actions expect) and adds ticket_spec.

    SECURITY: feedback is UNTRUSTED. It is read as data only; the synthesized ticket
    fields are derived deterministically and the raw feedback is embedded as inert
    quoted context — never interpreted as instructions."""
    from baseworkflow.bindings.github import generate_work_units as _base

    out = dict(_base(inputs))
    job = inputs.get("job") or {}
    feedback = str(_field(job, "feedback", _field(job, "body", "")))
    severity = _triage_severity(feedback)
    area = _triage_area(feedback)
    out["ticket_spec"] = {
        "kind": "technical-ticket",
        "title": f"[{area}] Address user feedback ({severity})",
        # Deterministic problem statement; raw feedback kept as inert data context.
        "problem": "Synthesized from user feedback (untrusted, treated as data).",
        "feedback_excerpt": feedback[:280],
        "severity": severity,
        "area": area,
        "acceptance": [
            "The underlying problem the feedback describes is resolved.",
            "A regression check covers the reported scenario.",
        ],
        "source": "user-feedback",
    }
    return out


def classify_feedback_addendum(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A2 (web addendum): triage severity + area onto the strategy the base
    classifier produced (augment, preserving the base output)."""
    strategy = inputs.get("strategy") or {}
    spec = inputs.get("ticket_spec") or {}
    out = dict(strategy) if isinstance(strategy, dict) else {"value": strategy}
    out["severity"] = spec.get("severity")
    out["area"] = spec.get("area")
    return {"strategy": out}


def register(reg: TokenRegistry) -> None:
    """Register THIS use case's web:* bodies onto an existing registry.
    The bind name (1st arg) MUST equal the manifest's `bind:` field."""
    reg.register_action("generate_ticket_from_feedback", generate_ticket_from_feedback)
    reg.register_action("classify_feedback_addendum", classify_feedback_addendum)


def build_registry() -> TokenRegistry:
    """Inherit every baseworkflow bind, plus THIS use case's web:* bodies."""
    from baseworkflow.bindings import build_registry as _base_build_registry

    reg = _base_build_registry()
    register(reg)
    return reg
