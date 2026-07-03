#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

# `entrypoint.sh report` (operator digest from the run-ledger, E4-2/#37) is
# DISABLED: src/reports/report.py was relocated into the parked legacy pipeline
# lineage during the consolidation and is not wired up. Re-enable once that
# lineage is rewired.
# if [[ "${1:-}" == "report" ]]; then
#   shift
#   exec "${PYTHON_BIN:-python3}" "${ROOT}/src/reports/report.py" "$@"
# fi

# Anything else: run one tick via the single Python entry. dispatch owns policy
# (sets PYTHONPATH + the SDK-engineer ENGINEER_BIN default), then drives the
# src.orchestration tick, which executes the YAML baseworkflow engine (foundation/workflow).
# Replaces the retired scripts/pipeline.sh shell launcher.
exec "${PYTHON_BIN:-python3}" "${ROOT}/dispatch" "$@"
