#!/usr/bin/env python3
"""smoke.py — workflow-engine smoke gate (CI).

Tests ONLY the canonical workflow engine (``engine/`` + ``src/baseworkflow/``)
and the authoring seam it drives. The legacy bash pipeline suite — the *visitor
lineage* (orchestration / architect / intake / budget / classifier / ledger) —
was parked in ``src/visitor/smoke.sh`` during the ``src/visitor/`` consolidation
and is NOT run here until that lineage's callers are rewired. See that file.

Each check shells out to a self-asserting target (exit 0 == pass). Fully offline
and dry-run: no network, no ``gh``/model calls.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# (label, argv) — each subprocess must exit 0 to pass.
CHECKS: list[tuple[str, list[str]]] = [
    (
        "workflow validator (engine/workflow + bindings registry over app/workflows/baseworkflow.yml)",
        [PY, "-m", "engine.workflow", "workflows/baseworkflow.yml",
         "--registry", "src.baseworkflow.bindings:build_registry"],
    ),
    (
        "BaseWorkflow engine e2e (src/baseworkflow/test_e2e.py)",
        [PY, "src/baseworkflow/test_e2e.py"],
    ),
    (
        "authoring seam: DISPATCH_ENGINE=baseworkflow bridge (src/baseworkflow ↔ engine)",
        [PY, "src/visitor/orchestration/test_baseworkflow_bridge.py"],
    ),
]


def main() -> int:
    env = {**os.environ, "PIPELINE_DRY_RUN": "1"}
    npass = nfail = 0
    print("== smoke.py — workflow-engine gate (offline, dry-run) ==")
    for label, argv in CHECKS:
        proc = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
        last = next((ln for ln in reversed((proc.stdout or "").splitlines()) if ln.strip()), "")
        if proc.returncode == 0:
            npass += 1
            print(f"  PASS  {label}")
            if last.strip():
                print(f"          {last.strip()}")
        else:
            nfail += 1
            print(f"  FAIL  {label}  (exit {proc.returncode})")
            for ln in ((proc.stdout or "") + (proc.stderr or "")).splitlines()[-6:]:
                print(f"          {ln}")
    print(f"\nsmoke.py: PASS={npass} FAIL={nfail}")
    return 1 if nfail else 0


if __name__ == "__main__":
    raise SystemExit(main())
