#!/usr/bin/env python3
"""__main__.py — the operator-facing "test your YAML" CLI.

    python -m foundation.workflow <workflow.yml> [--registry module:function]

Loads + validates a workflow YAML and prints ``[PASS]`` or the located error list
with a non-zero exit. Structure, manifest shape and the data-flow contract are
always checked; pass ``--registry`` (a ``module:function`` returning a
``TokenRegistry``) to additionally verify every action bind and loop predicate is
registered. The registry is imported dynamically by name, so this CLI does not
hard-wire any caller import — ``foundation`` stays a leaf.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from typing import List, Optional

from . import SchemaError, load_workflow, validate
from .manifest import ManifestError


def _load_registry(spec: str):
    mod_name, _, fn_name = spec.partition(":")
    module = importlib.import_module(mod_name)
    return getattr(module, fn_name or "build_registry")()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="foundation.workflow", description="Validate a workflow YAML (format test).")
    p.add_argument("workflow", help="path to the workflow YAML")
    p.add_argument("--registry", default="", help="module:function returning a TokenRegistry (token/predicate checks)")
    args = p.parse_args(argv)

    try:
        node = load_workflow(args.workflow)
    except (SchemaError, ManifestError) as exc:
        print(f"[FAIL] load error: {exc}")
        return 1

    registry = None
    if args.registry:
        try:
            registry = _load_registry(args.registry)
        except Exception as exc:  # noqa: BLE001 — diagnostic only; degrade to structure-only
            print(f"[WARN] could not import registry {args.registry!r}: {exc}")

    errors = validate(node, registry)
    if errors:
        print(f"[FAIL] {len(errors)} validation error(s):")
        for e in errors:
            print(f"  - {e}")
        return 1

    scope = "structure + data-flow + tokens" if registry else "structure + data-flow"
    print(f"[PASS] {args.workflow}  ✓  ({scope}; {len(node.phases)} phases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
