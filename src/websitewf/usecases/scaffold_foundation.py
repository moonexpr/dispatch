#!/usr/bin/env python3
"""scaffold_foundation.py — WebsiteWF use-case bindings for
"scaffold a website foundation (Next.js or Laravel)" (#167).

Disjoint from the proof vertical's src/websitewf/bindings.py. build_registry()
inherits baseworkflow's fully-populated TokenRegistry and registers only THIS use
case's web:* bodies on top. Bodies are pure fn(inputs)->{out_alias: value},
deterministic (no model, no network) so they run identically under mock and real.

Shape (per docs/adr/002 + the #167 use-case guide):
  * EXTEND architect:classify_strategy (mode: after) with
    web:classify_foundation_addendum — picks/records the framework (next|laravel)
    from job/triage onto the strategy AND authors the foundation SPEC
    {framework, dir_layout, key_files} in the spec phase, so the proxy can feed it
    to the engineer (the spec is authored upstream, like the proof vertical's
    web_route_spec).
  * ADD web:scaffold_foundation into `work`, after engineer:execute_orchestration —
    finalizes the foundation SPEC deliverable against the engineering result.
  * PROXY engineer:execute_orchestration — feeds the foundation spec in and mirrors
    its result to a web-scoped key (wiring lives in the overlay).

Validate:
  python3 -m engine.workflow app/workflows/websitewf-scaffold-foundation.yml \
    --registry src.websitewf.usecases.scaffold_foundation:build_registry
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict

# Repo root + src/ on sys.path so src.* and engine.* resolve regardless of import path.
_HERE = os.path.dirname(os.path.abspath(__file__))            # .../src/websitewf/usecases
_WEBSITEWF = os.path.dirname(_HERE)                           # .../src/websitewf
_SRC = os.path.dirname(_WEBSITEWF)                            # .../src
_ROOT = os.path.dirname(_SRC)                                 # repo root
for _p in (_ROOT, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine.workflow import TokenRegistry  # noqa: E402

# The two frameworks this use case can scaffold; anything else falls back to next.
_FRAMEWORKS = ("next", "laravel")


def _field(job: Any, key: str, default: Any = "") -> Any:
    return job.get(key, default) if isinstance(job, dict) else default


def _pick_framework(job: Any, triage: Any) -> str:
    """Deterministically pick next|laravel from job/triage signals, default next."""
    for src in (job, triage):
        val = _field(src, "framework", "")
        if isinstance(val, str) and val.lower() in _FRAMEWORKS:
            return val.lower()
    return "next"


def _dir_layout(framework: str) -> Dict[str, Any]:
    """The canonical top-level directory layout for the chosen framework."""
    if framework == "laravel":
        return {
            "app": "app/",
            "routes": "routes/",
            "views": "resources/views/",
            "config": "config/",
            "public": "public/",
        }
    # next (default)
    return {
        "app": "app/",
        "components": "components/",
        "public": "public/",
        "styles": "styles/",
        "config": "next.config.js",
    }


def _key_files(framework: str) -> list[str]:
    """The minimal set of files a freshly scaffolded foundation must contain."""
    if framework == "laravel":
        return ["composer.json", "artisan", "routes/web.php", "app/Http/Kernel.php"]
    return ["package.json", "next.config.js", "app/layout.tsx", "app/page.tsx"]


# -- web action bodies (pure: inputs -> {out_alias: value}) ------------------

def _foundation_spec(framework: str) -> Dict[str, Any]:
    """The deterministic foundation spec for the chosen framework."""
    return {
        "framework": framework,
        "dir_layout": _dir_layout(framework),
        "key_files": _key_files(framework),
    }


# EXTEND body — MUST match architect:classify_strategy's I/O contract
# (in strategy: deliverables.strategy, out strategy: deliverables.strategy), then
# augment with the chosen framework, preserving the base strategy shape. It ALSO
# authors the foundation_spec in the spec phase so the proxy can feed it to the
# engineer downstream (mirrors how the proof vertical authors web_route_spec upstream).
def classify_foundation_addendum(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Pick/record the framework (next|laravel) from job/triage onto the strategy
    the base classifier produced, and author the foundation spec for the engineer."""
    strategy = inputs.get("strategy") or {}
    job = inputs.get("job") or {}
    triage = inputs.get("triage") or {}
    framework = _pick_framework(job, triage)
    out = dict(strategy) if isinstance(strategy, dict) else {"value": strategy}
    out["framework"] = framework
    return {"strategy": out, "foundation_spec": _foundation_spec(framework)}


# ADD-task body: finalizes the foundation SPEC deliverable against the engineering
# result. The live engineer does the real codegen; this is a deterministic spec
# procedure (no model, no network) that confirms the authored foundation.
def scaffold_foundation(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Finalize the foundation spec {framework, dir_layout, key_files}.

    Reads the foundation spec authored by the classify addendum (falling back to the
    framework recorded on the strategy, then to job/triage signals)."""
    spec = inputs.get("foundation_spec") or {}
    if isinstance(spec, dict) and spec.get("framework") in _FRAMEWORKS:
        framework = spec["framework"]
    else:
        strategy = inputs.get("strategy") or {}
        job = inputs.get("job") or {}
        triage = inputs.get("triage") or {}
        framework = (
            strategy.get("framework")
            if isinstance(strategy, dict) and strategy.get("framework") in _FRAMEWORKS
            else _pick_framework(job, triage)
        )
    return {"foundation_spec": _foundation_spec(framework)}


def register(reg: TokenRegistry) -> None:
    """Register THIS use case's web:* bodies onto an existing registry.
    The bind name (1st arg) MUST equal the manifest's `bind:` field."""
    reg.register_action("scaffold_foundation", scaffold_foundation)
    reg.register_action("classify_foundation_addendum", classify_foundation_addendum)


def build_registry() -> TokenRegistry:
    """Inherit every baseworkflow bind, plus THIS use case's web:* bodies."""
    from src.baseworkflow.bindings import build_registry as _base_build_registry

    reg = _base_build_registry()
    register(reg)
    return reg
