#!/usr/bin/env bash
# test-prep.sh — live-wiring test for the HARNESS PREP stage (#102, epic #95).
#
# The prep stage sits between work-order emission and engineer dispatch
# (intake -> workorder -> [prep] -> engineer -> …) and PROVISIONS the work plan's
# *declared* harness before the job is issued. This harness exercises it OFFLINE,
# one block per #102 case family:
#
#   1. purpose -> harness setup   (data-driven from workplan-rules.yml; no-op for
#                                  a `standard` harness; never hard-coded in Python)
#   2. strategy -> orchestration  (swarm N-agent coordinator / dynamic probe→replan
#                                  / sequential no-op)
#   3. issues_affected            (single-issue and multi-issue aggregate both
#                                  ready the correct branch/worktree)
#   4. dry-run                    (PIPELINE_DRY_RUN=1 prints intended steps,
#                                  provisions nothing — the planner is pure)
#   5. fail-safe                  (an un-provisionable harness BLOCKS issuance with
#                                  a clear reason; idempotent; never half-preps)
#   6. stage wiring               (prep is a valid pipeline.sh --until/--from stage
#                                  and sits between workorder and engineer)
#
# Pure/offline: prep.py is exercised via its CLI; the stage wiring via dry-run
# pipeline ticks against committed fixtures. No gh, no network, no model. The
# planner provisions nothing, so nothing here mutates the repo. Run from anywhere:
#   bash scripts/demo/test-prep.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
PY="${PYTHON_BIN:-python3}"
PREP="${ROOT}/src/architect/prep.py"
FIX="${ROOT}/scripts/fixtures"
PIPE="${ROOT}/pipeline"
QFIX="${FIX}/queued-issues.json"
RJR="${FIX}/replay-job-request.json"   # captured Job Request, issue 101
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

pass=0 fail=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n'  "$1"; fail=$((fail+1)); }
assert_eq() { [[ "$2" == "$3" ]] && ok "$1 (=$3)" || bad "$1 (expected '$2', got '$3')"; }
assert_contains()     { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1 (missing: $3)" ;; esac; }
assert_not_contains() { case "$2" in *"$3"*) bad "$1 (unexpected: $3)" ;; *) ok "$1" ;; esac; }

# prep <args...> -> prep plan JSON on stdout (planner only; provisions nothing).
prep() { "${PY}" "${PREP}" "$@" 2>/dev/null; }
# pj <prep-json> <jq-filter>
pj()   { jq -r "$2" <<<"$1"; }

# --- shared job + plan fixtures -------------------------------------------
JOB="${TMP}/job.json"
printf '%s' '{"issue":101,"repo":"acme/x","title":"Add the prep stage",
  "body":"implement the harness prep stage","route":"gen-default","scope":"m"}' >"${JOB}"
# A decomposition plan with 2 independent (parallel) units -> earns a swarm.
PLAN2="${TMP}/plan2.json"
printf '%s' '{"units":[],"criteria":[],"staffing":{"agent_count":3,"swarm_max":5,
  "capped":false,"parallel":["U1","U2"],"sequential_tail":["U3"],"assignments":[]}}' >"${PLAN2}"

echo "== HARNESS PREP stage test (#102) — offline =="
echo
echo "-- 1. purpose -> harness setup (data-driven; standard = no-op) --"

NF="$(prep --job-json "${JOB}")"                                  # new-feature (keyword)
assert_eq "new-feature -> harness ponytail"  "ponytail"    "$(pj "${NF}" '.harness')"
assert_eq "ponytail provisioner marketplace" "marketplace" "$(pj "${NF}" '.provisioner')"
assert_eq "ponytail is provisionable"        "true"        "$(pj "${NF}" '.provisionable')"
assert_contains "ponytail setup carries the /plugin install command" \
  "$(pj "${NF}" '.harness_setup')" "/plugin install ponytail@ponytail"
assert_eq "ponytail setup splits into 2 steps" "2" "$(pj "${NF}" '.harness_steps | length')"

TST="$(prep --job-json "${JOB}" --labels "purpose:test")"        # test -> standard
assert_eq "test purpose -> harness standard" "standard" "$(pj "${TST}" '.harness')"
assert_eq "standard provisioner none"        "none"     "$(pj "${TST}" '.provisioner')"
assert_eq "standard is a no-op (0 setup steps)" "0" "$(pj "${TST}" '.harness_steps | length')"
assert_eq "standard still provisionable (no-op)" "true" "$(pj "${TST}" '.provisionable')"
assert_contains "no-op harness step says so" "$(pj "${TST}" '.steps | join("\n")')" "no-op"

# Data-driven: an override rules file changes the setup string WITHOUT touching
# Python — proving the command is read from YAML, never hard-coded.
cat >"${TMP}/rules-custom.yml" <<'YML'
purpose:
  default: new-feature
  labels: {}
  keyword_rules: []
  harness:
    new-feature: {harness: ponytail, setup: "echo CUSTOM-SETUP-MARKER", provision: marketplace}
strategy: {dynamic_purposes: [], swarm_min_parallel_units: 2, layouts: {}}
prep: {known_provisioners: [none, marketplace, skill]}
YML
CUST="$(DISPATCH_WORKPLAN_RULES="${TMP}/rules-custom.yml" prep --job-json "${JOB}")"
assert_eq "setup is read from YAML (data-driven, not hard-coded)" \
  "echo CUSTOM-SETUP-MARKER" "$(pj "${CUST}" '.harness_setup')"

echo
echo "-- 2. strategy -> orchestration (swarm / dynamic-workflow / sequential) --"

SWARM="$(prep --job-json "${JOB}" --plan-json "${PLAN2}")"        # 2 parallel units
assert_eq "2 parallel units -> strategy swarm" "swarm" "$(pj "${SWARM}" '.strategy')"
assert_eq "swarm orchestration kind"           "swarm" "$(pj "${SWARM}" '.orchestration.kind')"
assert_eq "swarm provisions N-agent coordinator (N=agent_count)" \
  "3" "$(pj "${SWARM}" '.orchestration.n_agents')"
assert_contains "coordinator is named with its agent count" \
  "$(pj "${SWARM}" '.orchestration.coordinator')" "3-agent swarm coordinator"

DYN="$(prep --job-json "${JOB}" --labels "research" --plan-json "${PLAN2}")"
assert_eq "research purpose -> strategy dynamic-workflow" "dynamic-workflow" "$(pj "${DYN}" '.strategy')"
assert_eq "dynamic orchestration kind" "dynamic-workflow" "$(pj "${DYN}" '.orchestration.kind')"
assert_eq "dynamic arms the probe"  "true" "$(pj "${DYN}" '.orchestration.probe')"
assert_eq "dynamic arms the replan" "true" "$(pj "${DYN}" '.orchestration.replan')"

SEQ="$(prep --job-json "${JOB}")"                                 # no plan -> sequential
assert_eq "single-unit/no-plan -> strategy sequential" "sequential" "$(pj "${SEQ}" '.strategy')"
assert_eq "sequential orchestration is a no-op" "none" "$(pj "${SEQ}" '.orchestration.kind')"

echo
echo "-- 3. issues_affected -> branch/worktree (single + multi-issue aggregate) --"

assert_eq "single-issue branch"   "pipeline/issue-101" "$(pj "${NF}" '.branch')"
assert_eq "single-issue worktree" ".worktrees/issue-101" "$(pj "${NF}" '.worktree')"
assert_eq "single-issue is not an aggregate" "false" "$(pj "${NF}" '.issues_affected.multi_issue')"

AGG="$(prep --job-json "${JOB}" --resolves "102,103")"
assert_eq "aggregate is multi-issue"        "true" "$(pj "${AGG}" '.issues_affected.multi_issue')"
assert_eq "aggregate resolves all 3 issues" "[101,102,103]" "$(pj "${AGG}" '.issues_affected.resolves | tojson')"
assert_eq "aggregate readies the primary's one branch" "pipeline/issue-101" "$(pj "${AGG}" '.branch')"
assert_contains "aggregate step names the resolved set" "$(pj "${AGG}" '.steps | join("\n")')" "#101, #102, #103"

echo
echo "-- 4. dry-run: planner is side-effect-free; the stage prints + provisions nothing --"

# The planner always produces intended steps and is byte-deterministic — there is
# nothing to "undo", which is what makes the stage's dry-run path inherently safe.
assert_eq "plan carries intended steps (>=2)" "true" \
  "$([[ "$(pj "${NF}" '.steps | length')" -ge 2 ]] && echo true || echo false)"
a="$(prep --job-json "${JOB}")"; b="$(prep --job-json "${JOB}")"
assert_eq "planner is deterministic (two runs identical)" "match" \
  "$([[ "$a" == "$b" ]] && echo match || echo differ)"

# End-to-end: a dry-run --until prep tick prints the intended steps and provisions
# nothing (the worktree/gh mutations stay dry-run-gated upstream).
D4="$(mktemp -d)"
e2e_dry="$(DISPATCH_ARTIFACTS_DIR="${D4}" bash "${PIPE}" --until prep --fixture "${QFIX}" 2>&1)"; e2e_rc=$?
assert_eq "--until prep dry-run tick exits 0" "0" "${e2e_rc}"
assert_contains "stage logs DRY-RUN intent" "${e2e_dry}" "prep DRY-RUN"
assert_contains "stage states it provisions nothing" "${e2e_dry}" "provisions nothing"
rm -rf "${D4}"

echo
echo "-- 5. fail-safe: an un-provisionable harness BLOCKS issuance (idempotent) --"

cat >"${TMP}/rules-bogus.yml" <<'YML'
purpose:
  default: new-feature
  labels: {}
  keyword_rules: []
  harness:
    new-feature: {harness: weird, setup: "do thing", provision: not-a-known-provisioner}
strategy: {dynamic_purposes: [], swarm_min_parallel_units: 2, layouts: {}}
prep: {known_provisioners: [none, marketplace, skill]}
YML
BOG="$(DISPATCH_WORKPLAN_RULES="${TMP}/rules-bogus.yml" prep --job-json "${JOB}")"
assert_eq "unknown provisioner -> not provisionable" "false" "$(pj "${BOG}" '.provisionable')"
assert_contains "block_reason names the unknown provisioner" \
  "$(pj "${BOG}" '.block_reason')" "not-a-known-provisioner"
assert_contains "blocked plan's steps say BLOCK" "$(pj "${BOG}" '.steps | join("\n")')" "BLOCK issuance"

# End-to-end: replaying --from prep with a bogus harness must block the engineer
# (no Invoice produced) and exit non-zero — never half-prep and proceed.
D5="$(mktemp -d)"
fs_err="$(DISPATCH_ARTIFACTS_DIR="${D5}" DISPATCH_WORKPLAN_RULES="${TMP}/rules-bogus.yml" \
  bash "${PIPE}" --from prep --artifact "${RJR}" --until engineer 2>&1)"; fs_rc=$?
[[ "${fs_rc}" -ne 0 ]] && ok "fail-safe replay exits non-zero (blocked)" \
                       || bad "fail-safe replay should exit non-zero (got ${fs_rc})"
assert_contains "stage logs that it is blocking issuance" "${fs_err}" "blocking issuance"
fstick="$(find "${D5}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)"
[[ -z "${fstick}" || ! -f "${fstick}/invoice.json" ]] && ok "engineer did NOT run (no invoice.json on block)" \
                                                       || bad "invoice.json present despite a blocked harness"
# Idempotent: a second identical blocked replay behaves identically (no corruption).
D5B="$(mktemp -d)"
DISPATCH_ARTIFACTS_DIR="${D5B}" DISPATCH_WORKPLAN_RULES="${TMP}/rules-bogus.yml" \
  bash "${PIPE}" --from prep --artifact "${RJR}" --until engineer >/dev/null 2>&1; fs_rc2=$?
assert_eq "fail-safe is idempotent (re-run blocks the same way)" "${fs_rc}" "${fs_rc2}"
rm -rf "${D5}" "${D5B}"

echo
echo "-- 6. stage wiring: prep sits between workorder and engineer; --until/--from --"

# Ordinals come from the canonical DISPATCH_STAGES (common.sh) — prep must be 3,
# strictly between workorder (2) and engineer (4).
ords="$(bash -c "source '${ROOT}/scripts/lib/common.sh' 2>/dev/null; \
  echo \"\$(stage_ord workorder) \$(stage_ord prep) \$(stage_ord engineer)\"")"
assert_eq "DISPATCH_STAGES order: workorder<prep<engineer = 2 3 4" "2 3 4" "${ords}"

# --until prep: prep runs (prep.json dumped) AND workorder ran (workorder.txt),
# but the engineer did NOT (no invoice.json); the halt is logged.
W6="$(mktemp -d)"
u6="$(DISPATCH_ARTIFACTS_DIR="${W6}" bash "${PIPE}" --until prep --fixture "${QFIX}" 2>&1)"; u6rc=$?
assert_eq "--until prep exits 0" "0" "${u6rc}"
t6="$(find "${W6}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)"
[[ -n "${t6}" && -f "${t6}/prep.json" ]]     && ok "prep.json dumped (prep stage ran)" || bad "prep.json missing under --until prep"
[[ -n "${t6}" && -f "${t6}/workorder.txt" ]] && ok "workorder.txt present (workorder ran before prep)" || bad "workorder.txt missing under --until prep"
[[ -n "${t6}" && ! -f "${t6}/invoice.json" ]] && ok "no invoice.json (engineer did NOT run after prep)" || bad "invoice.json present despite --until prep"
assert_contains "halt names the prep stage" "${u6}" "halting after prep"
# The dumped prep.json is well-formed and provisionable for a real queued issue.
if [[ -n "${t6}" && -f "${t6}/prep.json" ]]; then
  assert_eq "dumped prep.json is provisionable" "true" "$(jq -r '.provisionable' "${t6}/prep.json")"
fi
rm -rf "${W6}"

# --until workorder: prep has NOT run yet (it sits after workorder) -> no prep.json.
W6B="$(mktemp -d)"
DISPATCH_ARTIFACTS_DIR="${W6B}" bash "${PIPE}" --until workorder --fixture "${QFIX}" >/dev/null 2>&1
t6b="$(find "${W6B}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)"
[[ -n "${t6b}" && ! -f "${t6b}/prep.json" ]] && ok "--until workorder does NOT run prep (no prep.json)" || bad "prep.json present under --until workorder (prep ran too early)"
rm -rf "${W6B}"

# --from prep --until prep: resume AT prep on a captured Job Request, halt after.
W6C="$(mktemp -d)"
r6="$(DISPATCH_ARTIFACTS_DIR="${W6C}" bash "${PIPE}" --from prep --artifact "${RJR}" --until prep 2>&1)"; r6rc=$?
assert_eq "--from prep --until prep exits 0" "0" "${r6rc}"
assert_contains "resume names the prep stage" "${r6}" "resume from stage: prep"
t6c="$(find "${W6C}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)"
[[ -n "${t6c}" && -f "${t6c}/prep.json" ]]   && ok "--from prep dumps prep.json" || bad "prep.json missing on --from prep"
[[ -n "${t6c}" && ! -f "${t6c}/invoice.json" ]] && ok "--from prep --until prep stops before the engineer" || bad "invoice.json present despite --until prep"
assert_not_contains "resume skips the dispatch/intake body" "${r6}" "dispatch starting"
rm -rf "${W6C}"

# --from prep --until engineer: prep then the engineer run on the same artifact
# (proves prep chains into the engineer on resume).
W6D="$(mktemp -d)"
DISPATCH_ARTIFACTS_DIR="${W6D}" bash "${PIPE}" --from prep --artifact "${RJR}" --until engineer >/dev/null 2>&1; r6drc=$?
assert_eq "--from prep --until engineer exits 0" "0" "${r6drc}"
t6d="$(find "${W6D}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)"
if [[ -n "${t6d}" && -f "${t6d}/invoice.json" ]]; then
  assert_eq "prep chained into the engineer (invoice.issue=101)" "101" "$(jq -r '.issue' "${t6d}/invoice.json")"
else
  bad "invoice.json not produced by --from prep --until engineer"
fi
rm -rf "${W6D}"

# prep is in the advertised stage vocabulary (the unknown-stage error lists it).
bogus="$(bash "${PIPE}" --until nope-stage --fixture "${QFIX}" 2>&1)"; bogrc=$?
[[ "${bogrc}" -ne 0 ]] && ok "--until nope-stage exits non-zero" || bad "--until nope-stage should fail"
assert_contains "valid-stage list advertises prep" "${bogus}" "intake workorder prep engineer intake-invoice closure"
# --from prep is an accepted replay stage (no 'unknown --from stage' error).
fromhelp="$(bash "${PIPE}" --from prep 2>&1)";
assert_contains "--from prep is accepted (requires --artifact, not 'unknown stage')" "${fromhelp}" "requires --artifact"

echo
echo "-- 7. live path: operator purpose label wins (C1) + successful-path idempotence --"

# C1 on the LIVE path: an operator `purpose:test` label must drive the harness via
# dispatch.sh (labels threaded into prep), NOT keyword inference on title/body.
LBL="$(mktemp -d)"
LFIX="${LBL}/labeled.json"
jq '[ .[0] + {labels: ((.[0].labels // []) + ["purpose:test"]) } ]' "${QFIX}" > "${LFIX}"
DISPATCH_ARTIFACTS_DIR="${LBL}" bash "${PIPE}" --until prep --fixture "${LFIX}" >/dev/null 2>&1
ltick="$(find "${LBL}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)"
if [[ -n "${ltick}" && -f "${ltick}/prep.json" ]]; then
  assert_eq "live prep honors the operator label (source=label)" "label"    "$(jq -r '.purpose_source' "${ltick}/prep.json")"
  assert_eq "live label -> purpose test"                          "test"     "$(jq -r '.purpose'        "${ltick}/prep.json")"
  assert_eq "live label -> harness standard (no-op)"              "standard" "$(jq -r '.harness'        "${ltick}/prep.json")"
else
  bad "no prep.json from the labeled live --until prep run"
fi
rm -rf "${LBL}"

# C5 idempotence on the SUCCESSFUL path: two identical --until prep ticks dump a
# byte-identical prep.json — the stage is deterministic and side-effect-free, so a
# re-run never diverges or corrupts state.
IDA="$(mktemp -d)"; IDB="$(mktemp -d)"
DISPATCH_ARTIFACTS_DIR="${IDA}" bash "${PIPE}" --until prep --fixture "${QFIX}" >/dev/null 2>&1
DISPATCH_ARTIFACTS_DIR="${IDB}" bash "${PIPE}" --until prep --fixture "${QFIX}" >/dev/null 2>&1
pa="$(find "${IDA}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)/prep.json"
pb="$(find "${IDB}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)/prep.json"
if [[ -f "${pa}" && -f "${pb}" ]] && diff -q "${pa}" "${pb}" >/dev/null 2>&1; then
  ok "successful prep is idempotent (two ticks -> byte-identical prep.json)"
else
  bad "successful prep not idempotent across two ticks"
fi
rm -rf "${IDA}" "${IDB}"

echo
echo "-- 8. blocked issuance rolls back the claim (#127) — no orphan label/worktree --"

# On the LIVE claim path, intake claims the issue (queued -> claimed) and creates
# the pipeline/issue-<n> worktree BEFORE prep runs. If prep then fails safe (an
# un-provisionable harness), the rollback must reverse BOTH so a blocked issuance
# leaves no orphan `claimed` label and no orphan worktree/branch (which a later
# re-claim would collide with via `git worktree add`). Driven through the real
# claim path (devtools dispatch over the queued fixture) with the bogus-harness
# rules from §5; dry-run, so the mutations print as DRY-RUN tokens (asserted) and
# nothing real is touched.
RB="$(PIPELINE_FIXTURE_ISSUES="${QFIX}" DISPATCH_WORKPLAN_RULES="${TMP}/rules-bogus.yml" \
  bash "${PIPE}" devtools dispatch 2>&1)"
assert_contains "intake claimed #101 before prep ran (queued -> claimed)" "${RB}" \
  "issue edit 101 --remove-label queued --add-label claimed"
assert_contains "prep blocked issuance for the bogus harness" "${RB}" "prep blocked issuance"
assert_contains "block triggers the rollback (claimed -> queued)" "${RB}" \
  "rollback: claimed -> queued"
assert_contains "rollback releases the claim label (claimed -> queued)" "${RB}" \
  "issue edit 101 --remove-label claimed --add-label queued"
assert_contains "rollback removes the orphan worktree" "${RB}" \
  "worktree remove --force"
assert_contains "rollback deletes the orphan branch" "${RB}" \
  "branch -D pipeline/issue-101"
assert_not_contains "blocked issuance never dispatches the engineer" "${RB}" "engineer_dispatch"

# The dispatch was dry-run, so NO real worktree/branch can have leaked either —
# the same invariant smoke.sh §7.3 asserts, here on the blocked-issuance path.
[[ ! -d "${ROOT}/.worktrees/issue-101" ]] && ok "no real worktree leaked on a blocked issuance" \
                                          || bad "worktree .worktrees/issue-101 leaked despite rollback"
if git -C "${ROOT}" rev-parse --verify --quiet "pipeline/issue-101" >/dev/null 2>&1; then
  bad "branch pipeline/issue-101 leaked despite rollback"
else
  ok "no real branch leaked on a blocked issuance"
fi

echo
echo "== HARNESS PREP: ${pass} passed, ${fail} failed =="
[[ "${fail}" -eq 0 ]]
