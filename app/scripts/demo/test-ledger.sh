#!/usr/bin/env bash
# test-ledger.sh — live-wiring test for the LEDGER subsystem (#98, epic #95).
#
# Exercises the append-only JSONL run-ledger (E4/Pillar 4) OFFLINE:
#   * src/ledger/ledger.py        — the record builder + emit seam.
#   * scripts/lib/common.sh::ledger_emit — the bash transition seam (dry-run
#     stamping + best-effort "never fail the tick").
#   * src/reports/report.py        — the read-only operator digest.
#
# Pure/offline: local-file writes only, no gh, no network, no model. Ledger
# path is redirected to a temp via DISPATCH_LEDGER_FILE for every case.
#
# Complements scripts/smoke.sh. Run from anywhere:
#   bash scripts/demo/test-ledger.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../../.." && pwd)"
PY="${PYTHON_BIN:-python3}"
LEDGER="${ROOT}/src/ledger/ledger.py"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

pass=0 fail=0
ok()  { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n'  "$1"; fail=$((fail+1)); }
assert_eq() { [[ "$2" == "$3" ]] && ok "$1 (=$3)" || bad "$1 (expected '$2', got '$3')"; }
# assert_true <label> <cmd...>
assert_true() { local l="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$l"; else bad "$l"; fi; }

echo "== LEDGER subsystem test (#98) — offline =="
echo
echo "-- ledger.py: one line per stage, all keys present --"

LED="${TMP}/run-ledger.jsonl"
: > "${LED}"
# Emit one line per canonical stage.
for stage in claimed work-order engineer-dispatch invoice closure; do
  "${PY}" "${LEDGER}" --stage "${stage}" --issue 5 --tick-id "tick-LED" --dry-run 1 \
    --fields '{"label_before":"queued","label_after":"claimed"}' --file "${LED}" --quiet
done
assert_eq "5 stages -> 5 lines" "5" "$(wc -l < "${LED}" | tr -d ' ')"

# Every line carries the full key set, including the 4 cost.* sub-keys (null when absent).
missing="$(jq -c 'select(
  has("tick_id") and has("issue") and has("stage") and has("timestamp") and
  has("dry_run") and has("label_before") and has("label_after") and
  (.cost|has("tokens_in") and has("tokens_out") and has("duration_seconds") and has("model"))
  | not)' "${LED}" | wc -l | tr -d ' ')"
assert_eq "all lines have full key set" "0" "${missing}"

# Stage vocabulary preserved verbatim, in order.
got_stages="$(jq -r '.stage' "${LED}" | paste -sd, -)"
assert_eq "stage vocabulary" "claimed,work-order,engineer-dispatch,invoice,closure" "${got_stages}"

# Optional cost fields serialize as null (present, not absent) when omitted.
assert_eq "absent cost -> null (not missing)" "null" "$(jq -r 'select(.stage=="claimed").cost.tokens_in' "${LED}")"

echo
echo "-- cost.* from an invoice file (engineer stage) --"
INV="${TMP}/invoice.json"
printf '{"cost":{"tokens_in":40000,"tokens_out":20000,"duration_seconds":140,"model":"gen-default"}}\n' > "${INV}"
: > "${LED}"
"${PY}" "${LEDGER}" --stage invoice --issue 5 --tick-id "tick-LED" --dry-run 0 \
  --fields "{\"invoice\":\"${INV}\"}" --file "${LED}" --quiet
assert_eq "invoice tokens_in"  "40000"       "$(jq -r '.cost.tokens_in'  "${LED}")"
assert_eq "invoice tokens_out" "20000"       "$(jq -r '.cost.tokens_out' "${LED}")"
assert_eq "invoice model"      "gen-default" "$(jq -r '.cost.model'      "${LED}")"
assert_eq "invoice dry_run=0 -> false" "false" "$(jq -r '.dry_run' "${LED}")"

echo
echo "-- unknown stage rejected (exit 2) --"
"${PY}" "${LEDGER}" --stage bogus --issue 5 --file "${TMP}/x.jsonl" --quiet 2>/dev/null
assert_eq "unknown stage exit code" "2" "$?"

echo
echo "-- bash ledger_emit seam: dry_run stamping + never-fail --"
# Source common.sh in a subshell with the minimal env it needs, then exercise
# ledger_emit under both dry-run modes and against an unwritable sink.
run_emit() {  # run_emit <PIPELINE_DRY_RUN> <DISPATCH_LEDGER_FILE> <stage>
  ( set +u
    export PIPELINE_ROOT="${ROOT}" PYTHON_BIN="${PY}"
    export PIPELINE_DRY_RUN="$1" DISPATCH_LEDGER_FILE="$2" DISPATCH_TICK_ID="tick-EMIT"
    # shellcheck disable=SC1090
    source "${ROOT}/scripts/lib/common.sh" >/dev/null 2>&1 || true
    ledger_emit "$3" 5 '{"label_before":"queued","label_after":"claimed"}'
    echo "rc=$?" )
}
LE="${TMP}/emit.jsonl"; : > "${LE}"
rc1="$(run_emit 1 "${LE}" claimed | sed -n 's/^rc=//p')"
assert_eq "ledger_emit returns 0 (dry-run)" "0" "${rc1}"
assert_eq "dry-run stamps dry_run=true"  "true"  "$(jq -r '.dry_run' "${LE}")"
: > "${LE}"
run_emit 0 "${LE}" claimed >/dev/null
assert_eq "live stamps dry_run=false" "false" "$(jq -r '.dry_run' "${LE}")"

# Unwritable sink: a path under a non-existent, uncreatable parent. ledger_emit
# must swallow the error and still return 0 (observability never fails the tick).
rc_bad="$(run_emit 1 "/proc/cannot/write/here.jsonl" claimed | sed -n 's/^rc=//p')"
assert_eq "unwritable sink -> emit still returns 0" "0" "${rc_bad}"

echo
echo "-- report.py: read-only digest over the ledger --"
# Seed a small mixed ledger and render both formats via entrypoint.sh report.
SED="${TMP}/seed-ledger.jsonl"
cp "${ROOT}/src/budget/fixtures/ledger.mixed.jsonl" "${SED}"
md="$(DISPATCH_LEDGER_FILE="${SED}" bash "${ROOT}/entrypoint.sh" report --format markdown 2>/dev/null)"
assert_true "report --format markdown exits 0" test -n "${md}"
txt="$(DISPATCH_LEDGER_FILE="${SED}" bash "${ROOT}/entrypoint.sh" report --format text 2>/dev/null)"
assert_true "report --format text exits 0" test -n "${txt}"
# Report is read-only: it must not mutate the ledger it read.
before="$(md5sum "${SED}" | awk '{print $1}')"
DISPATCH_LEDGER_FILE="${SED}" bash "${ROOT}/entrypoint.sh" report --format markdown >/dev/null 2>&1
after="$(md5sum "${SED}" | awk '{print $1}')"
assert_eq "report does not mutate ledger" "${before}" "${after}"

echo
echo "== LEDGER: ${pass} passed, ${fail} failed =="
[[ "${fail}" -eq 0 ]]
