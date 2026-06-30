#!/usr/bin/env python3
"""smoke.py — workflow-engine smoke gate (CI).

Tests ONLY the canonical workflow engine (``foundation/`` + ``baseworkflow/``)
and the architect↔worker SEAM CONTRACT it owns. Carries **no reference to the
parked legacy pipeline lineage**, which lives in a separate non-functional
graveyard until its callers are rewired.

Checks:
  1. workflow validator — foundation/workflow over app/workflows/baseworkflow.yml
     with the baseworkflow bindings registry.
  2. BaseWorkflow engine e2e — baseworkflow/test_e2e.py.
  3. architect↔worker v1 seam contracts — every versioned schema under schemas/
     (WorkOrder / JobRequest / Invoice #143; Issue / Classification / RankedQueue
     / PrEvent / LedgerRecord #145) validates its golden fixture. The schemas and
     fixtures live at the repo root (schemas/, schemas/fixtures/), independent of
     any lineage, so this validates the frozen contract without running producers.

Fully offline + dry-run: no network, no gh/model calls.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
sys.path.insert(0, str(ROOT))  # repo root carries the `engine` package
from foundation import proc  # noqa: E402  (capturing subprocess wrapper)

# Subprocess checks — each self-asserting target must exit 0.
SUBPROC_CHECKS: list[tuple[str, list[str]]] = [
    (
        "workflow validator (foundation/workflow + bindings registry over app/workflows/baseworkflow.yml)",
        [PY, "-m", "foundation.workflow", "workflows/baseworkflow.yml",
         "--registry", "baseworkflow.bindings:build_registry"],
    ),
    (
        "BaseWorkflow engine e2e (baseworkflow/test_e2e.py)",
        [PY, "baseworkflow/test_e2e.py"],
    ),
]

# The frozen architect↔worker v1 seam: (schema stem, human label).
SEAM_CONTRACTS: list[tuple[str, str]] = [
    ("work-order", "WorkOrder v1 (#143)"),
    ("job-request", "JobRequest v1 (#143)"),
    ("invoice", "Invoice v1 (#143)"),
    ("issue", "Issue v1 (#145)"),
    ("classification", "Classification v1 (#145)"),
    ("ranked-queue", "RankedQueue v1 (#145)"),
    ("pr-event", "PrEvent v1 (#145)"),
    ("ledger-record", "LedgerRecord v1 (#145)"),
]


def run_subproc(argv: list[str], env: dict) -> tuple[bool, str]:
    cp = proc.run(argv, cwd=ROOT, env=env)
    if cp.returncode == 0:
        last = next((ln for ln in reversed((cp.stdout or "").splitlines()) if ln.strip()), "")
        return True, last.strip()
    tail = ((cp.stdout or "") + (cp.stderr or "")).splitlines()[-6:]
    return False, "\n".join(f"          {t}" for t in tail) or f"          exit {cp.returncode}"


def check_seam_contracts() -> tuple[bool, str]:
    """Validate every v1 golden fixture against its schema. No lineage code."""
    try:
        import jsonschema
    except ImportError:
        return False, "          jsonschema not installed (required for the seam contract check)"
    sd = ROOT / "schemas"
    fd = sd / "fixtures"
    bad: list[str] = []
    for stem, label in SEAM_CONTRACTS:
        schema_p = sd / f"{stem}.v1.json"
        golden_p = fd / f"{stem}.v1.golden.json"
        try:
            jsonschema.validate(
                json.loads(golden_p.read_text(encoding="utf-8")),
                json.loads(schema_p.read_text(encoding="utf-8")),
            )
        except FileNotFoundError as exc:
            bad.append(f"          {label}: missing {exc.filename}")
        except Exception as exc:  # noqa: BLE001 — surface any validation failure
            bad.append(f"          {label}: {type(exc).__name__}: {str(exc).splitlines()[0][:90]}")
    if bad:
        return False, "\n".join(bad)
    return True, f"{len(SEAM_CONTRACTS)}/{len(SEAM_CONTRACTS)} v1 schemas validate their golden fixtures"


def main() -> int:
    env = {**os.environ, "PIPELINE_DRY_RUN": "1"}
    npass = nfail = 0
    print("== smoke.py — workflow-engine gate (offline, dry-run) ==")

    results: list[tuple[str, bool, str]] = []
    for label, argv in SUBPROC_CHECKS:
        ok, detail = run_subproc(argv, env)
        results.append((label, ok, detail))
    ok, detail = check_seam_contracts()
    results.append(("architect↔worker v1 seam contracts (schemas/ vs golden fixtures)", ok, detail))

    for label, ok, detail in results:
        if ok:
            npass += 1
            print(f"  PASS  {label}")
            if detail:
                print(f"          {detail}")
        else:
            nfail += 1
            print(f"  FAIL  {label}")
            if detail:
                print(detail)
    print(f"\nsmoke.py: PASS={npass} FAIL={nfail}")
    return 1 if nfail else 0


if __name__ == "__main__":
    raise SystemExit(main())
