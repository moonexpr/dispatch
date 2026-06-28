#!/usr/bin/env python3
"""bindings.py — the WebsiteWF bind layer.

WebsiteWF is its own engine, a sibling of ``src/baseworkflow`` that *inherits
everything baseworkflow builds*: :func:`build_registry` starts from baseworkflow's
fully-populated :class:`~engine.workflow.TokenRegistry` and registers the web-only
action bodies on top, so every base token (``architect:*``, ``engineer:*``,
``admin:*`` …) still resolves and only the ``web:*`` tokens are added.

The bodies are pure ``fn(inputs) -> {out_alias: value}`` (the compiler wires their
declared interface I/O around them) and deterministic — no model, no network — so
they run identically under the mock and real factories (mock-safe for the e2e).

Validate the overlay against this registry:
  ``python3 -m engine.workflow app/workflows/websitewf.yml \\
    --registry src.websitewf.bindings:build_registry``
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict

# Repo root + src/ on sys.path so ``src.*`` and ``engine.*`` resolve regardless of
# how this module is imported (mirrors src/baseworkflow's convention).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SRC)
for _p in (_ROOT, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine.workflow import TokenRegistry  # noqa: E402


def _field(job: Any, key: str, default: str = "") -> Any:
    return job.get(key, default) if isinstance(job, dict) else default


# -- web action bodies (pure: inputs -> {out_alias: value}) ------------------
def generate_route_work_units(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A1 (web): the base work-unit classification *plus* an authored route spec.

    Composes baseworkflow's ``generate_work_units`` body (so ``purpose`` /
    ``work_unit`` keep the exact shape downstream base actions expect) and adds
    ``web_route_spec`` (framework + path) for the proxy and the scaffold task."""
    from src.baseworkflow.bindings.github import generate_work_units as _base

    out = dict(_base(inputs))
    job = inputs.get("job") or {}
    out["web_route_spec"] = {
        "framework": _field(job, "framework", "nextjs"),
        "path": _field(job, "route", "/new-page"),
        "title": _field(job, "title", "Add a page"),
    }
    return out


def classify_route_addendum(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """A2 (web addendum): tag the target framework + route path onto the strategy
    the base classifier produced (augment, preserving the base output)."""
    strategy = inputs.get("strategy") or {}
    spec = inputs.get("web_route_spec") or {}
    out = dict(strategy) if isinstance(strategy, dict) else {"value": strategy}
    out["framework"] = spec.get("framework")
    out["route_path"] = spec.get("path")
    return {"strategy": out}


def scaffold_route(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Lay down + wire the page/route scaffold from the authored route spec."""
    spec = inputs.get("web_route_spec") or {}
    path = spec.get("path", "/new-page")
    framework = spec.get("framework", "nextjs")
    page_file = f"app{path}/page.tsx" if framework == "nextjs" else f"resources/views{path}.blade.php"
    return {
        "scaffold_result": {
            "framework": framework,
            "path": path,
            "files": [page_file],
            "status": "scaffolded",
        }
    }


def register(reg: TokenRegistry) -> None:
    """Register the ``web:*`` action bodies onto an existing registry."""
    reg.register_action("generate_route_work_units", generate_route_work_units)
    reg.register_action("classify_route_addendum", classify_route_addendum)
    reg.register_action("scaffold_route", scaffold_route)


def build_registry() -> TokenRegistry:
    """A registry that inherits all of baseworkflow's binds, plus the ``web:*``
    bodies. This is the WebsiteWF engine's registry."""
    from src.baseworkflow.bindings import build_registry as _base_build_registry

    reg = _base_build_registry()
    register(reg)
    return reg
