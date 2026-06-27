#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

# `entrypoint.sh report [flags]` renders the operator digest from the run-ledger
# (E4-2/#37); anything else runs a pipeline tick. The report path is read-only
# and offline — it never claims, mutates, or calls a model.
if [[ "${1:-}" == "report" ]]; then
  shift
  exec "${PYTHON_BIN:-python3}" "${ROOT}/src/reports/report.py" "$@"
fi

# Anything else: run one tick via the single Python entry. dispatch.py owns policy
# (sets PYTHONPATH + the SDK-engineer ENGINEER_BIN default), then drives the
# src.orchestration tick, which executes the YAML baseworkflow engine (engine/workflow).
# Replaces the retired scripts/pipeline.sh shell launcher.
exec "${PYTHON_BIN:-python3}" "${ROOT}/dispatch.py" "$@"
