#!/usr/bin/env python3
"""backend_feature.py — WebsiteWF use-case bindings for "build new web technology
that requires backend support" (#169).

Disjoint from the proof vertical's websitewf/bindings.py. ``build_registry()``
inherits baseworkflow's fully-populated :class:`~foundation.workflow.TokenRegistry` and
registers only THIS use case's ``web:*`` bodies on top. Bodies are pure
``fn(inputs) -> {out_alias: value}``, deterministic (no model, no network) so they
run identically under the mock and real factories. The single web action here,
``web:scaffold_backend``, authors a backend contract spec (API routes + data model
+ server logic) onto ``deliverables.backend_spec``; the live engineer does the
actual codegen (the overlay PROXies the engineer's I/O to source the orchestration
script from that spec), and admin opens the PR.

Validate:
  python3 -m foundation.workflow app/workflows/websitewf-backend-feature.yml \
    --registry websitewf.usecases.backend_feature:build_registry
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


# -- web action bodies (pure: inputs -> {out_alias: value}) ------------------
def scaffold_backend(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Author a backend contract SPEC for a web feature that needs backend support.

    Deterministically derives the contract — API route(s), data model, server
    logic — from the job, with sane defaults so a bare job still yields a
    well-formed spec. Writes only to the deliverables shelf (no codegen here): the
    live engineer turns this contract into code, fed it via the overlay's proxy."""
    job = inputs.get("job") or {}
    feature = _field(job, "feature", _field(job, "title", "new-feature"))
    resource = _field(job, "resource", "item")
    base_route = _field(job, "api_route", f"/api/{resource}")
    framework = _field(job, "framework", "nextjs")
    return {
        "backend_spec": {
            "feature": feature,
            "framework": framework,
            "api_routes": [
                {"method": "GET", "path": base_route},
                {"method": "POST", "path": base_route},
            ],
            "data_model": {
                "name": resource,
                "fields": _field(job, "fields", ["id", "created_at"]),
            },
            "server_logic": (
                f"Handle CRUD for {resource} behind {base_route}; "
                "validate input, persist via the data model, return JSON."
            ),
        }
    }


def register(reg: TokenRegistry) -> None:
    """Register THIS use case's ``web:*`` bodies onto an existing registry. The bind
    name (1st arg) MUST equal the manifest's ``bind:`` field."""
    reg.register_action("scaffold_backend", scaffold_backend)


def build_registry() -> TokenRegistry:
    """A registry that inherits every baseworkflow bind, plus THIS use case's
    ``web:*`` bodies."""
    from baseworkflow.bindings import build_registry as _base_build_registry

    reg = _base_build_registry()
    register(reg)
    return reg
