#!/usr/bin/env python3
"""__main__.py — the operator-facing "test your YAML" CLI.

    python -m foundation.workflow <workflow.yml> [--registry module:function]
                                  [--services module:function] [--needs]

Loads + validates a workflow YAML and prints ``[PASS]`` or the located error list
with a non-zero exit. Structure, manifest shape and the data-flow contract are
always checked; pass ``--registry`` (a ``module:function`` returning a
``TokenRegistry``) to additionally verify every action bind, loop predicate and
controller reference is registered; pass ``--services`` (a ``module:function``
returning a ``ServiceRegistry``) to additionally verify needs satisfaction —
every declared controller's non-optional needs met by the environment's
provisions (ADR-003). ``--needs`` prints the per-controller needs report (each
need annotated with its satisfying provision, or ``UNMET``). Registries are
imported dynamically by name, so this CLI does not hard-wire any caller import —
``foundation`` stays a leaf.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from typing import List, Optional

from . import SchemaError, load_workflow, needs_report, validate
from .manifest import ManifestError


def _load_builder(spec: str, default_fn: str):
    mod_name, _, fn_name = spec.partition(":")
    module = importlib.import_module(mod_name)
    return getattr(module, fn_name or default_fn)()


def _print_needs(report: dict) -> None:
    print(f"needs — workflow {report['workflow']!r}")
    for cname, entry in report["controllers"].items():
        if not entry.get("registered"):
            print(f"  controller {cname}: NOT REGISTERED")
            continue
        print(f"  controller {cname}:")
        for n in entry["needs"] or [{"need": "(none)", "optional": False, "satisfied_by": ""}]:
            mark = n["satisfied_by"] if n["satisfied_by"] else ("optional, unprovided" if n["optional"] else "UNMET")
            print(f"    - {n['need']}  ←  {mark}")
    for token, needs in report["actions"].items():
        print(f"  action {token}:")
        for n in needs:
            mark = n["satisfied_by"] if n["satisfied_by"] else ("optional, unprovided" if n["optional"] else "UNMET")
            print(f"    - {n['need']}  ←  {mark}")
    if report["satisfied"] is not None:
        verdict = "SATISFIED" if report["satisfied"] else f"UNSATISFIED ({', '.join(report['unmet'])})"
        print(f"  workflow contract: {verdict}")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="foundation.workflow", description="Validate a workflow YAML (format test).")
    p.add_argument("workflow", help="path to the workflow YAML")
    p.add_argument("--registry", default="", help="module:function returning a TokenRegistry (token/predicate/controller checks)")
    p.add_argument("--services", default="", help="module:function returning a ServiceRegistry (needs-satisfaction check)")
    p.add_argument("--needs", action="store_true", help="print the per-controller needs report")
    args = p.parse_args(argv)

    try:
        node = load_workflow(args.workflow)
    except (SchemaError, ManifestError) as exc:
        print(f"[FAIL] load error: {exc}")
        return 1

    registry = None
    if args.registry:
        try:
            registry = _load_builder(args.registry, "build_registry")
        except Exception as exc:  # noqa: BLE001 — diagnostic only; degrade to structure-only
            print(f"[WARN] could not import registry {args.registry!r}: {exc}")

    services = None
    if args.services:
        try:
            services = _load_builder(args.services, "build_services")
        except Exception as exc:  # noqa: BLE001 — diagnostic only; degrade to no needs check
            print(f"[WARN] could not import services {args.services!r}: {exc}")

    errors = validate(node, registry, services=services)
    if args.needs:
        _print_needs(needs_report(node, registry, services))
    if errors:
        print(f"[FAIL] {len(errors)} validation error(s):")
        for e in errors:
            print(f"  - {e}")
        return 1

    scope = "structure + data-flow"
    if registry:
        scope += " + tokens"
    if services:
        scope += " + needs"
    print(f"[PASS] {args.workflow}  ✓  ({scope}; {len(node.phases)} phases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
