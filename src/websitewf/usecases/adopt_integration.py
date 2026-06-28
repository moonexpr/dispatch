#!/usr/bin/env python3
"""adopt_integration.py — WebsiteWF use-case bindings for
"adopt existing / third-party technology (e.g. Stripe) into an application" (#170).

Disjoint from the proof vertical's src/websitewf/bindings.py. build_registry()
inherits baseworkflow's fully-populated TokenRegistry and registers only THIS use
case's web:* bodies on top. Bodies are pure fn(inputs)->{out_alias: value},
deterministic (no model, no network) so they run identically under mock and real.
The live engineer does the actual codegen; these procedures only author the SPEC.

SECURITY (binding): secrets live ONLY in env. The adoption spec emits the env-var
NAMES an integration needs (e.g. STRIPE_SECRET_KEY) — never secret VALUES. Do NOT
read os.environ for the secret itself, and never write a secret value into the spec,
code, fixtures, or PR text. The spec names env vars only.

Validate:
  python3 -m engine.workflow app/workflows/websitewf-adopt-integration.yml \
    --registry src.websitewf.usecases.adopt_integration:build_registry
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


def _field(job: Any, key: str, default: Any = "") -> Any:
    return job.get(key, default) if isinstance(job, dict) else default


# Known adoption targets -> the deterministic spec for adopting them. Each entry
# names the env vars the integration needs by NAME ONLY (never values — see the
# module docstring's security note).
_INTEGRATIONS: Dict[str, Dict[str, Any]] = {
    "stripe": {
        "sdk": "stripe",
        "config": {"language": "node", "package": "stripe"},
        "env_var_names": ["STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET"],
        "glue_points": ["lib/stripe.ts", "app/api/checkout/route.ts", "app/api/webhook/route.ts"],
    },
    "auth0": {
        "sdk": "@auth0/nextjs-auth0",
        "config": {"language": "node", "package": "@auth0/nextjs-auth0"},
        "env_var_names": ["AUTH0_SECRET", "AUTH0_BASE_URL", "AUTH0_CLIENT_ID", "AUTH0_CLIENT_SECRET"],
        "glue_points": ["lib/auth0.ts", "app/api/auth/[...auth0]/route.ts"],
    },
}


# -- web action bodies (pure: inputs -> {out_alias: value}) ------------------
def _author_integration_spec(job: Any) -> Dict[str, Any]:
    """Deterministically derive the adoption spec from the job. NAMES-ONLY for env
    vars — never secret values (see the module security note)."""
    target = str(_field(job, "integration", "stripe")).lower()
    base = _INTEGRATIONS.get(target)
    if base is None:
        # Unknown target: still emit a well-formed (generic) spec naming the
        # conventional env var, never a value.
        env_name = f"{target.upper().replace('-', '_')}_API_KEY" if target else "INTEGRATION_API_KEY"
        base = {
            "sdk": target or "third-party-sdk",
            "config": {"language": _field(job, "language", "node")},
            "env_var_names": [env_name],
            "glue_points": [f"lib/{target or 'integration'}.ts"],
        }
    return {
        "target": target,
        "sdk": base["sdk"],
        "config": base["config"],
        # NAMES ONLY — never secret values. See the security note above.
        "env_var_names": list(base["env_var_names"]),
        "glue_points": list(base["glue_points"]),
        "title": _field(job, "title", f"Adopt {target}"),
    }


def adopt_integration(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """ADD-task body: writes/refreshes the adoption SPEC deliverable describing how
    to bring a third-party technology into the app — SDK/library choice, config, the
    env-var NAMES it requires, and the glue points to wire it in.

    SECURITY: env_var_names lists NAMES ONLY. This body never reads a secret value
    from the environment and never emits a secret value into the spec. The live
    engineer reads the actual secret from env at runtime; the spec only declares
    which env vars must be set."""
    job = inputs.get("job") or {}
    return {"integration_spec": _author_integration_spec(job)}


def classify_integration_addendum(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """EXTEND addendum (runs in the spec phase, AFTER architect:classify_strategy):
    plan the adoption. It tags the integration target onto the strategy the base
    classifier produced (preserving the base output shape) AND authors the
    integration_spec early — upstream of the engineer — so the proxy can source the
    engineer's orchestration_script from it, and the post-engineer add task can
    refresh it."""
    strategy = inputs.get("strategy") or {}
    job = inputs.get("job") or {}
    out = dict(strategy) if isinstance(strategy, dict) else {"value": strategy}
    target = str(_field(job, "integration", "stripe")).lower()
    out["integration_target"] = target
    out["adopt_known_sdk"] = target in _INTEGRATIONS
    return {"strategy": out, "integration_spec": _author_integration_spec(job)}


def register(reg: TokenRegistry) -> None:
    """Register THIS use case's web:* bodies onto an existing registry.
    The bind name (1st arg) MUST equal the manifest's `bind:` field."""
    reg.register_action("adopt_integration", adopt_integration)
    reg.register_action("classify_integration_addendum", classify_integration_addendum)


def build_registry() -> TokenRegistry:
    """Inherit every baseworkflow bind, plus THIS use case's web:* bodies."""
    from src.baseworkflow.bindings import build_registry as _base_build_registry

    reg = _base_build_registry()
    register(reg)
    return reg
