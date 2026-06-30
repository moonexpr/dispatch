#!/usr/bin/env bash
# test-budget.sh — live-wiring test for the BUDGET subsystem (#97, epic #95).
#
# Exercises the two halves of the budget pillar OFFLINE (D2: "oracle = ceiling,
# ledger = attribution, pre-flight guard = throttle"):
#   * src/budget/guard.py + oracle.py — the pre-flight soft-cap THROTTLE
#     (consumed by ./pipeline devtools dispatch::budget_preflight_throttles).
#   * src/architect/approval.py       — the scope->budget map + phase split.
#
# Pure/offline: every oracle read is a fixture (BUDGET_ORACLE_FIXTURE); the
# claude-monitor path is exercised only with a deliberately-absent binary to
# prove the gate fails OPEN. No network, no model, no ~/.claude read, no gh.
#
# Complements scripts/smoke.sh (the 351-assertion unit baseline) with the
# end-to-end wiring smoke can't see. Run from anywhere:
#   bash scripts/demo/test-budget.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../../.." && pwd)"
PY="${PYTHON_BIN:-python3}"
GUARD="${ROOT}/src/budget/guard.py"
FIXDIR="${ROOT}/src/budget/fixtures"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

pass=0 fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n'  "$1"; fail=$((fail+1)); }
# assert_eq <label> <expected> <actual>
assert_eq() { [[ "$2" == "$3" ]] && ok "$1 (=$3)" || bad "$1 (expected '$2', got '$3')"; }

# guard_field <fixture-path-or-empty> <jq-field> [extra-env...]
guard_field() {
  local fx="$1" field="$2"; shift 2
  if [[ -n "${fx}" ]]; then
    BUDGET_ORACLE_FIXTURE="${fx}" "$@" "${PY}" "${GUARD}" 2>/dev/null | jq -r "${field}"
  else
    "$@" "${PY}" "${GUARD}" 2>/dev/null | jq -r "${field}"
  fi
}

echo "== BUDGET subsystem test (#97) — offline =="
echo
echo "-- guard.py / oracle: pre-flight soft-cap throttle --"

# 1. UNDER-CAP committed fixture (0.4545 < 0.80) -> proceed (throttle=false).
assert_eq "under-cap throttle"  "false"   "$(guard_field "${FIXDIR}/window-state.under-cap.json" '.throttle')"
assert_eq "under-cap fraction"  "0.4545"  "$(guard_field "${FIXDIR}/window-state.under-cap.json" '.fraction')"

# 2. OVER-CAP committed fixture (0.9091 >= 0.80) -> THROTTLE (forces dry-run/skip-claim upstream).
assert_eq "over-cap throttle"   "true"    "$(guard_field "${FIXDIR}/window-state.over-cap.json" '.throttle')"
assert_eq "over-cap fraction"   "0.9091"  "$(guard_field "${FIXDIR}/window-state.over-cap.json" '.fraction')"

# 3. NO fixture + NO monitor binary -> oracle raises -> guard FAILS OPEN (throttle=false, fraction=null).
miss="$(guard_field "" '.throttle'  env CLAUDE_MONITOR_BIN=/nonexistent/claude-monitor BUDGET_ORACLE_FIXTURE=)"
assert_eq "no-data fails open"  "false"   "${miss}"
assert_eq "no-data fraction null" "null"  "$(guard_field "" '.fraction' env CLAUDE_MONITOR_BIN=/nonexistent/claude-monitor BUDGET_ORACLE_FIXTURE=)"

# 4. MALFORMED fixture (oracle can't parse) -> FAILS OPEN (throttle=false).
printf '{ this is not json ' > "${TMP}/bad.json"
assert_eq "malformed fixture fails open" "false" "$(guard_field "${TMP}/bad.json" '.throttle')"
# empty fixture file -> also fail open
: > "${TMP}/empty.json"
assert_eq "empty fixture fails open"     "false" "$(guard_field "${TMP}/empty.json" '.throttle')"

# 5. BOUNDARY fraction == soft_cap (0.80) -> THROTTLE (>= is inclusive).
printf '{"plan":"max5","limit_tokens":100000,"used_tokens":80000}\n'  > "${TMP}/eq.json"
assert_eq "fraction==soft_cap throttles" "true"  "$(guard_field "${TMP}/eq.json" '.throttle')"
assert_eq "fraction==soft_cap value"     "0.8"   "$(guard_field "${TMP}/eq.json" '.fraction')"
# just-below the cap -> proceed
printf '{"plan":"max5","limit_tokens":100000,"used_tokens":79900}\n'  > "${TMP}/below.json"
assert_eq "fraction just-below proceeds" "false" "$(guard_field "${TMP}/below.json" '.throttle')"

# 6. soft_cap surfaced in decision matches the committed tuning (0.8).
assert_eq "soft_cap from tuning" "0.8" "$(guard_field "${FIXDIR}/window-state.under-cap.json" '.soft_cap')"

echo
echo "-- approval.py: scope -> budget map + phase split --"

# approve <scope> -> JSON authorization
approve() {
  local scope="$1"
  PYTHONPATH="${ROOT}/src:${ROOT}/src/architect" "${PY}" -c "
import json,sys; sys.path.insert(0,'${ROOT}/src/architect'); sys.path.insert(0,'${ROOT}/src')
import approval
print(json.dumps(approval.approve({'issue':1,'scope':'${scope}'},{'scope':'${scope}','route':'gen-default'}).to_dict()))"
}

# 7. scope -> total budget map (xs 20k / s 40k / m 80k / l 160k).
assert_eq "scope xs budget" "20000"  "$(approve xs | jq -r '.budget_tokens')"
assert_eq "scope s  budget" "40000"  "$(approve s  | jq -r '.budget_tokens')"
assert_eq "scope m  budget" "80000"  "$(approve m  | jq -r '.budget_tokens')"
assert_eq "scope l  budget" "160000" "$(approve l  | jq -r '.budget_tokens')"

# 8. UNKNOWN scope -> falls back to m (80000), never crashes.
assert_eq "unknown scope -> m" "80000" "$(approve zzz | jq -r '.budget_tokens')"

# 9. phase split for l (160000): READ .15 / IMPLEMENT .45 / VERIFY .12 / COMMIT&PR .08, reserve ~.20.
L="$(approve l)"
assert_eq "l READ .15"        "24000"  "$(jq -r '.phase_budgets.READ'        <<<"${L}")"
assert_eq "l IMPLEMENT .45"   "72000"  "$(jq -r '.phase_budgets.IMPLEMENT'   <<<"${L}")"
assert_eq "l VERIFY .12"      "19200"  "$(jq -r '.phase_budgets.VERIFY'      <<<"${L}")"
assert_eq "l COMMIT&PR .08"   "12800"  "$(jq -r '.phase_budgets["COMMIT & PR"]' <<<"${L}")"
assert_eq "l reserve ~.20"    "32000"  "$(jq -r '.reserve_tokens'            <<<"${L}")"

# 10. boundary scope xs phase split (20000): reserve held, phases sum to .80.
XS="$(approve xs)"
assert_eq "xs READ .15"       "3000"   "$(jq -r '.phase_budgets.READ' <<<"${XS}")"
assert_eq "xs reserve ~.20"   "4000"   "$(jq -r '.reserve_tokens'     <<<"${XS}")"
xs_phases="$(jq -r '[.phase_budgets|to_entries[].value]|add' <<<"${XS}")"
assert_eq "xs phases sum (.80)" "16000" "${xs_phases}"

echo
echo "== BUDGET: ${pass} passed, ${fail} failed =="
[[ "${fail}" -eq 0 ]]
