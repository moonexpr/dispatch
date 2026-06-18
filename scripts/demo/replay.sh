#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# replay.sh — one-command offline replay of a demo scenario (E8-3 / issue #45).
#
# The driver a tester runs each round. It resolves an overlay scenario into a
# temp fixture via mutate.py (E8-2), runs a full OFFLINE, DRY-RUN tick through
# `./dispatch --fixture`, and dumps the round's artifacts for inspection under
# scripts/demo/rounds/<scenario>/<n>/:
#   fixture.json      the resolved scenario (from mutate.py)
#   work-order.txt    the work order (`./dispatch --fixture`, stdout)
#   job-request.json  the Job Request the Engineer would receive, projected from
#                     the `--json` envelope's job fields (schemas/job-request.json)
#   invoice.json      the Invoice from the offline Engineer (scripts/mock-engineer.sh,
#                     schemas/invoice.json)
#
# Everything is offline and dry-run — nothing on GitHub is touched (dispatch
# under PIPELINE_DRY_RUN=1 only echoes intended calls). It is REPEATABLE (same
# scenario -> byte-identical work-order + job-request, since mutate.py and
# ./dispatch are deterministic) and RESETTABLE (`--reset` clears that scenario's
# rounds/ subtree; the committed snapshot/scenario files are never modified).
#
# Usage:
#   scripts/demo/replay.sh <scenario> [--round N] [--reset] [--dag] [--issue N]
# <scenario> is a slug (blank-vague-body), filename, or path under scenarios/.
# Rounds land under ${DEMO_ROUNDS_DIR:-scripts/demo/rounds} (the override lets
# tests redirect into a temp dir).
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/../lib/common.sh"

DEMO_DIR="${SCRIPT_DIR}"
ROUNDS_DIR="${DEMO_ROUNDS_DIR:-${DEMO_DIR}/rounds}"
MUTATE="${DEMO_DIR}/mutate.py"
DISPATCH="${PIPELINE_ROOT}/dispatch"
MOCK_ENGINEER="${PIPELINE_ROOT}/scripts/mock-engineer.sh"

SCENARIO=""
ROUND=""
RESET=0
PASSTHRU=()   # --dag / --issue N forwarded to ./dispatch

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset) RESET=1; shift ;;
    --round) ROUND="$2"; shift 2 ;;
    --dag)   PASSTHRU+=(--dag); shift ;;
    --issue) PASSTHRU+=(--issue "$2"); shift 2 ;;
    -h|--help)
      grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*) die "unknown option: $1 (try --help)" ;;
    *)  SCENARIO="$1"; shift ;;
  esac
done

[[ -n "${SCENARIO}" ]] || die "usage: replay.sh <scenario> [--round N] [--reset] [--dag] [--issue N]"

# Resolve the scenario slug + file: accept a bare name, a filename, or a path.
slug="$(basename "${SCENARIO}" .json)"
scenario_file="${DEMO_DIR}/scenarios/${slug}.json"
[[ -f "${scenario_file}" ]] || scenario_file="${SCENARIO}"   # allow a direct path
scenario_rounds="${ROUNDS_DIR}/${slug}"

# --reset: clear this scenario's TRANSIENT rounds output and exit. The committed
# snapshot/scenario files live elsewhere and are never touched — reset only
# deletes regenerable round artifacts.
if [[ "${RESET}" -eq 1 ]]; then
  rm -rf "${scenario_rounds}"
  mkdir -p "${scenario_rounds}"
  log "replay: reset rounds for '${slug}' (${scenario_rounds})"
  exit 0
fi

[[ -f "${scenario_file}" ]] || die "scenario not found: ${SCENARIO} (looked for ${DEMO_DIR}/scenarios/${slug}.json)"
require_tool jq

# Allocate the round dir: auto-increment, or honour --round N.
mkdir -p "${scenario_rounds}"
if [[ -n "${ROUND}" ]]; then
  n="${ROUND}"
else
  n=1
  while [[ -e "${scenario_rounds}/${n}" ]]; do n=$((n + 1)); done
fi
round_dir="${scenario_rounds}/${n}"
mkdir -p "${round_dir}"

# Offline + dry-run for the whole replay; round dir is the artifacts sink (the
# same DISPATCH_ARTIFACTS_DIR seam --dag/--until already use).
export PIPELINE_DRY_RUN=1
export DISPATCH_ARTIFACTS_DIR="${round_dir}"

# 1. Resolve the overlay -> this round's fixture.json (deterministic).
"${PYTHON_BIN}" "${MUTATE}" --scenario "${scenario_file}" --out "${round_dir}/fixture.json"

# 2. Work order (text) + envelope (--json) from the offline tick.
"${DISPATCH}" --fixture "${round_dir}/fixture.json" ${PASSTHRU[@]+"${PASSTHRU[@]}"} \
  > "${round_dir}/work-order.txt" 2>/dev/null
envelope="$("${DISPATCH}" --fixture "${round_dir}/fixture.json" --json ${PASSTHRU[@]+"${PASSTHRU[@]}"} 2>/dev/null)"

# 3. Project the Job Request from the envelope's job fields. The --json envelope
#    is a SUPERSET (it also carries authorization/units/staffing/work_order); the
#    Job Request is exactly the 8 keys schemas/job-request.json requires.
printf '%s' "${envelope}" \
  | jq '{job_id, issue, repo, title, body, route, scope, confidence}' \
  > "${round_dir}/job-request.json"

# 4. Invoice from the offline Engineer (mock-engineer.sh maps issue -> a
#    committed, schema-valid invoice fixture).
"${MOCK_ENGINEER}" "$(cat "${round_dir}/job-request.json")" > "${round_dir}/invoice.json" 2>/dev/null

log "replay: scenario='${slug}' round=${n} (dry-run, offline) -> ${round_dir}"
log "replay:   fixture.json · work-order.txt · job-request.json · invoice.json"
