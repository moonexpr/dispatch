#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

# `entrypoint.sh report [flags]` renders the operator digest from the run-ledger
# (E4-2/#37); anything else runs a pipeline tick. The report path is read-only
# and offline — it never claims, mutates, or calls a model.
if [[ "${1:-}" == "report" ]]; then
  shift
  exec "${PYTHON_BIN:-python3}" "${ROOT}/services/reports/report.py" "$@"
fi

exec "${ROOT}/scripts/pipeline.sh" "$@"
