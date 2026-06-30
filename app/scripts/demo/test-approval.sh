#!/usr/bin/env bash
# test-approval.sh — live-wiring test for ADMIN APPROVAL (#100, epic #95):
# the label state machine + fix-dispatch ladder.
#
# Drives the three decision-point scripts OFFLINE in dry-run (PIPELINE_DRY_RUN=1,
# so gh_mutate prints `DRY-RUN: gh …` and mutates nothing) with crafted Invoice /
# CI payloads, and asserts the INTENDED label transitions:
#   * architect-intake.sh — Invoice status → approval / fix-ladder / escalation.
#   * fix-dispatch.sh      — CI-failure retry ladder (1→local 2→default 3→frontier >3→human).
#   * closure.sh           — CI-green + review-approved gate → done-pending-merge → done.
# Plus the pure ladder helpers (tier_for_attempt / resolve_attempt / max_fix_attempt).
#
# No gh, no network, no model — every mutation is a printed DRY-RUN line.
# Run from anywhere:  bash scripts/demo/test-approval.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../../.." && pwd)"
S="${ROOT}/scripts"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
export PIPELINE_DRY_RUN=1
export DISPATCH_LEDGER_FILE="${TMP}/ledger.jsonl"   # keep ledger writes off the repo

pass=0 fail=0
ok()  { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n'  "$1"; fail=$((fail+1)); }
assert_eq() { [[ "$2" == "$3" ]] && ok "$1 (=$3)" || bad "$1 (expected '$2', got '$3')"; }
# assert_has <label> <haystack> <needle>
assert_has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (missing: $3)" ;; esac; }
assert_not() { case "$2" in *"$3"*) bad "$1 (unexpected: $3)" ;; *) ok "$1" ;; esac; }

# intake <status> <pr_number> -> combined stdout+stderr of architect-intake.sh
intake() {
  local status="$1" pr="$2"
  local inv
  inv="$(jq -nc --arg s "$status" --argjson pr "${pr:-null}" \
    '{invoice_id:"iv-1",issue:55,repo:"acme/x",status:$s,route_used:"gen-default",
      pr_number:$pr,summary:"s",cost:{tokens_in:1,tokens_out:1,duration_seconds:1},
      timestamp:"2026-06-20T00:00:00Z"}')"
  bash "${S}/architect-intake.sh" "${inv}" 2>&1
}

echo "== ADMIN APPROVAL test (#100) — offline / dry-run =="
echo
echo "-- architect-intake.sh: Invoice status -> label transition --"

o="$(intake completed 70)"
assert_has "completed: claimed -> done-pending-merge" "$o" "--remove-label claimed --add-label done-pending-merge"
assert_has "completed: arms auto-merge (squash)"      "$o" "pr merge 70 --auto --squash"
o="$(intake partial 71)"
assert_has "partial: adds fix-attempt-1"              "$o" "--add-label fix-attempt-1"
assert_not "partial: does NOT close the issue"        "$o" "done-pending-merge"
o="$(intake failed 72)"
assert_has "failed: adds fix-attempt-1"               "$o" "--add-label fix-attempt-1"
o="$(intake needs-human 0)"
assert_has "needs-human: claimed -> needs-human"      "$o" "--remove-label claimed --add-label needs-human"

# Unknown / malformed Invoice is rejected (non-zero), never silently approved.
bash "${S}/architect-intake.sh" '{"status":"bogus","issue":1}' >/dev/null 2>&1
assert_eq "unknown status rejected (exit 1)" "1" "$?"
bash "${S}/architect-intake.sh" '{"issue":1}' >/dev/null 2>&1
assert_eq "missing status rejected (exit 1)" "1" "$?"

echo
echo "-- fix-dispatch.sh: CI-failure retry ladder --"

# fixd <attempt-or-empty> <labels-json> <conclusion> -> output
fixd() {
  local attempt="$1" labels="$2" conclusion="${3:-failure}"
  local payload
  payload="$(jq -nc --argjson labels "${labels}" --arg c "${conclusion}" \
    '{pr:200,issue:55,conclusion:$c,brief:"b",labels:$labels}')"
  if [[ -n "${attempt}" ]]; then
    PIPELINE_FIX_ATTEMPT="${attempt}" bash "${S}/fix-dispatch.sh" <<<"${payload}" 2>&1
  else
    PIPELINE_FIX_ATTEMPT="" bash "${S}/fix-dispatch.sh" <<<"${payload}" 2>&1
  fi
}

o="$(fixd "" '[]')"           # no labels -> attempt 1
assert_has "attempt 1: labels fix-attempt-1 (tier gen-local)" "$o" "--add-label fix-attempt-1"
assert_has "attempt 1: tier resolved gen-local"               "$o" "tier=gen-local"
o="$(fixd "" '[{"name":"fix-attempt-1"}]')"   # max label 1 -> attempt 2
assert_has "attempt 2: fix-attempt-1 -> fix-attempt-2" "$o" "--remove-label fix-attempt-1 --add-label fix-attempt-2"
assert_has "attempt 2: tier gen-default"               "$o" "tier=gen-default"
o="$(fixd "" '[{"name":"fix-attempt-2"}]')"   # -> attempt 3
assert_has "attempt 3: fix-attempt-2 -> fix-attempt-3" "$o" "--remove-label fix-attempt-2 --add-label fix-attempt-3"
assert_has "attempt 3: tier gen-frontier"              "$o" "tier=gen-frontier"
o="$(fixd "" '[{"name":"fix-attempt-3"}]')"   # -> attempt 4 > cap
assert_has "attempt 4 (>cap): escalates needs-human" "$o" "--add-label needs-human"
assert_not "attempt 4: does NOT add fix-attempt-4"   "$o" "fix-attempt-4"
o="$(fixd "" '[]' success)"   # CI green -> no-op
assert_not "CI success: no label mutation (no-op)" "$o" "--add-label"
assert_has "CI success: explicit no-op log"        "$o" "no-op"
# Explicit env override wins over labels.
o="$(fixd 3 '[]')"
assert_has "PIPELINE_FIX_ATTEMPT override -> attempt 3" "$o" "--add-label fix-attempt-3"

echo
echo "-- closure.sh: CI-green + review-approved gate --"

# clo <conclusion> <review> <merged> -> output
clo() {
  local payload
  payload="$(jq -nc --arg c "$1" --arg r "$2" --argjson m "${3:-false}" \
    '{pr:300,issue:55,conclusion:$c,review_state:$r,merged:$m}')"
  bash "${S}/closure.sh" <<<"${payload}" 2>&1
}

o="$(clo success approved false)"
assert_has "green+approved: in-review -> done-pending-merge" "$o" "--remove-label in-review --add-label done-pending-merge"
assert_has "green+approved: arms auto-merge"                 "$o" "pr merge 300 --auto --squash"
o="$(clo success none false)"
assert_not "green but NOT approved: gate holds (no transition)" "$o" "done-pending-merge"
assert_has "green but NOT approved: no-op log"                  "$o" "not ready"
o="$(clo failure approved false)"
assert_not "approved but CI red: gate holds" "$o" "done-pending-merge"
o="$(clo success approved true)"
assert_has "merged event: done-pending-merge -> done" "$o" "--add-label done --remove-label done-pending-merge"

echo
echo "-- pure ladder helpers (tier_for_attempt / resolve_attempt / max_fix_attempt) --"
helpers="$(
  export PIPELINE_DRY_RUN=1 PIPELINE_FIX_ATTEMPT="" PIPELINE_FIXTURE_PR=""
  # shellcheck disable=SC1090
  source "${S}/lib/common.sh" >/dev/null 2>&1
  printf 't1=%s t2=%s t3=%s t4=%s t0=%s\n' \
    "$(tier_for_attempt 1)" "$(tier_for_attempt 2)" "$(tier_for_attempt 3)" \
    "$(tier_for_attempt 4)" "$(tier_for_attempt 0)"
)"
assert_has "tier 1 -> gen-local"     "$helpers" "t1=gen-local"
assert_has "tier 2 -> gen-default"   "$helpers" "t2=gen-default"
assert_has "tier 3 -> gen-frontier"  "$helpers" "t3=gen-frontier"
assert_has "tier 4 -> needs-human"   "$helpers" "t4=needs-human"
assert_has "tier 0/invalid -> needs-human" "$helpers" "t0=needs-human"
# (max_fix_attempt/resolve_attempt live in fix-dispatch.sh and are proven by the
#  ladder tests above: attempt 2 derived from a fix-attempt-1 label, etc.)

echo
echo "-- dry-run safety: every mutation was a printed DRY-RUN line (nothing executed) --"
o="$(intake completed 70)"
assert_has "intake mutations are DRY-RUN only" "$o" "DRY-RUN: gh"

echo
echo "== ADMIN APPROVAL: ${pass} passed, ${fail} failed =="
[[ "${fail}" -eq 0 ]]
