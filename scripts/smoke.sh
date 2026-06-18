#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# smoke.sh — acceptance runner (HANDOFF §7). Exit 0 == all asserts pass.
#
# Runs fully OFFLINE and DRY-RUN: no network, no GitHub, no model calls,
# no mutations. Checks that need an absent engine (gh) degrade to a
# structural/dry-run proxy and emit a SKIP for the live portion.
# ---------------------------------------------------------------------------
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT}" || exit 1

# Deterministic offline test environment.
export PIPELINE_DRY_RUN=1
export CLASSIFIER_OFFLINE=1
export PIPELINE_REPO="${PIPELINE_REPO:-acme/example}"
unset PIPELINE_FIXTURE_ISSUES PIPELINE_FIXTURE_PR PIPELINE_FIX_ATTEMPT 2>/dev/null || true

FIX="${ROOT}/scripts/fixtures"
CLS="${ROOT}/services/classifier"
PASS=0; FAIL=0; SKIP=0
GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; NC=$'\033[0m'

pass() { PASS=$((PASS+1)); printf '  %sPASS%s %s\n' "${GREEN}" "${NC}" "$1"; }
fail() { FAIL=$((FAIL+1)); printf '  %sFAIL%s %s\n' "${RED}" "${NC}" "$1"; [[ -n "${2:-}" ]] && printf '       %s%s%s\n' "${DIM}" "$2" "${NC}"; }
skip() { SKIP=$((SKIP+1)); printf '  %sSKIP%s %s\n' "${YEL}" "${NC}" "$1"; }
section() { printf '\n%s== %s ==%s\n' "${BOLD}" "$1" "${NC}"; }

assert_contains() { case "$2" in *"$3"*) pass "$1";; *) fail "$1" "missing: $3";; esac; }
assert_not_contains() { case "$2" in *"$3"*) fail "$1" "unexpected: $3";; *) pass "$1";; esac; }

have() { command -v "$1" >/dev/null 2>&1; }

# ===========================================================================
# §7 SECTION-NUMBER MAP — single source of truth (smoke-sections-v1, issue #51)
# ---------------------------------------------------------------------------
# Every pillar draws its NEW §7.x sections from its reserved range below. The
# duplicate-id guard near the end of the §7 region (the "section-numbering
# authority" section) fails the suite if two sections ever share a number.
#
# This map is the AUTHORITY: where an individual issue body names a §7.x
# number, this map wins (several issue bodies were filed in parallel and
# collide — that is the bug this section closes). Pick the next free number
# inside your pillar's range; never reuse a number already declared above.
# Gaps are allowed (§7.8 is intentionally vacant — a retired early draft);
# only DUPLICATES are forbidden.
#
#   §7.1–§7.10   Foundational pipeline ......... FROZEN (existing; §7.8 vacant)
#   §7.11        Architect work-prompt ......... FROZEN (existing)
#   §7.12–§7.15  Pillar 1 · Cron / unattended .. flock · reaper · heartbeat · scheduler
#   §7.16–§7.18  Pillar 5 · Debug & harness .... artifact-dump · stage-gating · snapshot/mutate/replay
#   §7.19–§7.20  Pillar 4 · Monitoring ......... run-ledger · digest renderer
#   §7.21–§7.23  Pillar 3 · Budget ............. claude-monitor · invoice reconcile · soft-cap
#   §7.24–§7.25  Day 3 · Research-mode ......... gap-detection · research order · DAG embed
#   §7.26–§7.27  Day 3 · Integration & runbook . full-tick smoke · operator runbook
# ===========================================================================

# ---------------------------------------------------------------------------
section "§7.1 bootstrap-labels.sh idempotent (dry-run, no-op, exit 0)"
r1="$(scripts/bootstrap-labels.sh 2>/dev/null)"; c1=$?
r2="$(scripts/bootstrap-labels.sh 2>/dev/null)"; c2=$?
[[ $c1 -eq 0 && $c2 -eq 0 ]] && pass "both runs exit 0" || fail "exit codes" "run1=$c1 run2=$c2"
d1="$(printf '%s\n' "$r1" | grep '^DRY-RUN:')"
d2="$(printf '%s\n' "$r2" | grep '^DRY-RUN:')"
[[ "$d1" == "$d2" ]] && pass "second run is a no-op (identical intended calls)" || fail "non-idempotent output"
assert_contains "upserts the 'queued' label with --force" "$d1" "gh label create queued"
assert_contains "uses idempotent --force" "$d1" "--force"

# ---------------------------------------------------------------------------
section "§7.2 classify.py: schema-valid + deterministic on 3 fixtures"
schema_ok() {
  jq -e '
    . as $r
    | (["implement","needs-human","wont-do","duplicate?","decompose"] | index($r.action)) != null
    and (["xs","s","m","l"] | index($r.scope)) != null
    and (["gen-local","gen-default","gen-frontier"] | index($r.route)) != null
    and ($r.confidence | type) == "number"
    and $r.confidence >= 0 and $r.confidence <= 1
  ' >/dev/null 2>&1
}
for n in 1 2 3; do
  o1="$(python3 "${CLS}/classify.py" --issue-json "${CLS}/fixtures/issue_${n}.json" 2>/dev/null)"
  o2="$(python3 "${CLS}/classify.py" --issue-json "${CLS}/fixtures/issue_${n}.json" 2>/dev/null)"
  if printf '%s' "$o1" | schema_ok; then pass "issue_${n}: schema-valid ($o1)"; else fail "issue_${n}: schema invalid" "$o1"; fi
  [[ "$o1" == "$o2" ]] && pass "issue_${n}: deterministic across runs" || fail "issue_${n}: nondeterministic" "$o1 vs $o2"
done

# ---------------------------------------------------------------------------
section "§7.3 dry-run dispatch prints correct gh/claude calls, mutates nothing"
disp="$(PIPELINE_FIXTURE_ISSUES="${FIX}/queued-issues.json" scripts/dispatch.sh 2>&1)"
assert_contains "launches engineer_dispatch for #101" "$disp" "job_id"
assert_contains "passes the routed group (gen-default for #101)" "$disp" "gen-default"
assert_contains "passes the issue number" "$disp" '"issue":101'
assert_contains "swaps queued -> claimed" "$disp" "--add-label claimed"
assert_contains "routes low-confidence #103 to needs-human" "$disp" "#103 -> needs-human"
assert_contains "honors concurrency=1 (leaves #102 queued)" "$disp" "#102 eligible"
assert_not_contains "no merge in dispatch" "$disp" "pr merge"
# no mutations: no worktree, no branch created
[[ ! -d "${ROOT}/.worktrees" ]] && pass "no worktree created" || fail "worktree leaked"
if git -C "$ROOT" rev-parse --verify --quiet "pipeline/issue-101" >/dev/null 2>&1; then
  fail "branch pipeline/issue-101 leaked"; else pass "no branch created"; fi

# ---------------------------------------------------------------------------
section "§7.4 fix-dispatch ladder: attempt 2 -> gen-default; >3 -> operator notify"
f2="$(PIPELINE_FIXTURE_PR="${FIX}/pr-fix-attempt-2.json" scripts/fix-dispatch.sh 2>&1)"
assert_contains "attempt 2 selects gen-default" "$f2" "gen-default"
assert_not_contains "attempt 2 does not escalate" "$f2" "needs-human"
fc="$(PIPELINE_FIXTURE_PR="${FIX}/pr-fix-attempt-over-cap.json" scripts/fix-dispatch.sh 2>&1)"
assert_contains "attempt >3 labels needs-human" "$fc" "needs-human"
assert_contains "attempt >3 posts a PR comment for the operator" "$fc" "pr comment"
assert_not_contains "attempt >3 does NOT invoke /fix-ci" "$fc" "/fix-ci"

# Also exercise the label-derivation path (no explicit attempt) for robustness.
der="$(PIPELINE_FIXTURE_PR=<(jq 'del(.attempt)' "${FIX}/pr-fix-attempt-2.json") scripts/fix-dispatch.sh 2>&1)"
assert_contains "label-derived attempt (fix-attempt-1 -> 2) also picks gen-default" "$der" "gen-default"

# ---------------------------------------------------------------------------
section "§7.5 every secret referenced is documented in pipeline.env.example"
ENVF="${ROOT}/pipeline.env.example"
required_secrets=(GITHUB_TOKEN TRIAGE_GITHUB_TOKEN WORKER_GITHUB_TOKEN CLOSURE_GITHUB_TOKEN \
  ANTHROPIC_API_KEY)
for v in "${required_secrets[@]}"; do
  ln="$(grep -nE "^${v}=" "$ENVF" | head -1 | cut -d: -f1)"
  if [[ -z "$ln" ]]; then fail "secret ${v} not declared in example"; continue; fi
  start=$(( ln>3 ? ln-3 : 1 ))
  if sed -n "${start},$((ln-1))p" "$ENVF" | grep -q '#'; then
    pass "${v} documented with a comment"
  else
    fail "${v} present but lacks an adjacent comment"
  fi
done
# Reverse scan: any *_TOKEN/_KEY/_SECRET referenced in pipeline runtime files
# must be documented. Exclude smoke.sh itself (its regex literals self-match).
referenced="$(grep -rhoE '[A-Z][A-Z0-9_]*(_TOKEN|_KEY|_SECRET)' \
  scripts services .github --exclude=smoke.sh 2>/dev/null | sort -u)"
undocumented=""
while IFS= read -r v; do
  [[ -z "$v" ]] && continue
  grep -qE "${v}=" "$ENVF" || undocumented+="${v} "
done <<< "$referenced"
[[ -z "$undocumented" ]] && pass "no undocumented secret-like vars referenced" \
  || fail "undocumented secret-like vars" "$undocumented"

# ---------------------------------------------------------------------------
section "§7.6 closure.sh dry-run (success payload): auto-merge + summary"
clo="$(PIPELINE_FIXTURE_PR="${FIX}/closure-success.json" scripts/closure.sh 2>&1)"
assert_contains "arms auto-merge (--auto)" "$clo" "--auto"
assert_contains "squash merge" "$clo" "--squash"
assert_contains "posts a closure summary comment" "$clo" "closure summary"
assert_not_contains "does not hard-merge (no 'merge --admin')" "$clo" "--admin"

# ---------------------------------------------------------------------------
section "§7.7 Invoice schema: all three status-case fixtures are schema-valid"
python3 - "${FIX}/invoice-completed.json" \
          "${FIX}/invoice-failed.json" \
          "${FIX}/invoice-needs-human.json" <<'PY'
import json, sys, re

REQUIRED = {"invoice_id","issue","repo","status","route_used","cost","summary","timestamp"}
COST_REQUIRED = {"tokens_in","tokens_out","duration_seconds"}
STATUS_VALUES = {"completed","failed","partial","needs-human"}
SCOPE_VALUES  = {"xs","s","m","l","xl"}
ROUTE_VALUES  = {"gen-local","gen-default","gen-frontier"}

errors = []
for path in sys.argv[1:]:
    name = path.split("/")[-1]
    try:
        with open(path) as fh:
            inv = json.load(fh)
    except Exception as e:
        errors.append(f"{name}: JSON parse error: {e}")
        continue
    missing = REQUIRED - inv.keys()
    if missing:
        errors.append(f"{name}: missing required fields: {sorted(missing)}")
    if not isinstance(inv.get("issue"), int):
        errors.append(f"{name}: 'issue' must be integer")
    if inv.get("status") not in STATUS_VALUES:
        errors.append(f"{name}: 'status' must be one of {STATUS_VALUES}")
    if "scope_actual" in inv and inv["scope_actual"] not in SCOPE_VALUES:
        errors.append(f"{name}: 'scope_actual' must be one of {SCOPE_VALUES}")
    if inv.get("route_used") not in ROUTE_VALUES:
        errors.append(f"{name}: 'route_used' must be one of {ROUTE_VALUES}")
    cost = inv.get("cost", {})
    if not isinstance(cost, dict) or not COST_REQUIRED.issubset(cost.keys()):
        errors.append(f"{name}: 'cost' missing sub-fields {COST_REQUIRED - set(cost)}")
    ts = inv.get("timestamp", "")
    if not re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', ts):
        errors.append(f"{name}: 'timestamp' must be ISO 8601")

if errors:
    for e in errors: print(f"  ERR {e}")
    sys.exit(1)
PY
if [[ $? -eq 0 ]]; then
  pass "invoice-completed, invoice-failed, invoice-needs-human all pass schema check"
else
  fail "one or more invoice fixtures failed schema validation"
fi

# ---------------------------------------------------------------------------
section "§7.9 dispatch work-order emission (offline, deterministic)"
FXQ="${ROOT}/services/architect/fixtures/play-queue.json"
wo1="$(PIPELINE_DRY_RUN=1 ./dispatch --fixture "$FXQ" 2>/dev/null)"; wc1=$?
wo2="$(PIPELINE_DRY_RUN=1 ./dispatch --fixture "$FXQ" 2>/dev/null)"
[[ $wc1 -eq 0 ]] && pass "dispatch exits 0 on the fixture queue" || fail "dispatch exit" "got $wc1"
[[ "$wo1" == "$wo2" ]] && pass "work order is deterministic across runs" || fail "work order nondeterministic"
assert_contains "emits a WORK PLAN header" "$wo1" "WORK PLAN"
assert_contains "selects the foundational primary (#2)" "$wo1" "Closes #2"
assert_contains "carries the routed tier (gen-default for #2)" "$wo1" "gen-default"
assert_contains "carries the scope budget (m -> 80,000)" "$wo1" "80,000 tokens"
assert_contains "has prompt-master STOP CONDITIONS" "$wo1" "STOP CONDITIONS"
assert_contains "has FORBIDDEN ACTIONS (ADMIN constraint)" "$wo1" "FORBIDDEN ACTIONS"
assert_contains "carries the DRY_RUN flag" "$wo1" "DRY_RUN=1"
assert_contains "names the Engineer's verify gate" "$wo1" "bash scripts/smoke.sh"
assert_contains "includes the Invoice format" "$wo1" "INVOICE FORMAT"
assert_contains "frames issue text as untrusted data" "$wo1" "untrusted data"
# Self-contained work order: units of work + staffing + embedded resources (the §3 enhancement).
assert_contains "decomposes into UNITS OF WORK" "$wo1" "UNITS OF WORK"
assert_contains "labels a unit with a <domain> <function> specialist" "$wo1" "developer tooling (shell) engineer"
assert_contains "labels the integration unit QA / test automation" "$wo1" "QA / test automation engineer"
assert_contains "emits a STAFFING plan" "$wo1" "STAFFING"
assert_contains "staffs one agent per unit (3 units -> 3 agents)" "$wo1" "Employ 3 agent"
assert_contains "embeds resources in-band (EMBEDDED RESOURCES)" "$wo1" "EMBEDDED RESOURCES"
assert_contains "embeds the worker contract (CLAUDE.md)" "$wo1" "Worker contract"
assert_contains "embeds real referenced source (load_queued_issues)" "$wo1" "load_queued_issues"
# PHASE 1 no longer sends the engineer off to read files — it points at the embedded bundle.
p1="$(printf '%s\n' "$wo1" | grep -m1 '^PHASE 1')"
assert_contains "PHASE 1 points at the embedded bundle" "$p1" "embedded above"
assert_contains "PHASE 1 forbids fetching/researching more files" "$p1" "Do NOT fetch or research"
# --all emits one work order per eligible job (#2,#3,#5).
allc="$(PIPELINE_DRY_RUN=1 ./dispatch --fixture "$FXQ" --all 2>/dev/null | grep -c '^# OBJECTIVE')"
[[ "$allc" == "3" ]] && pass "--all emits 3 eligible work orders (#2,#3,#5)" || fail "--all count" "got $allc"
# --json is a valid envelope carrying work_order + authorization.
if PIPELINE_DRY_RUN=1 ./dispatch --fixture "$FXQ" --json 2>/dev/null \
   | jq -e '.work_order and .authorization.budget_tokens and (.issue==2)' >/dev/null; then
  pass "--json emits a valid envelope (work_order + authorization)"
else
  fail "--json envelope invalid"
fi
# Selection filters the wont-do human task (#9) and the low-confidence defer (#7).
elig="$(PIPELINE_DRY_RUN=1 ./dispatch --fixture "$FXQ" 2>&1 >/dev/null)"
assert_contains "skips the wont-do human task (#9)" "$elig" "wont-do"
assert_contains "defers low-confidence #7" "$elig" "conf 0.34"

# ---------------------------------------------------------------------------
section "§7.10 static issue DAG + operator issue selection (offline, deterministic)"
DAGDIR="$(mktemp -d 2>/dev/null || mktemp -d -t dag)"
DISPATCH_ARTIFACTS_DIR="$DAGDIR/a" ./dispatch --dag --fixture "$FXQ" >/dev/null 2>&1; dc=$?
DISPATCH_ARTIFACTS_DIR="$DAGDIR/b" ./dispatch --dag --fixture "$FXQ" >/dev/null 2>&1
[[ $dc -eq 0 ]] && pass "dispatch --dag exits 0" || fail "dispatch --dag exit" "got $dc"
[[ -f "$DAGDIR/a/issue-dag.json" && -f "$DAGDIR/a/issue-dag.md" ]] \
  && pass "writes issue-dag.json and issue-dag.md" || fail "DAG artifacts missing"
djson="$(cat "$DAGDIR/a/issue-dag.json" 2>/dev/null)"
dmd="$(cat "$DAGDIR/a/issue-dag.md" 2>/dev/null)"
# Machine view: roots are the unblocked issues; edges encode the dependencies.
if printf '%s' "$djson" | jq -e '.roots == [2,9]' >/dev/null 2>&1; then
  pass "DAG json roots are the unblocked issues (#2,#9)"
else
  fail "DAG json roots wrong" "$(printf '%s' "$djson" | jq -c '.roots' 2>/dev/null)"
fi
if printf '%s' "$djson" | jq -e 'any(.edges[]; .==[5,2]) and any(.edges[]; .==[7,2])' >/dev/null 2>&1; then
  pass "DAG json edges encode #5->#2 and #7->#2 (dependents -> dependency)"
else
  fail "DAG json edges wrong"
fi
# Human views: a Mermaid graph and a Markdown table (the operator browses these).
assert_contains "DAG md carries a Mermaid graph" "$dmd" '```mermaid'
assert_contains "DAG md carries an issues table" "$dmd" "| Issue | Title |"
assert_contains "DAG md names the selection command" "$dmd" "--issue"
# The static artifact is deterministic across runs (no wall-clock, sorted output).
if diff -q "$DAGDIR/a/issue-dag.json" "$DAGDIR/b/issue-dag.json" >/dev/null 2>&1 \
   && diff -q "$DAGDIR/a/issue-dag.md" "$DAGDIR/b/issue-dag.md" >/dev/null 2>&1; then
  pass "DAG artifacts are byte-identical across runs"
else
  fail "DAG artifacts nondeterministic"
fi
# Operator selection: --issue N emits that issue's work order (override) and notes deps.
i5out="$(./dispatch --issue 5 --fixture "$FXQ" 2>/dev/null)"; i5c=$?
i5err="$(./dispatch --issue 5 --fixture "$FXQ" 2>&1 >/dev/null)"
[[ $i5c -eq 0 ]] && pass "dispatch --issue 5 exits 0" || fail "--issue 5 exit" "got $i5c"
assert_contains "--issue 5 emits the work order for #5 (Closes #5)" "$i5out" "Closes #5"
assert_contains "--issue 5 notes the unmet dependency on #2" "$i5err" "depends on #2"
# An unknown issue number is rejected (no eligible job).
./dispatch --issue 999 --fixture "$FXQ" >/dev/null 2>&1; i9c=$?
[[ $i9c -eq 3 ]] && pass "--issue with an unknown number exits 3" || fail "--issue 999 exit" "got $i9c"
# The declarative tuning surface (the admin-editable config) is valid JSON.
if jq -e . "${ROOT}/services/tuning.json" >/dev/null 2>&1; then
  pass "services/tuning.json is valid JSON (the tuning surface)"
else
  fail "services/tuning.json is not valid JSON"
fi
rm -rf "$DAGDIR"

# ---------------------------------------------------------------------------
section "§7.11 tighten work prompt: epic decomposition + issue-derived criteria + target-aware gate (offline)"
EPQ="${ROOT}/services/architect/fixtures/epic-queue.json"

# (a) Epic decomposition: --issue <epic> emits one work order per implementable child.
ep1="$(PIPELINE_DRY_RUN=1 ./dispatch --fixture "$EPQ" --issue 1 2>/dev/null)"; epc=$?
ep1b="$(PIPELINE_DRY_RUN=1 ./dispatch --fixture "$EPQ" --issue 1 2>/dev/null)"
[[ $epc -eq 0 ]] && pass "dispatch --issue <epic> exits 0" || fail "epic decompose exit" "got $epc"
[[ "$ep1" == "$ep1b" ]] && pass "epic decomposition is byte-identical across runs" || fail "epic decomposition nondeterministic"
nwo="$(printf '%s\n' "$ep1" | grep -c '^# OBJECTIVE')"
[[ "$nwo" == "2" ]] && pass "epic #1 decomposes into 2 child work orders (#4,#5)" || fail "epic child count" "got $nwo (want 2)"
assert_contains "epic decomposition emits child #4 (Closes #4)" "$ep1" "Closes #4"
assert_contains "epic decomposition emits child #5 (Closes #5)" "$ep1" "Closes #5"

# (b) An [Epic] is a container, never dispatched as an atomic unit of work.
epelig="$(PIPELINE_DRY_RUN=1 ./dispatch --fixture "$EPQ" 2>&1 >/dev/null)"
epdef="$(PIPELINE_DRY_RUN=1 ./dispatch --fixture "$EPQ" 2>/dev/null)"
assert_contains "classifier routes [Epic] #1 to decomposition" "$epelig" "decompose"
assert_contains "default selection picks a child as primary (Closes #4)" "$epdef" "Closes #4"
assert_not_contains "default selection never dispatches the [Epic] itself (no Closes #1)" "$epdef" "Closes #1"

# (c) Issue-derived DONE CRITERIA: the child's own acceptance bullets become the checklist.
dc4="$(printf '%s\n' "$ep1" | awk '/DONE CRITERIA/{f=1;next} f&&/^# /{exit} f')"
assert_contains "DONE CRITERIA lifts a real issue bullet, not a generic placeholder" "$dc4" "the dev server starts without errors"
crit="$(python3 -c "import sys; sys.path.insert(0,'${ROOT}/services/architect'); import decompose; print('|'.join(decompose.extract_criteria('## Acceptance criteria\n- alpha one\n- **beta** two\n')))")"
[[ "$crit" == "alpha one|beta two" ]] && pass "extract_criteria lifts bullets (markdown stripped) under an acceptance heading" || fail "extract_criteria wrong" "got '$crit'"

# (d) Target-aware verify gate (verify.py): override > detection (smoke.sh / npm / make) > GENERIC.
vout="$(python3 - "${ROOT}" <<'PY'
import os, sys, tempfile
sys.path.insert(0, os.path.join(sys.argv[1], "services", "architect"))
import verify
res = {}
with tempfile.TemporaryDirectory() as d:
    npmd = os.path.join(d, "npm"); os.mkdir(npmd)
    with open(os.path.join(npmd, "package.json"), "w") as fh:
        fh.write('{"scripts": {"test": "jest"}}')
    bare = os.path.join(d, "bare"); os.mkdir(bare)
    res["npm"] = verify.resolve_verify_cmd(repo_root=npmd, live=False)
    res["bare"] = verify.resolve_verify_cmd(repo_root=bare, live=False)
    res["override"] = verify.resolve_verify_cmd(repo_root=bare, override="make check", live=False)
res["smoke"] = verify.resolve_verify_cmd(repo_root=sys.argv[1], live=False)
res["generic"] = verify.GENERIC
for k, v in res.items():
    print(f"{k}={v}")
PY
)"
vget() { printf '%s\n' "$vout" | sed -n "s/^$1=//p"; }
[[ "$(vget npm)" == "npm test" ]] && pass "verify gate resolves 'npm test' from package.json scripts" || fail "verify npm gate" "got '$(vget npm)'"
[[ "$(vget bare)" == "$(vget generic)" ]] && pass "verify gate falls back to GENERIC for a bare repo" || fail "verify generic gate" "got '$(vget bare)'"
[[ "$(vget smoke)" == "bash scripts/smoke.sh" ]] && pass "verify gate resolves 'bash scripts/smoke.sh' for the dispatch repo" || fail "verify smoke gate" "got '$(vget smoke)'"
[[ "$(vget override)" == "make check" ]] && pass "verify gate honors an explicit override verbatim" || fail "verify override" "got '$(vget override)'"

# ---------------------------------------------------------------------------
section "§7.12 cron: flock tick mutex serializes overlapping ticks (offline, deterministic)"
LOCKD="$(mktemp -d 2>/dev/null || mktemp -d -t dlock)"
LF="${LOCKD}/tick.lock"
FXI="${FIX}/queued-issues.json"
# (2) No-flock fallback: force flock "absent" via FLOCK_BIN; the tick must WARN
#     (naming the missing tool) and still complete the dry-run tick unlocked.
nf="$(FLOCK_BIN="flock-missing-shim" DISPATCH_LOCK_FILE="${LF}" \
      bash scripts/pipeline.sh --fixture "${FXI}" 2>&1)"; nfc=$?
[[ $nfc -eq 0 ]] && pass "no-flock fallback completes the tick (exit 0)" || fail "no-flock tick exit" "got $nfc"
assert_contains "no-flock fallback WARNs, naming the missing tool" "$nf" "flock-missing-shim not on PATH"
assert_contains "no-flock fallback still runs the dispatch body" "$nf" "pipeline: dispatch starting"
if have flock; then
  # (1) Serialization: the harness holds the lock on fd 8; a concurrent tick
  #     must skip, exit 0, and claim nothing.
  exec 8>"${LF}"; flock -n 8 || fail "harness could not acquire the test lock"
  held="$(DISPATCH_LOCK_FILE="${LF}" bash scripts/pipeline.sh --fixture "${FXI}" 2>&1)"; hc=$?
  exec 8>&-   # release the test lock
  [[ $hc -eq 0 ]] && pass "contended tick exits 0 (a skipped overlap is success)" || fail "contended tick exit" "got $hc"
  assert_contains "contended tick logs the lock-held skip" "$held" "another tick holds the lock"
  assert_not_contains "contended tick claims nothing (no --add-label claimed)" "$held" "--add-label claimed"
  # (3) Clean release: the next tick acquires the lock without seeing it held.
  freed="$(DISPATCH_LOCK_FILE="${LF}" bash scripts/pipeline.sh --fixture "${FXI}" 2>&1)"; fcd=$?
  [[ $fcd -eq 0 ]] && pass "released tick exits 0" || fail "released tick exit" "got $fcd"
  assert_not_contains "released tick does not see a held lock" "$freed" "another tick holds the lock"
  assert_contains "released tick runs the dispatch body under the lock" "$freed" "pipeline: dispatch starting"
else
  skip "flock not on PATH — serialization + clean-release asserts (Linux prod-host only)"
fi
rm -rf "${LOCKD}"

# ---------------------------------------------------------------------------
# §7 section-numbering authority (smoke-sections-v1, issue #51). Enforces the
# MAP at the top of the §7 region: no two §7.x sections may share a number.
# Gaps are allowed; only duplicates fail. This guard lets parallel pillar
# issues add §7.x sections without silently colliding.
section "§7 section-numbering authority — duplicate §7.x id guard (smoke-sections-v1)"
# Duplicated values from a newline-delimited list on stdin (prints nothing if none).
dup_ids() { sort | uniq -d; }
# §7.x numbers actually declared via section "§7.N ..." in a script file.
declared_7x() { grep -oE 'section "§?7\.[0-9]+' "$1" | grep -oE '7\.[0-9]+'; }
real_ids="$(declared_7x "${BASH_SOURCE[0]}")"
real_dups="$(printf '%s\n' "$real_ids" | dup_ids)"
if [[ -z "$real_dups" ]]; then
  pass "no duplicate §7.x section ids in smoke.sh ($(printf '%s\n' "$real_ids" | grep -c .) sections)"
else
  fail "duplicate §7.x section ids present" "$(printf '%s' "$real_dups" | tr '\n' ' ')"
fi
# Negative self-test: the guard must FLAG a duplicate when one is injected.
inj_dups="$(printf '7.3\n7.9\n7.3\n' | dup_ids)"
[[ "$inj_dups" == "7.3" ]] && pass "guard flags an injected duplicate (§7.3)" \
  || fail "guard failed to detect an injected duplicate" "got '$inj_dups'"

# ---------------------------------------------------------------------------
section "§8 security guardrails (checkable)"
# Dry-run defaults ON.
dflt="$(env -u PIPELINE_DRY_RUN bash -c 'source "'"${ROOT}"'/scripts/lib/common.sh"; echo "$PIPELINE_DRY_RUN"')"
[[ "$dflt" == "1" ]] && pass "PIPELINE_DRY_RUN defaults ON (=1)" || fail "dry-run not default-on" "got '$dflt'"
# Classifier (quarantine reader) has no exec capability. AST-based so the
# docstring that *names* the forbidden APIs does not trip the check.
if python3 - "${CLS}/classify.py" "${CLS}/classify_local_stub.py" <<'PY'
import ast, sys
BAD_IMPORTS = {"subprocess", "pty", "ctypes", "multiprocessing"}
BAD_NAMES = {"eval", "exec", "__import__", "compile"}
BAD_ATTRS = {"system", "popen", "Popen", "spawn", "spawnv", "spawnl", "run", "call", "check_output"}
bad = []
for path in sys.argv[1:]:
    with open(path) as fh:
        tree = ast.parse(fh.read(), path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BAD_IMPORTS:
                    bad.append(f"{path}: import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in BAD_IMPORTS:
                bad.append(f"{path}: from {node.module}")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in BAD_NAMES:
                bad.append(f"{path}: {f.id}()")
            if isinstance(f, ast.Attribute) and f.attr in BAD_ATTRS:
                # subprocess.* / os.system / .popen etc.
                base = getattr(f.value, "id", "")
                if base in ("os", "subprocess") or f.attr in ("system", "popen", "Popen"):
                    bad.append(f"{path}: .{f.attr}()")
sys.exit(1 if bad else 0)
PY
then
  pass "classifier has zero exec/tool capability (quarantine reader)"
else
  fail "classifier exposes exec capability"
fi
# No real secret values committed: required-secret lines in example are empty/placeholder.
leaked=""
for v in "${required_secrets[@]}"; do
  val="$(grep -E "^${v}=" "$ENVF" | head -1 | cut -d= -f2-)"
  case "$val" in ""|"<"*|*"example"*|*"changeme"*) : ;; *) leaked+="${v} ";; esac
done
[[ -z "$leaked" ]] && pass "no real secret values in committed example" \
  || fail "committed example has non-placeholder secret value(s)" "$leaked"
# pipeline.env (the filled-in secrets file) is gitignored.
grep -qxF 'pipeline.env' "${ROOT}/.gitignore" && pass "pipeline.env is gitignored" \
  || fail "pipeline.env not gitignored"

# ---------------------------------------------------------------------------
printf '\n%s========================================%s\n' "${BOLD}" "${NC}"
printf '  %sPASS=%d%s  %sFAIL=%d%s  %sSKIP=%d%s\n' \
  "${GREEN}" "$PASS" "${NC}" "${RED}" "$FAIL" "${NC}" "${YEL}" "$SKIP" "${NC}"
printf '%s========================================%s\n' "${BOLD}" "${NC}"
[[ $FAIL -eq 0 ]] && { echo "smoke: PASS"; exit 0; } || { echo "smoke: FAIL"; exit 1; }
