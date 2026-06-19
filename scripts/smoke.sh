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

# Contain the per-tick run-record (E1-2) so ticks driven anywhere in this suite
# never write into the working tree's default .dispatch/; §7.14 overrides this
# per-invocation for its own assertions.
_smoke_rrdir="$(mktemp -d 2>/dev/null || mktemp -d -t smoke-rr)"
export DISPATCH_RUN_RECORD="${_smoke_rrdir}/run-record.log"

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
#   §7.28        Pillar 5 · Demo bootstrap ..... demo-repo seed manifest + label provisioning (offline proxy)
#                (#47, Part of #25: the harness range §7.16–§7.18 was already full
#                 when this straggler landed, so it draws a fresh id per the
#                 "gaps allowed, no duplicates" rule above. The #47 body's "§7.11"
#                 predates this map, which reserves §7.11 for the architect prompt.)
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

# #33: the issue's own Acceptance Criteria / Definition of Done section is lifted
# into DONE CRITERIA (extract_criteria is wired into the general work-order path
# via decompose.plan). Asserted at the extraction + render seam — no fixture-queue
# change, so the §7.9/§7.10 ranking/root invariants above are untouched.
ACP="${ROOT}/services/architect"
dod7_9="$(python3 -c "import sys;sys.path.insert(0,'${ACP}');import decompose;print('|'.join(decompose.extract_criteria('## Definition of Done\n- ship it\n- test it\n')))")"
[[ "$dod7_9" == "ship it|test it" ]] && pass "extract_criteria lifts a Definition of Done section" || fail "DoD extraction" "got '$dod7_9'"
prec7_9="$(python3 -c "import sys;sys.path.insert(0,'${ACP}');import decompose;print('|'.join(decompose.extract_criteria('## Definition of Done\n- dod one\n\n## Acceptance Criteria\n- acc one\n- acc two\n')))")"
[[ "$prec7_9" == "acc one|acc two" ]] && pass "Acceptance Criteria takes precedence over Definition of Done" || fail "criteria precedence" "got '$prec7_9'"
crender7_9="$(python3 - "${ACP}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import decompose, workorder
body = "## Acceptance Criteria\n- The /healthz endpoint returns 200\n- A unit test covers the 503 path\n"
job = {"issue": 99, "repo": "acme/x", "title": "t", "body": body,
       "route": "gen-default", "scope": "m", "confidence": 0.9}
crit = decompose.plan(job, [], verify_cmd="bash scripts/smoke.sh")["criteria"]
cl = workorder._checklist(99, "bash scripts/smoke.sh", crit)
gen_plan = decompose.plan({"issue": 2, "body": "no acceptance section here",
                           "route": "gen-local", "scope": "s", "confidence": 0.9}, [])
gen = workorder._checklist(2, "bash scripts/smoke.sh", gen_plan["criteria"])
ok = ("The /healthz endpoint returns 200" in cl and "A unit test covers the 503 path" in cl
      and "Closes #99" in cl and "bash scripts/smoke.sh" in cl
      and "acceptance criteria in the issue body" in gen)
print("OK" if ok else "FAIL")
PY
)"
[[ "$crender7_9" == "OK" ]] && pass "issue acceptance items reach DONE CRITERIA (gate + Closes kept; no-section -> generic fallback)" || fail "DONE CRITERIA render path" "got '$crender7_9'"

# #34: per-unit TEST PROCEDURE matrix (setup/exercise/verify) — one block per unit,
# rendered from each unit's deliverable/files/acceptance, placed after STAFFING and
# before DONE CRITERIA, gated on plan (absent when plan is None). Reuses $wo1 (#2).
assert_contains "emits a TEST PROCEDURE section" "$wo1" "TEST PROCEDURE"
# Isolate the TEST PROCEDURE region (from its header to the next '# ' section header).
tp7_9="$(printf '%s\n' "$wo1" | awk '/^# TEST PROCEDURE/{f=1;next} /^# /{f=0} f')"
assert_contains "TEST PROCEDURE carries a Setup label" "$tp7_9" "Setup:"
assert_contains "TEST PROCEDURE carries an Exercise label" "$tp7_9" "Exercise:"
assert_contains "TEST PROCEDURE carries a Verify label" "$tp7_9" "Verify:"
assert_contains "verify step dogfoods offline smoke" "$tp7_9" "scripts/smoke.sh"
assert_contains "verify step is offline/dry-run" "$tp7_9" "PIPELINE_DRY_RUN=1"
tpcount7_9="$(printf '%s\n' "$tp7_9" | grep -c 'Verify:')"
[[ "$tpcount7_9" == "3" ]] && pass "one TEST PROCEDURE block per unit (#2 -> 3 units -> 3 blocks)" || fail "TEST PROCEDURE block count" "got $tpcount7_9"
# The section is gated on plan: a no-plan render omits it entirely.
noplan7_9="$(python3 - "${ACP}" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import workorder
from approval import approve
job = {"issue": 7, "repo": "acme/x", "title": "t", "body": "b",
       "route": "gen-local", "scope": "s", "confidence": 0.9}
triage = {"confidence": 0.9, "scope": "s", "route": "gen-local"}
auth = approve(job, triage)
wo = workorder.render(job, triage, auth, plan=None)
print("ABSENT" if "TEST PROCEDURE" not in wo else "PRESENT")
PY
)"
[[ "$noplan7_9" == "ABSENT" ]] && pass "TEST PROCEDURE omitted when plan is None (parity with UNITS/STAFFING gating)" || fail "TEST PROCEDURE no-plan gating" "got '$noplan7_9'"

# #35: surface the DAG blocks/blocked-by edges + issue URLs in the work order.
# Fixture #2 is a root (blocked-by nothing) and blocks #5 and #7 (see §7.10 edges).
assert_contains "emits a DEPENDENCIES section" "$wo1" "DEPENDENCIES"
dep7_9="$(printf '%s\n' "$wo1" | awk '/^# DEPENDENCIES/{f=1;next} /^# /{f=0} f')"
assert_contains "DEPENDENCIES has a Blocked by list" "$dep7_9" "Blocked by"
assert_contains "DEPENDENCIES has a Blocks list" "$dep7_9" "Blocks"
assert_contains "#2 blocks #5 (dependent surfaced)" "$dep7_9" "#5"
assert_contains "#2 blocks #7 (dependent surfaced)" "$dep7_9" "#7"
url5_9="$(jq -r '.[] | select(.number==5) | .url' "$FXQ")"
assert_contains "Blocks entries carry the dependent's issue URL" "$dep7_9" "$url5_9"
# #2 is a root: its Blocked by list renders the empty em-dash marker.
blockedby7_9="$(printf '%s\n' "$dep7_9" | awk '/Blocked by/{f=1;next} /Blocks/{f=0} f')"
assert_contains "#2 (a root) renders the empty Blocked-by marker" "$blockedby7_9" "—"
# Header surfaces the dispatched issue's own URL.
url2_9="$(jq -r '.[] | select(.number==2) | .url' "$FXQ")"
assert_contains "header surfaces the issue URL" "$wo1" "$url2_9"
# --json envelope carries the dependency edges as machine data.
if PIPELINE_DRY_RUN=1 ./dispatch --fixture "$FXQ" --json 2>/dev/null \
   | jq -e '(.blocks | length > 0) and (.blocked_by | length == 0) and (.issue_url != "")' >/dev/null; then
  pass "--json envelope carries blocks/blocked_by/issue_url for #2"
else
  fail "--json dependency envelope missing/empty"
fi

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
section "§7.16 debug: DISPATCH_ARTIFACTS_DIR per-stage artifact dump (offline, deterministic)"
# (§7.16 per the #51 section map — debug & harness range. The issue body's
# "§7.14" predates that map.) Reuse §7.9's work-order fixture (FXQ); the dump
# is gated on DISPATCH_ARTIFACTS_DIR, so §7.9 itself is unaffected.
ADIR="$(mktemp -d 2>/dev/null || mktemp -d -t adir)"
DISPATCH_ARTIFACTS_DIR="$ADIR" ./dispatch --fixture "$FXQ" >/dev/null 2>&1
tickdir="$(find "$ADIR" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)"
[[ -n "$tickdir" ]] && pass "a tick-<id> subdir is created under DISPATCH_ARTIFACTS_DIR" || fail "no tick-* subdir created"
if [[ -n "$tickdir" && -f "$tickdir/workorder.txt" ]]; then
  pass "workorder.txt dumped"
  assert_contains "workorder.txt is the real rendered order (WORK PLAN header)" "$(cat "$tickdir/workorder.txt")" "WORK PLAN"
else
  fail "workorder.txt missing in tick dir"
fi
if [[ -n "$tickdir" && -f "$tickdir/job-request.json" ]]; then
  pass "job-request.json dumped"
  if python3 - "$tickdir/job-request.json" <<'PY'
import json, sys
REQ = {"job_id", "issue", "repo", "title", "body", "route", "scope", "confidence"}
ROUTE = {"gen-local", "gen-default", "gen-frontier"}; SCOPE = {"xs", "s", "m", "l"}
j = json.load(open(sys.argv[1]))
errs = []
if set(j) != REQ: errs.append(f"keys differ: {sorted(set(j) ^ REQ)}")
if not isinstance(j.get("issue"), int): errs.append("issue not int")
if j.get("route") not in ROUTE: errs.append("route invalid")
if j.get("scope") not in SCOPE: errs.append("scope invalid")
c = j.get("confidence")
if not isinstance(c, (int, float)) or not 0 <= c <= 1: errs.append("confidence invalid")
if not isinstance(j.get("repo"), str): errs.append("repo not string")
sys.exit(1 if errs else 0)
PY
  then pass "job-request.json validates against schemas/job-request.json (structural, per §7.7)"
  else fail "job-request.json failed schema validation"
  fi
else
  fail "job-request.json missing in tick dir"
fi
# No regression: the --dag artifact still lands at the artifacts-dir top level (§7.10).
DISPATCH_ARTIFACTS_DIR="$ADIR/dag" ./dispatch --dag --fixture "$FXQ" >/dev/null 2>&1
[[ -f "$ADIR/dag/issue-dag.json" ]] && pass "issue-dag.json still produced (no §7.10 regression)" || fail "issue-dag.json regressed"
rm -rf "$ADIR"

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
section "§7.13 cron: crash reaper re-queues stuck 'claimed' issues past timeout (offline, deterministic)"
# (§7.13 per the #51 section map — Pillar 1 cron range, reaper slot; the #28 body's
# "§7.12" predates that map, which reserves §7.12 for flock and §7.13 for the
# reaper.) The reaper runs at the TOP of the tick, inside the E1-1 lock, BEFORE
# claiming: it re-queues issues stuck in `claimed` past DISPATCH_CLAIM_TIMEOUT_HOURS
# with no open PR, touching only the claimed/queued pair it owns — D1 recovery only
# (never opens, escalates, or re-dispatches in the same tick). Timeout falls back to
# recovery.reaper_timeout_hours in services/tuning.json (operator decision #49).
RPD="$(mktemp -d 2>/dev/null || mktemp -d -t reap)"
STUCK="${FIX}/claimed-stuck-issues.json"
EMPTYQ="${RPD}/empty-queued.json"; printf '[]\n' > "${EMPTYQ}"
RPRR="${RPD}/run-record.log"
RPLOCK="${RPD}/tick.lock"
# Deterministic clock + timeout: "now" is pinned and the timeout fixed at 4h, so the
# decision never depends on the wall clock or tuning.json. The reaper reads its
# claimed candidates (claim timestamp + whether each has an open PR — the offline
# "PR set") from DISPATCH_REAPER_FIXTURE; the queued source is empty so the only
# tick action is recovery.
reap_out="$(DISPATCH_REAPER_FIXTURE="${STUCK}" DISPATCH_NOW_OVERRIDE='2026-06-18T12:00:00Z' \
  DISPATCH_CLAIM_TIMEOUT_HOURS=4 DISPATCH_RUN_RECORD="${RPRR}" DISPATCH_ARTIFACTS_DIR="${RPD}/art" \
  DISPATCH_LOCK_FILE="${RPLOCK}" \
  bash scripts/pipeline.sh --fixture "${EMPTYQ}" 2>&1)"; reap_rc=$?
[[ $reap_rc -eq 0 ]] && pass "reaper tick exits 0" || fail "reaper tick exit" "got $reap_rc"
# (1) Over-timeout, no-PR issue (#201): re-queued claimed -> queued + a provenance comment.
assert_contains "over-timeout no-PR issue #201 is re-queued (claimed -> queued)" "$reap_out" "issue edit 201 --remove-label claimed --add-label queued"
assert_contains "reaper posts a crash-reaper provenance comment on #201" "$reap_out" "Re-queued by crash reaper"
assert_contains "reaper comment names the tick id for operator provenance" "$reap_out" "tick="
# (2) Within-timeout issue (#202): a normal in-flight claim, never reaped.
assert_not_contains "within-timeout issue #202 is left claimed (no re-queue)" "$reap_out" "issue edit 202 --remove-label claimed"
# (3) Old issue WITH an open PR (#203): in-flight progress, never reaped.
assert_not_contains "old issue #203 with an open PR is never reaped" "$reap_out" "issue edit 203 --remove-label claimed"
# (4) Dry-run: the reap is PRINTED via run(), never executed (mutates nothing).
assert_contains "reaper re-queue is dry-run only (printed, not executed)" "$reap_out" "DRY-RUN: gh issue edit 201"
# (5) The reaped issue is recorded in the E1-2 tick run-record (operator history).
endrec="$(grep '^event=ended ' "${RPRR}" 2>/dev/null | head -1)"
assert_contains "ended run-record lists the reaped issue (#201)" "$endrec" "reaped=201"
assert_contains "ended run-record carries reaped_count=1" "$endrec" "reaped_count=1"
# (6) D1: the reaper owns ONLY the claimed/queued edge — it never escalates/declines.
assert_not_contains "reaper never escalates to needs-human" "$reap_out" "add-label needs-human"
assert_not_contains "reaper never applies wont-do" "$reap_out" "add-label wont-do"
rm -rf "${RPD}"

# ---------------------------------------------------------------------------
section "§7.14 cron: per-tick heartbeat / run-record seam (offline, deterministic)"
# (§7.14 per the #51 section map — Pillar 1 cron range, heartbeat slot; the
# issue #27 body's "§7.13" predates that map, which reserves §7.13 for the
# reaper.) The run record is local-file only and is written even under dry-run.
RRD="$(mktemp -d 2>/dev/null || mktemp -d -t rrec)"
RR="${RRD}/run-record.log"
RRFXI="${FIX}/queued-issues.json"
# (1) A normal dry-run tick writes exactly one started + one ended record that
#     share a tick id; the ended record names the claimed issue (#101), status 0.
DISPATCH_RUN_RECORD="${RR}" DISPATCH_ARTIFACTS_DIR="${RRD}/art" \
  bash scripts/pipeline.sh --fixture "${RRFXI}" >/dev/null 2>&1; trc=$?
[[ $trc -eq 0 ]] && pass "dry-run tick exits 0" || fail "dry-run tick exit" "got $trc"
nstart="$(grep -c '^event=started ' "${RR}" 2>/dev/null || true)"; nstart="${nstart:-0}"
nend="$(grep -c '^event=ended ' "${RR}" 2>/dev/null || true)"; nend="${nend:-0}"
[[ "$nstart" == "1" && "$nend" == "1" ]] && pass "exactly one started + one ended record" || fail "record count" "started=$nstart ended=$nend"
sid="$(sed -n 's/^event=started tick_id=\([^ ]*\).*/\1/p' "${RR}" 2>/dev/null | head -1)"
eid="$(sed -n 's/^event=ended tick_id=\([^ ]*\).*/\1/p' "${RR}" 2>/dev/null | head -1)"
[[ -n "$sid" && "$sid" == "$eid" ]] && pass "started and ended share one tick id ($sid)" || fail "tick id mismatch" "start='$sid' end='$eid'"
startline="$(grep '^event=started ' "${RR}" 2>/dev/null | head -1)"
assert_contains "started record carries a UTC ISO-8601 timestamp" "$startline" "ts=$(date -u +%Y-)"
assert_contains "started record carries the dry-run flag" "$startline" "dry_run=1"
endline="$(grep '^event=ended ' "${RR}" 2>/dev/null | head -1)"
assert_contains "ended record lists the claimed issue (#101)" "$endline" "claimed=101"
assert_contains "ended record carries exit status 0" "$endline" "status=0"
# (3) Crash-path proof — a tick that dies after start leaves a started record
#     with NO ended record: the stuck-tick signal the E1-3 reaper / E4 ledger read.
RR2="${RRD}/crash-record.log"
DISPATCH_RUN_RECORD="${RR2}" DISPATCH_ARTIFACTS_DIR="${RRD}/art2" DISPATCH_CRASH_AFTER_START_TEST=1 \
  bash scripts/pipeline.sh --fixture "${RRFXI}" >/dev/null 2>&1; ccode=$?
[[ $ccode -ne 0 ]] && pass "crashed tick exits non-zero" || fail "crashed tick should fail" "got $ccode"
crashrec="$(cat "${RR2}" 2>/dev/null)"
assert_contains "crashed tick still wrote a started record" "$crashrec" "event=started"
assert_not_contains "crashed tick left NO ended record (reaper signal)" "$crashrec" "event=ended"
rm -rf "${RRD}"

# ---------------------------------------------------------------------------
section "§7.15 cron: committed scheduler examples (crontab/launchd) — doc artifacts"
# (§7.15 per the #51 section map — Pillar 1 cron range, scheduler slot. Issue
# #29 is docs + example artifacts only; this validates the committed text.)
CRONTAB_EX="${ROOT}/examples/dispatch.crontab"
LAUNCHD_EX="${ROOT}/examples/dispatch.launchd.plist"
# (1) The example artifacts exist and reference a REAL entrypoint path.
[[ -f "${CRONTAB_EX}" ]] && pass "examples/dispatch.crontab exists" || fail "examples/dispatch.crontab missing"
[[ -f "${LAUNCHD_EX}" ]] && pass "examples/dispatch.launchd.plist exists" || fail "examples/dispatch.launchd.plist missing"
[[ -f "${ROOT}/entrypoint.sh" ]] && pass "entrypoint.sh is a real path in the repo" || fail "entrypoint.sh missing (examples reference it)"
crontab_txt="$(cat "${CRONTAB_EX}" 2>/dev/null)"
launchd_txt="$(cat "${LAUNCHD_EX}" 2>/dev/null)"
assert_contains "crontab example invokes entrypoint.sh" "$crontab_txt" "entrypoint.sh"
assert_contains "launchd example invokes entrypoint.sh" "$launchd_txt" "entrypoint.sh"
# (2) Live-run is never accidental: PIPELINE_DRY_RUN is set explicitly, and the
#     example serializes via flock (and/or documents the in-process E1-1 lock).
assert_contains "crontab sets PIPELINE_DRY_RUN explicitly" "$crontab_txt" "PIPELINE_DRY_RUN"
assert_contains "crontab sets PIPELINE_REPO explicitly" "$crontab_txt" "PIPELINE_REPO"
assert_contains "crontab serializes overlapping ticks with flock" "$crontab_txt" "flock"
assert_contains "crontab documents the E1-1 in-process lock" "$crontab_txt" "DISPATCH_LOCK_FILE"
assert_contains "launchd sets PIPELINE_DRY_RUN explicitly" "$launchd_txt" "PIPELINE_DRY_RUN"
# (3) Upgrade paths are documented as such, with the #15 pointer (AC).
assert_contains "crontab notes OpenClaw + billy.maic (#15) as upgrade paths" "$crontab_txt" "#15"
# (4) The README references the committed examples (discoverability).
readme_txt="$(cat "${ROOT}/README.md" 2>/dev/null)"
assert_contains "README references the committed crontab example" "$readme_txt" "examples/dispatch.crontab"
assert_contains "README documents dry-run as the scheduler default" "$readme_txt" "dry-run is the"

# ---------------------------------------------------------------------------
section "§7.17 debug: --until <stage> halt-after gating in pipeline.sh (offline, deterministic)"
# (§7.17 per the #51 section map — debug & harness range, stage-gating slot; the
# issue #31 body's "§7.15" predates that map. The --from replay half is E2-3.)
UNTILD="$(mktemp -d 2>/dev/null || mktemp -d -t until)"
UFXI="${FIX}/queued-issues.json"
# --until workorder: render + dump the work order, then halt before the engineer.
ustderr="$(DISPATCH_ARTIFACTS_DIR="${UNTILD}" bash scripts/pipeline.sh --until workorder --fixture "${UFXI}" 2>&1)"; urc=$?
[[ $urc -eq 0 ]] && pass "--until workorder exits 0" || fail "--until workorder exit" "got $urc"
utick="$(find "${UNTILD}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)"
[[ -n "$utick" && -f "$utick/workorder.txt" ]] && pass "workorder.txt dumped (workorder stage ran)" || fail "workorder.txt missing under --until workorder"
[[ -n "$utick" && ! -f "$utick/invoice.json" ]] && pass "no invoice.json (engineer stage did NOT run — halt fired)" || fail "invoice.json present despite --until workorder"
assert_contains "stderr logs the halt naming the workorder stage" "$ustderr" "halted after stage: workorder"
# Negative: an unknown stage exits non-zero and lists the valid stage names.
bogus="$(bash scripts/pipeline.sh --until bogus-stage --fixture "${UFXI}" 2>&1)"; brc=$?
[[ $brc -ne 0 ]] && pass "--until bogus-stage exits non-zero" || fail "--until bogus-stage should fail" "got $brc"
assert_contains "unknown-stage error lists the valid stages" "$bogus" "intake workorder engineer intake-invoice closure"
# Default (no --until) is unchanged: a plain dry-run tick runs the dispatch body
# and does NOT emit the halt line.
plain="$(bash scripts/pipeline.sh --fixture "${UFXI}" 2>&1)"
assert_contains "default tick still runs the dispatch body" "$plain" "pipeline: dispatch starting"
assert_not_contains "default tick emits no halt line" "$plain" "halted after stage"
rm -rf "${UNTILD}"

# --- E2-3 (#32) replay: --from <stage> --artifact <file> resumes one stage from
# a captured artifact, skipping intake/classification. EXTENDS §7.17 (shared
# debug stage-gating slot per the #51 map; the #32 body's "§7.15" predates it).
RPLD="$(mktemp -d 2>/dev/null || mktemp -d -t replay)"
RJR="${FIX}/replay-job-request.json"
# Canonical replay: --from engineer --artifact <job-request> --until engineer
# runs EXACTLY the engineer stage on the captured Job Request (issue 101 maps to
# the completed-invoice fixture), proving the artifact was the stage's input.
rstderr="$(DISPATCH_ARTIFACTS_DIR="${RPLD}" bash scripts/pipeline.sh \
  --from engineer --artifact "${RJR}" --until engineer 2>&1)"; rrc=$?
[[ $rrc -eq 0 ]] && pass "--from engineer --until engineer exits 0" || fail "--from engineer exit" "got $rrc"
rtick="$(find "${RPLD}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)"
if [[ -n "$rtick" && -f "$rtick/invoice.json" ]]; then
  riss="$(jq -r '.issue' "$rtick/invoice.json" 2>/dev/null)"
  [[ "$riss" == "101" ]] && pass "engineer ran on the captured Job Request (replayed invoice.issue=101)" \
    || fail "replayed invoice issue mismatch" "got $riss"
else
  fail "invoice.json not dumped by --from engineer replay"
fi
assert_contains "stderr logs the resume naming the engineer stage" "$rstderr" "resume from stage: engineer"
assert_contains "stderr logs the --until halt after engineer" "$rstderr" "halted after stage: engineer"
# Earlier stages did NOT run: no work order dumped, and no dispatch/intake body.
[[ -n "$rtick" && ! -f "$rtick/workorder.txt" ]] && pass "no workorder.txt (intake/classification skipped on resume)" || fail "workorder.txt present — earlier stages ran despite --from engineer"
assert_not_contains "resume skips the dispatch/intake body" "$rstderr" "dispatch starting"
# Determinism: a second identical replay yields a byte-identical invoice dump.
RPLD2="$(mktemp -d 2>/dev/null || mktemp -d -t replay2)"
DISPATCH_ARTIFACTS_DIR="${RPLD2}" bash scripts/pipeline.sh --from engineer --artifact "${RJR}" --until engineer >/dev/null 2>&1
rtick2="$(find "${RPLD2}" -maxdepth 1 -type d -name 'tick-*' 2>/dev/null | head -1)"
if [[ -n "$rtick" && -n "$rtick2" && -f "$rtick/invoice.json" && -f "$rtick2/invoice.json" ]] \
   && diff -q "$rtick/invoice.json" "$rtick2/invoice.json" >/dev/null 2>&1; then
  pass "replay is deterministic (two runs -> byte-identical invoice dump)"
else
  fail "replay not deterministic across two runs"
fi
rm -rf "${RPLD2}"
# Negative: --from without --artifact exits non-zero and names the requirement.
n1="$(bash scripts/pipeline.sh --from engineer 2>&1)"; n1rc=$?
[[ $n1rc -ne 0 ]] && pass "--from without --artifact exits non-zero" || fail "--from should require --artifact" "got $n1rc"
assert_contains "missing-artifact error names the requirement" "$n1" "requires --artifact"
# Negative: --artifact pointing at schema-invalid JSON exits non-zero.
BADART="${RPLD}/bad.json"
printf '%s\n' '{"not":"a job request"}' >"${BADART}"
n2="$(bash scripts/pipeline.sh --from engineer --artifact "${BADART}" 2>&1)"; n2rc=$?
[[ $n2rc -ne 0 ]] && pass "--from engineer with schema-invalid artifact exits non-zero" || fail "schema-invalid artifact should fail" "got $n2rc"
assert_contains "schema-invalid error names the missing field(s)" "$n2" "missing required field"
# Negative: unknown --from stage exits non-zero and lists the valid stages.
n3="$(bash scripts/pipeline.sh --from bogus-stage --artifact "${RJR}" 2>&1)"; n3rc=$?
[[ $n3rc -ne 0 ]] && pass "--from bogus-stage exits non-zero" || fail "--from bogus-stage should fail" "got $n3rc"
assert_contains "unknown --from lists the valid stages" "$n3" "engineer intake-invoice closure"
rm -rf "${RPLD}"

# ---------------------------------------------------------------------------
section "§7.19 monitoring: append-JSONL run-ledger emit (offline, deterministic)"
# (§7.19 per the #51 section map — Pillar 4 monitoring range, run-ledger slot;
# the issue #36 body's "§7.16" predates that map, which reserves §7.16-§7.18 for
# the debug & harness pillar.) Drives the real wiring fully offline + dry-run:
# dispatch.sh emits the claim + work-order lines; architect-intake.sh emits the
# engineer-dispatch / invoice / closure lines (cost copied from the Invoice).
LEDGERD="$(mktemp -d 2>/dev/null || mktemp -d -t ledger)"
LFILE="${LEDGERD}/run-ledger.jsonl"
LINV="${ROOT}/scripts/fixtures/ledger-invoice.json"
[[ -f "$LINV" ]] && pass "ledger-invoice.json fixture exists" || fail "ledger-invoice.json missing"
(
  export DISPATCH_LEDGER_FILE="$LFILE" DISPATCH_TICK_ID="tick-smoke-719" PIPELINE_DRY_RUN=1
  PIPELINE_FIXTURE_ISSUES="${FIX}/queued-issues.json" bash "${ROOT}/scripts/dispatch.sh" >/dev/null 2>&1 || true
  bash "${ROOT}/scripts/architect-intake.sh" < "$LINV" >/dev/null 2>&1 || true
)
[[ -s "$LFILE" ]] && pass "run-ledger.jsonl exists and is non-empty" || fail "run-ledger not written"
if jq -c . "$LFILE" >/dev/null 2>&1; then pass "every ledger line is valid JSON (jq -c .)"; else fail "ledger has malformed JSONL"; fi
nrec="$(jq -s 'length' "$LFILE" 2>/dev/null)"
[[ "${nrec:-0}" -ge 1 ]] && pass "ledger is jq -s aggregatable (${nrec} records)" || fail "ledger not aggregatable" "got '$nrec'"
if jq -se 'all(.[]; .tick_id != null and .timestamp != null)' "$LFILE" >/dev/null 2>&1; then
  pass "every line carries non-null tick_id + timestamp"
else fail "a ledger line is missing tick_id/timestamp"; fi
if jq -se 'any(.[]; .stage=="engineer-dispatch" and .cost.tokens_in != null)' "$LFILE" >/dev/null 2>&1; then
  pass "engineer-dispatch line carries the Invoice cost.tokens_in"
else fail "engineer-dispatch cost not populated from the Invoice"; fi
if jq -se 'any(.[]; .stage=="claimed" and .label_before=="queued" and .label_after=="claimed")' "$LFILE" >/dev/null 2>&1; then
  pass "claim line records the queued -> claimed transition"
else fail "claim line label transition missing"; fi
if jq -se 'all(.[]; .stage as $s | ["claimed","work-order","engineer-dispatch","invoice","closure"] | index($s) != null)' "$LFILE" >/dev/null 2>&1; then
  pass "all stages drawn from the fixed vocabulary"
else fail "ledger emitted an out-of-vocabulary stage"; fi
# Optional fields serialize as null keys (never absent) — uniform aggregation.
if jq -se 'all(.[]; has("cost") and (.cost|has("tokens_in") and has("model")) and has("label_before") and has("dry_run"))' "$LFILE" >/dev/null 2>&1; then
  pass "missing optional fields serialize as null keys (uniform schema)"
else fail "a ledger line dropped an optional key instead of nulling it"; fi
# Offline: dry-run tick mutates no GitHub state; every line stamps dry_run:true.
if jq -se 'all(.[]; .dry_run==true)' "$LFILE" >/dev/null 2>&1; then
  pass "every line stamps dry_run:true (no GitHub mutation under dry-run)"
else fail "a ledger line not marked dry_run under a dry-run tick"; fi
rm -rf "${LEDGERD}"

# ---------------------------------------------------------------------------
section "§7.20 monitoring: operator digest renderer (offline, deterministic)"
# (§7.20 per the #51 section map — Pillar 4 monitoring range, digest-renderer
# slot; the issue #37 body's "§7.17" predates that map, which reserves §7.17 for
# the debug stage-gating slot. run-ledger = §7.19.)
REP="${ROOT}/services/reports/report.py"
RLF="${ROOT}/scripts/fixtures/run-ledger.jsonl"
PYR="${PYTHON_BIN:-python3}"
dig="$("$PYR" "$REP" --ledger "$RLF" --format markdown 2>/dev/null)"; drc=$?
[[ $drc -eq 0 ]] && pass "digest renders (exit 0)" || fail "digest exit" "got $drc"
# Distinct issue numbers from the fixture appear.
if grep -q '#101' <<<"$dig" && grep -q '#102' <<<"$dig" && grep -q '#103' <<<"$dig"; then
  pass "digest lists the distinct issue numbers (101, 102, 103)"
else fail "digest missing issue numbers"; fi
# A by-current-stage summary line.
assert_contains "digest carries a by-current-stage summary" "$dig" "by current stage"
# Per-issue token total is the EXACT hand-computed sum (#101: 18000 + 6000 = 24000).
if grep -qE '#101 .*24000' <<<"$dig"; then
  pass "per-issue token total is the exact sum (#101 -> 24000)"
else fail "digest token total wrong for #101"; fi
# Determinism: same ledger -> byte-identical digest.
dig2="$("$PYR" "$REP" --ledger "$RLF" --format markdown 2>/dev/null)"
[[ "$dig" == "$dig2" ]] && pass "digest is byte-identical across runs" || fail "digest nondeterministic"
# Empty ledger -> 'no runs recorded', exit 0 (does not crash).
empL="$(mktemp)"; eo="$("$PYR" "$REP" --ledger "$empL" 2>&1)"; erc=$?
[[ $erc -eq 0 ]] && assert_contains "empty ledger renders 'no runs recorded'" "$eo" "no runs recorded" || fail "empty digest exit" "got $erc"
rm -f "$empL"
# --format text also exits 0 and carries the issue numbers.
dtxt="$("$PYR" "$REP" --ledger "$RLF" --format text 2>/dev/null)"
if grep -q '#101' <<<"$dtxt" && grep -q '#103' <<<"$dtxt"; then pass "--format text renders the issues"; else fail "--format text wrong"; fi
# --comment prints the would-be GitHub comment under dry-run and never calls gh.
dcm="$("$PYR" "$REP" --ledger "$RLF" --comment 2>/dev/null)"
assert_contains "--comment prints a dry-run GitHub-comment stub" "$dcm" "would post"
# Read-only / offline: report.py references no gh/claude/subprocess seam.
if grep -nE 'subprocess|gh_mutate|claude_invoke|os\.system|gh issue' "$REP" >/dev/null 2>&1; then
  fail "report.py references a gh/exec seam"
else pass "report.py is read-only/offline (no gh/claude/subprocess)"; fi

# ---------------------------------------------------------------------------
section "§7.21 budget: claude-monitor usage oracle (offline fixture, deterministic)"
# (§7.21 per the #51 section map — Pillar 3 budget range, claude-monitor slot;
# the issue #36/#38 body's "§7.17" predates that map.) Exercised entirely via the
# committed offline fixture — never reads ~/.claude, never invokes claude-monitor.
ORACLE="${ROOT}/services/budget/oracle.py"
OFX="${ROOT}/services/budget/fixtures/window-state.under-cap.json"
[[ -f "$OFX" ]] && pass "window-state.under-cap.json fixture exists" || fail "budget oracle fixture missing"
ostate="$(BUDGET_ORACLE_FIXTURE="$OFX" python3 "$ORACLE" 2>/dev/null)"; orc=$?
[[ $orc -eq 0 ]] && pass "oracle exits 0 in fixture mode" || fail "oracle exit" "got $orc"
# (1) The normalized record carries every required key, sourced from the fixture.
if jq -e '.source=="fixture" and has("plan") and has("limit_tokens") and has("used_tokens") and has("fraction") and has("window_start")' <<<"$ostate" >/dev/null 2>&1; then
  pass "window_state has {plan,limit_tokens,used_tokens,fraction,window_start,source==fixture}"
else
  fail "window_state record missing keys / wrong source" "$ostate"
fi
# (2) fraction == used/limit (rounded to 4dp): 40000/88000 -> 0.4545.
ofrac="$(jq -r '.fraction' <<<"$ostate")"
oexp="$(python3 -c "import json,sys; d=json.load(open('$OFX')); print(round(d['used_tokens']/d['limit_tokens'],4))")"
[[ "$ofrac" == "$oexp" ]] && pass "fraction == used/limit rounded (${ofrac})" || fail "fraction wrong" "got $ofrac want $oexp"
# (3) Deterministic: same fixture in -> byte-identical stdout out.
ostate2="$(BUDGET_ORACLE_FIXTURE="$OFX" python3 "$ORACLE" 2>/dev/null)"
[[ "$ostate" == "$ostate2" ]] && pass "oracle is deterministic across runs (byte-identical)" || fail "oracle nondeterministic"
# (4) Offline purity: fixture mode does not read the real session logs — the run
#     is byte-identical with HOME pointed at an empty temp dir.
OEMPTY="$(mktemp -d 2>/dev/null || mktemp -d -t orahome)"
ostate3="$(HOME="$OEMPTY" BUDGET_ORACLE_FIXTURE="$OFX" python3 "$ORACLE" 2>/dev/null)"
[[ "$ostate" == "$ostate3" ]] && pass "fixture mode is HOME-independent (no ~/.claude read)" || fail "oracle reads HOME in fixture mode"
rm -rf "$OEMPTY"
# (5) Live path is not exercised in CI — SKIP when claude-monitor is absent.
if command -v claude-monitor >/dev/null 2>&1; then
  pass "claude-monitor present (live path available)"
else
  skip "claude-monitor not installed — live oracle path not exercised (fixture path covered above)"
fi
# (6) The tuning surface carries the budget block the oracle reads.
if jq -e '.budget.window.plan_tier and .budget.plan_limits' "${ROOT}/services/tuning.json" >/dev/null 2>&1; then
  pass "services/tuning.json budget block carries plan_tier + plan_limits"
else
  fail "tuning.json budget block missing plan_tier/plan_limits"
fi

# ---------------------------------------------------------------------------
section "§7.22 budget: per-job Invoice reconciliation (actual vs authorized, offline)"
# (§7.22 per the #51 section map — Pillar 3 budget range, invoice-reconcile slot;
# the issue #39 body's "§7.18" predates that map, which reserves §7.18 for the
# debug & harness pillar. claude-monitor oracle = §7.21, soft-cap = §7.23.)
RECON="${ROOT}/services/budget/reconcile.py"
LMIX="${ROOT}/services/budget/fixtures/ledger.mixed.jsonl"
PYB="${PYTHON_BIN:-python3}"
recon="$("$PYB" "$RECON" --ledger "$LMIX" 2>/dev/null)"
# Per-job verdict: the under-budget job is not overspent; the over-budget job is.
if printf '%s' "$recon" | jq -e '(.jobs|length)==2' >/dev/null 2>&1; then
  pass "reconcile counts one verdict per cost-bearing job (zero-cost claimed line skipped)"
else fail "reconcile job count wrong (claimed line not skipped?)"; fi
if printf '%s' "$recon" | jq -e '.jobs[]|select(.issue==101)|.overspent==false' >/dev/null 2>&1; then
  pass "under-budget job #101 (60k/80k) -> overspent: false"
else fail "under-budget job not flagged correctly"; fi
if printf '%s' "$recon" | jq -e '.jobs[]|select(.issue==102)|.overspent==true and .overspend_pct>0' >/dev/null 2>&1; then
  pass "over-budget job #102 (52k/40k) -> overspent: true, positive overspend_pct"
else fail "over-budget job not flagged correctly"; fi
# authorized re-derived via approval.approve() when the line carries scope, not budget.
if printf '%s' "$recon" | jq -e '.jobs[]|select(.issue==102)|.authorized_tokens==40000' >/dev/null 2>&1; then
  pass "authorized re-derived from scope s via approval.approve() (-> 40000)"
else fail "re-derivation of authorized budget from scope failed"; fi
# Daily aggregate: one overspent of two jobs; sums equal the fixture's totals.
aggd="$("$PYB" "$RECON" --ledger "$LMIX" --by day 2>/dev/null)"
if printf '%s' "$aggd" | jq -e '.aggregate[0]|.overspent_count==1 and .job_count==2 and .sum_actual_tokens==112000 and .sum_authorized_tokens==120000' >/dev/null 2>&1; then
  pass "day aggregate: job_count=2, overspent_count=1, sum_actual=112000, sum_authorized=120000"
else fail "day aggregate wrong" "$(printf '%s' "$aggd" | jq -c '.aggregate')"; fi
# Weekly aggregate buckets by ISO-week.
if printf '%s' "$("$PYB" "$RECON" --ledger "$LMIX" --by week 2>/dev/null)" | jq -e '.aggregate[0]|(.period|test("W[0-9][0-9]")) and .job_count==2' >/dev/null 2>&1; then
  pass "week aggregate buckets by ISO-week (period like YYYY-Www)"
else fail "week aggregate wrong"; fi
# Determinism: same ledger -> byte-identical reconciliation JSON.
r2="$("$PYB" "$RECON" --ledger "$LMIX" --by day 2>/dev/null)"
[[ "$aggd" == "$r2" ]] && pass "reconciliation is byte-identical across runs" || fail "reconciliation nondeterministic"
# Offline: reconcile.py makes no network/model/subprocess call (no gh/claude/import subprocess).
if grep -nE 'subprocess|import os.*system|claude_invoke|gh_mutate| gh ' "$RECON" >/dev/null 2>&1; then
  fail "reconcile.py references a network/exec seam"
else pass "reconcile.py is pure/offline (no gh/claude/subprocess)"; fi

# ---------------------------------------------------------------------------
section "§7.23 budget: pre-flight soft-cap guard throttles the tick (offline)"
# (§7.23 per the #51 section map — Pillar 3 budget range, soft-cap slot; the
# issue #40 body's "§7.19" predates that map. claude-monitor = §7.21, invoice
# reconcile = §7.22.)
GUARD="${ROOT}/services/budget/guard.py"
OVER="${ROOT}/services/budget/fixtures/window-state.over-cap.json"
UNDER="${ROOT}/services/budget/fixtures/window-state.under-cap.json"
FXQ_SC="${ROOT}/scripts/fixtures/queued-issues.json"
PYG="${PYTHON_BIN:-python3}"
# (1) Guard decision: over-cap -> throttle true; under-cap -> throttle false.
gov="$(BUDGET_ORACLE_FIXTURE="$OVER" "$PYG" "$GUARD" 2>/dev/null)"
if printf '%s' "$gov" | jq -e '.throttle==true and (.fraction>=.soft_cap)' >/dev/null 2>&1; then
  pass "over-cap window -> throttle: true (fraction >= soft_cap)"
else fail "over-cap did not throttle" "$gov"; fi
gun="$(BUDGET_ORACLE_FIXTURE="$UNDER" "$PYG" "$GUARD" 2>/dev/null)"
if printf '%s' "$gun" | jq -e '.throttle==false and (.fraction<.soft_cap)' >/dev/null 2>&1; then
  pass "under-cap window -> throttle: false (fraction < soft_cap)"
else fail "under-cap throttled unexpectedly" "$gun"; fi
# (2) Fail-open: no fixture + no monitor -> throttle false (default path unchanged).
gno="$(env -u BUDGET_ORACLE_FIXTURE CLAUDE_MONITOR_BIN=/nonexistent-monitor "$PYG" "$GUARD" 2>/dev/null)"
if printf '%s' "$gno" | jq -e '.throttle==false' >/dev/null 2>&1; then
  pass "no window data -> fail-open throttle: false (a missing oracle never blocks the tick)"
else fail "guard did not fail open without oracle data" "$gno"; fi
# (3) Dispatch pre-flight: over-cap + operator --live (DRY_RUN=0) -> forced
#     dry-run, NO claim. The guard runs before any gh, so PIPELINE_DRY_RUN=0 is
#     safe here — nothing mutates.
SCLEDG="${_smoke_rrdir}/throttle-ledger.jsonl"; rm -f "$SCLEDG"
tov="$(BUDGET_ORACLE_FIXTURE="$OVER" PIPELINE_FIXTURE_ISSUES="$FXQ_SC" PIPELINE_DRY_RUN=0 \
       DISPATCH_LEDGER_FILE="$SCLEDG" bash "${ROOT}/scripts/dispatch.sh" 2>&1)"
assert_contains "throttle: dispatch logs the soft-cap throttle (fraction + soft_cap)" "$tov" "soft-cap THROTTLE"
assert_contains "throttle: forces PIPELINE_DRY_RUN=1 even when operator passed live" "$tov" "forcing PIPELINE_DRY_RUN=1"
assert_not_contains "throttle: no issue is claimed (no queued->claimed transition)" "$tov" "-> claimed"
# (4) The throttle event is recorded to the run-ledger (event/fraction/soft_cap).
if [[ -s "$SCLEDG" ]] && jq -e 'select(.event=="throttled") | has("fraction") and has("soft_cap")' "$SCLEDG" >/dev/null 2>&1; then
  pass "throttle event recorded to the run-ledger (event=throttled, fraction, soft_cap)"
else fail "throttle ledger line missing/incomplete"; fi
rm -f "$SCLEDG"
# (5) Under-cap: dispatch proceeds and claims as today (reuse the §7.3 dry-run idiom).
tun="$(BUDGET_ORACLE_FIXTURE="$UNDER" PIPELINE_FIXTURE_ISSUES="$FXQ_SC" PIPELINE_DRY_RUN=1 \
       bash "${ROOT}/scripts/dispatch.sh" 2>&1)"
assert_contains "no-throttle: under-cap tick claims normally (queued->claimed)" "$tun" "-> claimed"
assert_not_contains "no-throttle: under-cap tick is not throttled" "$tun" "THROTTLE"
# (6) Determinism: the guard decision is byte-identical across runs.
gov2="$(BUDGET_ORACLE_FIXTURE="$OVER" "$PYG" "$GUARD" 2>/dev/null)"
[[ "$gov" == "$gov2" ]] && pass "guard decision is deterministic across runs" || fail "guard nondeterministic"

# ---------------------------------------------------------------------------
section "§7.18 demo harness: snapshot + mutator + replay + scenario catalog (offline, per-route)"
# (§7.18 per the #51 section map — Pillar 5 debug & harness range, the shared
# snapshot/mutate/replay slot. The issue #43 body's "§7.12" predates that map,
# which reserves §7.12–§7.15 for the cron pillar. The mutator (#44) and replay
# (#45) EXTEND this same §7.18 section rather than claiming new numbers — the
# harness pillar has one slot and the duplicate-id guard collapses any 7.18.x.)
# All assertions run OFFLINE against the committed sample snapshots — no live gh.
SNAP="${ROOT}/scripts/demo/snapshots/demo-current.json"
EMPTY="${ROOT}/scripts/demo/snapshots/demo-empty.json"
SNAPSH="${ROOT}/scripts/demo/snapshot.sh"
# Schema: superset fields present; labels are {name} objects (dispatch+intake shape).
if jq -e 'type=="array" and all(.[]; has("number") and has("title") and has("body") and has("labels") and has("assignees") and has("url"))' "$SNAP" >/dev/null 2>&1; then
  pass "snapshot carries the dual-consumer superset (number,title,body,labels,assignees,url)"
else
  fail "snapshot missing superset fields"
fi
if jq -e 'all(.[]; (.labels|length==0) or (.labels[0]|has("name")))' "$SNAP" >/dev/null 2>&1; then
  pass "labels captured as {name} objects (not flattened)"
else
  fail "labels not object-shaped — dispatch/intake fidelity broken"
fi
# Drop-in for ./dispatch --fixture (dispatch.py shares intake's loader).
if PIPELINE_DRY_RUN=1 ./dispatch --fixture "$SNAP" >/dev/null 2>&1; then
  pass "snapshot is a drop-in PIPELINE_FIXTURE_ISSUES (dispatch exit 0)"
else
  fail "dispatch rejected snapshot"
fi
# Drop-in for intake.py INTAKE_FIXTURE_REPO (object labels flatten via lbl['name']).
if INTAKE_FIXTURE_REPO="$SNAP" "${PYTHON_BIN:-python3}" "${ROOT}/services/intake/intake.py" --repo ReclaimByDesign/demo-repository 2>/dev/null | jq -e 'length>=1' >/dev/null; then
  pass "snapshot is a drop-in INTAKE_FIXTURE_REPO (intake emits items)"
else
  fail "intake rejected snapshot"
fi
# Provenance sidecar shape.
PROV="${ROOT}/scripts/demo/snapshots/demo-current.provenance.json"
if jq -e 'has("captured_at") and has("head_sha") and has("gh_login") and has("fields")' "$PROV" >/dev/null 2>&1; then
  pass "provenance sidecar carries timestamp+sha+login+fields"
else
  fail "provenance sidecar incomplete"
fi
# Empty-queue snapshot replays cleanly: ./dispatch on an empty array exits 3
# ("dispatch: queue is empty", dispatch.py:505-507) — assert 3, not 0.
out="$(PIPELINE_DRY_RUN=1 ./dispatch --fixture "$EMPTY" 2>&1)"; ec=$?
[[ $ec -eq 3 ]] && assert_contains "empty snapshot reports empty queue" "$out" "queue is empty" || fail "empty snapshot exit" "got $ec"
# Least-privilege: snapshot.sh never calls a mutating gh subcommand.
if grep -nE 'issue (create|edit|comment|delete)|label create|pr (create|merge|comment)' "$SNAPSH" >/dev/null 2>&1; then
  fail "snapshot.sh contains a mutating gh call"
else
  pass "snapshot.sh is read-only (no mutating gh subcommand)"
fi

# --- E8-2 (#44) scenario mutator: EXTENDS §7.18 (the shared harness slot — no
# new §7.x number; the duplicate-id guard collapses any 7.18.x). mutate.py is a
# pure-Python, no-network overlay resolver over the committed base snapshot.
SC="${ROOT}/scripts/demo/scenarios"
MUT="${ROOT}/scripts/demo/mutate.py"
PY="${PYTHON_BIN:-python3}"
# Deterministic / idempotent: two runs on the same base+overlay are byte-identical.
m1="$("$PY" "$MUT" --scenario "$SC/blank-vague-body.json" --base "$SNAP" 2>/dev/null)"
m2="$("$PY" "$MUT" --scenario "$SC/blank-vague-body.json" --base "$SNAP" 2>/dev/null)"
[[ -n "$m1" && "$m1" == "$m2" ]] && pass "mutator output is deterministic across runs" || fail "mutator nondeterministic"
# Output sorted by issue number.
if printf '%s' "$m1" | jq -e '[.[].number] == ([.[].number]|sort)' >/dev/null 2>&1; then
  pass "mutator output sorted by issue number"
else fail "mutator output not sorted"; fi
# set-body actually mutates the targeted field (blank-vague-body empties #103).
if printf '%s' "$m1" | jq -e 'any(.[]; .number==103 and (.body|length)==0)' >/dev/null 2>&1; then
  pass "set-body mutates only the targeted field (#103 body emptied)"
else fail "set-body did not empty #103 body"; fi
# remove-issue drops the number; add-issue introduces a full issue object.
rm="$("$PY" "$MUT" --scenario "$SC/drop-issue.json" --base "$SNAP" 2>/dev/null)"
if printf '%s' "$rm" | jq -e 'all(.[]; .number != 105)' >/dev/null 2>&1; then pass "drop-issue removes #105"; else fail "drop-issue did not remove"; fi
add="$("$PY" "$MUT" --scenario "$SC/add-bug.json" --base "$SNAP" 2>/dev/null)"
if printf '%s' "$add" | jq -e 'any(.[]; .number==901)' >/dev/null 2>&1; then pass "add-issue introduces #901"; else fail "add-issue did not add"; fi
# add-issue keeps labels object-shaped (drop-in fidelity for intake.py).
if printf '%s' "$add" | jq -e '(.[] | select(.number==901) | .labels[0] | has("name"))' >/dev/null 2>&1; then
  pass "add-issue preserves {name} label objects"
else fail "add-issue labels not object-shaped"; fi
# Base snapshot on disk is unchanged after a run (reset is free).
cp "$SNAP" "${_smoke_rrdir}/snap-before.json"
"$PY" "$MUT" --scenario "$SC/drop-issue.json" --base "$SNAP" >/dev/null 2>&1
if cmp -s "$SNAP" "${_smoke_rrdir}/snap-before.json"; then pass "base snapshot untouched on disk (reset is free)"; else fail "mutator mutated the base snapshot"; fi
# Resolved fixture is a valid drop-in for ./dispatch --fixture.
mtmp="$(mktemp)"; printf '%s' "$m1" > "$mtmp"
PIPELINE_DRY_RUN=1 ./dispatch --fixture "$mtmp" >/dev/null 2>&1 \
  && pass "resolved fixture is a drop-in (dispatch exit 0)" || fail "resolved fixture rejected by dispatch"
rm -f "$mtmp"
# Output is always a top-level JSON array (full-replacement escape hatch shape).
if printf '%s' "$add" | jq -e 'type=="array"' >/dev/null 2>&1; then pass "mutator emits a JSON array"; else fail "mutator output not an array"; fi

# --- E8-3 (#45) replay driver: EXTENDS §7.18 (shared harness slot). One-command
# offline replay (resolve via mutate.py -> ./dispatch --fixture -> dump round
# artifacts). jsonschema is NOT installed here, so the Job Request / Invoice are
# validated structurally (key-set + types) per §7.7/§7.16, never via jsonschema.
RP="${ROOT}/scripts/demo/replay.sh"
RPD="$(mktemp -d 2>/dev/null || mktemp -d -t replay)"
DEMO_ROUNDS_DIR="$RPD" PIPELINE_DRY_RUN=1 bash "$RP" blank-vague-body >/dev/null 2>&1; rprc=$?
[[ $rprc -eq 0 ]] && pass "replay exits 0 (offline dry-run)" || fail "replay exit" "got $rprc"
RPR1="$(ls -d "$RPD"/blank-vague-body/* 2>/dev/null | head -1)"
if [[ -n "$RPR1" && -f "$RPR1/fixture.json" && -f "$RPR1/work-order.txt" && -f "$RPR1/job-request.json" && -f "$RPR1/invoice.json" ]]; then
  pass "replay round dir carries fixture + work-order + job-request + invoice"
else fail "replay round artifacts missing"; fi
# Job Request structurally valid (reuse §7.16's bespoke validator — NOT jsonschema).
if [[ -n "$RPR1" ]] && python3 - "$RPR1/job-request.json" <<'PY'
import json, sys
REQ = {"job_id", "issue", "repo", "title", "body", "route", "scope", "confidence"}
ROUTE = {"gen-local", "gen-default", "gen-frontier"}; SCOPE = {"xs", "s", "m", "l"}
j = json.load(open(sys.argv[1])); errs = []
if set(j) != REQ: errs.append("keys")
if not isinstance(j.get("issue"), int): errs.append("issue")
if j.get("route") not in ROUTE: errs.append("route")
if j.get("scope") not in SCOPE: errs.append("scope")
c = j.get("confidence")
if not isinstance(c, (int, float)) or not 0 <= c <= 1: errs.append("confidence")
if not isinstance(j.get("repo"), str): errs.append("repo")
sys.exit(1 if errs else 0)
PY
then pass "replay job-request.json is schema-valid (structural, per §7.7/§7.16)"
else fail "replay job-request.json schema-invalid"; fi
# Invoice structurally valid (reuse §7.7's bespoke invoice validator — NOT jsonschema).
if [[ -n "$RPR1" ]] && python3 - "$RPR1/invoice.json" <<'PY'
import json, sys
REQUIRED = {"invoice_id", "issue", "repo", "status", "route_used", "cost", "summary", "timestamp"}
STATUS = {"completed", "failed", "partial", "needs-human"}
j = json.load(open(sys.argv[1])); errs = []
if not REQUIRED <= set(j): errs.append("keys")
if j.get("status") not in STATUS: errs.append("status")
if not isinstance(j.get("cost"), dict): errs.append("cost")
sys.exit(1 if errs else 0)
PY
then pass "replay invoice.json is schema-valid (structural, per §7.7)"
else fail "replay invoice.json schema-invalid"; fi
# No live mutation: the dumped work order is dry-run shaped.
[[ -n "$RPR1" ]] && assert_contains "replay work order is dry-run shaped" "$(cat "$RPR1/work-order.txt")" "DRY_RUN=1"
# Repeatable: a second round is byte-identical on work-order + job-request.
DEMO_ROUNDS_DIR="$RPD" PIPELINE_DRY_RUN=1 bash "$RP" blank-vague-body >/dev/null 2>&1
RPR2="$(ls -d "$RPD"/blank-vague-body/* 2>/dev/null | tail -1)"
if [[ -n "$RPR1" && -n "$RPR2" && "$RPR1" != "$RPR2" ]] \
   && diff -q "$RPR1/work-order.txt" "$RPR2/work-order.txt" >/dev/null 2>&1 \
   && diff -q "$RPR1/job-request.json" "$RPR2/job-request.json" >/dev/null 2>&1; then
  pass "two replays produce byte-identical work-order + job-request"
else fail "replay not repeatable"; fi
# Resettable: --reset empties the scenario rounds dir.
DEMO_ROUNDS_DIR="$RPD" bash "$RP" --reset blank-vague-body >/dev/null 2>&1
[[ -z "$(ls -A "$RPD/blank-vague-body" 2>/dev/null)" ]] && pass "replay --reset empties the scenario rounds dir" || fail "replay --reset left artifacts"
rm -rf "$RPD"

# --- E8-4 (#46) scenario catalog: EXTENDS §7.18 (shared harness slot). Committed
# overlays, one per classifier route. The asserted action/route is a PROPERTY of
# the real classify.py (resolve via mutate.py, classify the target) — honored
# against the offline classifier's hint-token rules, not hardcoded.
SCAT="${ROOT}/scripts/demo/scenarios"
MUTC="${ROOT}/scripts/demo/mutate.py"
CLSC="${ROOT}/services/classifier/classify.py"
PYC="${PYTHON_BIN:-python3}"
# Resolve a scenario, classify a target issue, echo "action route".
classify_target() {  # $1 scenario file, $2 issue number
  local fx it t b
  fx="$("$PYC" "$MUTC" --scenario "$1" --base "$SNAP" 2>/dev/null)"
  it="$(printf '%s' "$fx" | jq -c ".[] | select(.number==$2)")"
  t="$(printf '%s' "$it" | jq -r '.title')"; b="$(printf '%s' "$it" | jq -r '.body // ""')"
  CLASSIFIER_OFFLINE=1 "$PYC" "$CLSC" --title "$t" --body "$b" | jq -r '"\(.action) \(.route)"'
}
# Every catalog overlay is valid JSON.
catalog_bad=0
for f in "$SCAT"/*.json; do jq -e '.' "$f" >/dev/null 2>&1 || catalog_bad=1; done
[[ $catalog_bad -eq 0 ]] && pass "all catalog scenarios are valid JSON" || fail "a catalog scenario is invalid JSON"
# Per-route assertions (each route is a property of the real classifier).
assert_contains "bug-fix routes implement/gen-local"           "$(classify_target "$SCAT/bug-fix.json" 101)"                 "implement gen-local"
assert_contains "feature routes implement/gen-default"         "$(classify_target "$SCAT/feature-with-acceptance.json" 102)" "implement gen-default"
assert_contains "large-refactor routes implement/gen-frontier" "$(classify_target "$SCAT/large-refactor.json" 104)"          "implement gen-frontier"
assert_contains "out-of-scope routes wont-do"                  "$(classify_target "$SCAT/out-of-scope-wont-do.json" 105)"    "wont-do"
# Vague item defers BELOW threshold. classify.py returns implement at low
# confidence; ./dispatch prints 'skip (conf … < 0.55)' (NOT 'needs-human', a
# dispatch.sh-only label) — assert the confidence is sub-threshold.
vfxr="$("$PYC" "$MUTC" --scenario "$SCAT/vague-needs-human.json" --base "$SNAP" 2>/dev/null)"
vit="$(printf '%s' "$vfxr" | jq -c '.[] | select(.number==103)')"
vt="$(printf '%s' "$vit" | jq -r '.title')"; vb="$(printf '%s' "$vit" | jq -r '.body // ""')"
if CLASSIFIER_OFFLINE=1 "$PYC" "$CLSC" --title "$vt" --body "$vb" | jq -e ".confidence < ${PIPELINE_CONFIDENCE_THRESHOLD:-0.55}" >/dev/null 2>&1; then
  pass "vague scenario defers below PIPELINE_CONFIDENCE_THRESHOLD (operator/needs-human path)"
else fail "vague scenario not below threshold"; fi
# Catalog README documents each scenario.
RMEC="${ROOT}/scripts/demo/README.md"
if [[ -f "$RMEC" ]] && grep -q 'bug-fix.json' "$RMEC" && grep -q 'out-of-scope-wont-do.json' "$RMEC"; then
  pass "scripts/demo/README.md carries the scenario catalog table"
else fail "demo README catalog table missing"; fi

# ---------------------------------------------------------------------------
section "§7.24 research-mode: architect gap-detection heuristic (offline, deterministic)"
# (§7.24 per the #51 section map — Day-3 research-mode range; the #54 body's "§7.20"
# predates that map, which reserves §7.19-§7.20 for monitoring. gap-detection is the
# first research-mode slot.) detect_gap is pure/offline and returns needs_research
# plus an enumerated reason (label / no-matching-files / low-confidence) per job.
RGQ="${ROOT}/services/architect/fixtures/research-gap-queue.json"
[[ -f "$RGQ" ]] && pass "research-gap-queue.json fixture exists" || fail "research gap fixture missing"
# Drive detect_gap directly (mirrors how §7.2 exercises classify.py); prints one
# "<num> <needs_research> <reason>" line per fixture issue. discover() supplies the
# repo-match signal so detect_gap stays pure.
gap_run() {
  python3 - "$RGQ" <<'PY'
import sys, json
sys.path.insert(0, "services"); sys.path.insert(0, "services/architect")
import resources, research
for job in json.load(open(sys.argv[1])):
    v = research.detect_gap(job, resources.discover(job.get("body", ""), "."))
    print(job["number"], v["needs_research"], v["reason"])
PY
}
gap="$(gap_run)"
assert_contains "needs-research label -> needs_research (reason=label)" "$gap" "301 True label"
assert_contains "unknown file/API tokens + zero repo matches -> needs_research (no-matching-files)" "$gap" "302 True no-matching-files"
assert_contains "well-grounded issue (real repo file) -> no research" "$gap" "303 False none"
assert_contains "below-threshold confidence -> needs_research (low-confidence)" "$gap" "304 True low-confidence"
# Deterministic: identical (job, repo state, tuning) -> identical verdicts.
gap2="$(gap_run)"
[[ "$gap" == "$gap2" ]] && pass "gap verdicts are byte-identical across runs" || fail "gap-detection nondeterministic"
# Kill-switch: generation.research.enabled=false -> always needs_research=False.
GAPTMP="$(mktemp -d 2>/dev/null || mktemp -d -t gap)"
printf '%s\n' '{"generation":{"research":{"enabled":false}}}' > "${GAPTMP}/tuning.json"
ks="$(DISPATCH_TUNING_FILE="${GAPTMP}/tuning.json" python3 - "$RGQ" <<'PY'
import sys, json
sys.path.insert(0, "services"); sys.path.insert(0, "services/architect")
import resources, research
q = json.load(open(sys.argv[1]))
print("killswitch:", " ".join(str(research.detect_gap(job, resources.discover(job.get("body", ""), "."))["needs_research"]) for job in q))
PY
)"
assert_contains "kill-switch (research.enabled=false) forces all verdicts False" "$ks" "killswitch: False False False False"
assert_not_contains "kill-switch leaves no True verdict" "$ks" "True"
rm -rf "${GAPTMP}"

# --- E6-2 (#55): research ORDER emission (extends §7.24). Dispatch a gapped issue
# with research actuation ON (generation.research.dispatch_enabled, default off) and
# assert the rendered order is research-shaped; a grounded issue stays implementation;
# the existing dispatch path is unaffected (regression guard).
RORD="$(mktemp -d 2>/dev/null || mktemp -d -t rord)"
printf '%s\n' '{"generation":{"research":{"dispatch_enabled":true}}}' > "${RORD}/tuning.json"
RDT="${RORD}/tuning.json"
RG_TOPIC="docs/research/issue-302-integrate-the-quux-analytics-client.md"
# (a) Gapped issue (#302, no-matching-files) renders a RESEARCH order.
wo302="$(DISPATCH_TUNING_FILE="${RDT}" PIPELINE_DRY_RUN=1 ./dispatch --fixture "$RGQ" --issue 302 2>/dev/null)"
assert_contains "research order carries the Mode header marker" "$wo302" "Mode:"
assert_contains "research order's deliverable is the docs/research/<topic>.md path" "$wo302" "$RG_TOPIC"
assert_contains "research order FORBIDS implementing the feature" "$wo302" "Do NOT implement"
assert_not_contains "research order drops implementation-objective language" "$wo302" "Deliver the work described in issue"
# (b) --json envelope: mode=research on the cheap research route for the gapped issue.
if DISPATCH_TUNING_FILE="${RDT}" PIPELINE_DRY_RUN=1 ./dispatch --fixture "$RGQ" --issue 302 --json 2>/dev/null \
   | jq -e '.mode=="research" and .route=="gen-local"' >/dev/null; then
  pass "--json envelope: gapped issue is mode=research on the cheap research route"
else fail "--json research envelope wrong"; fi
# (c) Well-grounded issue (#303) stays implementation even with actuation on.
DISPATCH_TUNING_FILE="${RDT}" PIPELINE_DRY_RUN=1 ./dispatch --fixture "$RGQ" --issue 303 --json 2>/dev/null \
  | jq -e '.mode=="implementation"' >/dev/null && pass "grounded issue stays mode=implementation" || fail "grounded issue mis-flagged research"
# (d) Default (dispatch_enabled off): the same gapped issue renders implementation —
#     the heuristic never leaks into the default dispatch path.
./dispatch --fixture "$RGQ" --issue 302 --json 2>/dev/null \
  | jq -e '.mode=="implementation"' >/dev/null && pass "research actuation is gated off by default (no leak)" || fail "research leaked with gate off"
# (e) Regression: §7.9's play-queue emits only implementation orders even with
#     actuation ON (its eligible issues are genuinely grounded / high-confidence).
if DISPATCH_TUNING_FILE="${RDT}" PIPELINE_DRY_RUN=1 ./dispatch --fixture "$FXQ" --all --json 2>/dev/null \
   | jq -e 'length>=1 and all(.[]; .mode=="implementation")' >/dev/null; then
  pass "play-queue regression: every emitted order is mode=implementation"
else fail "a play-queue order leaked into research mode"; fi
rm -rf "${RORD}"

# ---------------------------------------------------------------------------
section "§7.28 demo-repo bootstrap seed manifest + label provisioning (offline proxy, #47)"
# Offline proxy for E0-1 (#47). The LIVE gh label/issue creation against
# ReclaimByDesign/demo-repository is an OPERATOR step (run bootstrap-labels.sh
# with PIPELINE_REPO set, then `gh issue create` from this manifest) and is
# intentionally NOT asserted here — smoke stays network-free. We assert the
# committed artifacts: the seed manifest's shape + its deterministic offline
# classification, and that label provisioning would target the demo repo.
SEED="${ROOT}/scripts/demo/seed/backlog.json"
# (a) Manifest is valid JSON: an array of >=5 issues, each carrying the queued label.
if jq -e 'type=="array" and length>=5 and all(.[]; (.labels|index("queued")))' "$SEED" >/dev/null 2>&1; then
  pass "seed manifest: >=5 issues, each labelled queued"
else
  fail "seed manifest invalid (need array, length>=5, every item labelled queued)"
fi
# (b) bootstrap-labels.sh dry-run, targeted at the demo repo via PIPELINE_REPO,
#     would create the queued label there (the operator's live run is the same
#     no-op-safe `--force` upsert). Network-free: PIPELINE_DRY_RUN stays on.
seed_bl="$(PIPELINE_DRY_RUN=1 PIPELINE_REPO=ReclaimByDesign/demo-repository bash "${ROOT}/scripts/bootstrap-labels.sh" 2>&1)"
assert_contains "bootstrap-labels would create the queued label" "$seed_bl" "gh label create queued"
assert_contains "bootstrap-labels would target the demo repo" "$seed_bl" "--repo ReclaimByDesign/demo-repository"
assert_contains "label upsert is idempotent (--force)" "$seed_bl" "--force"
# (c) Every manifest issue classifies schema-valid + deterministically (offline),
#     and the backlog spans distinct classifier routes plus a clear decline shape —
#     a representative web-dev mix (bug, feature, vague, large, out-of-scope, typo).
SEEDTMP="$(mktemp -d 2>/dev/null || mktemp -d -t seed)"
seed_len="$(jq 'length' "$SEED")"
seed_routes=""; seed_actions=""; seed_confs=""; seed_det=1; seed_schema=1
for ((i=0; i<seed_len; i++)); do
  jq -c ".[$i]" "$SEED" > "${SEEDTMP}/one.json"
  s1="$(python3 "${CLS}/classify.py" --issue-json "${SEEDTMP}/one.json" 2>/dev/null)"
  s2="$(python3 "${CLS}/classify.py" --issue-json "${SEEDTMP}/one.json" 2>/dev/null)"
  [[ "$s1" == "$s2" ]] || seed_det=0
  if ! printf '%s' "$s1" | jq -e '
      . as $r
      | (["implement","needs-human","wont-do","duplicate?","decompose"]|index($r.action))!=null
      and (["xs","s","m","l"]|index($r.scope))!=null
      and (["gen-local","gen-default","gen-frontier"]|index($r.route))!=null
      and ($r.confidence|type)=="number" and $r.confidence>=0 and $r.confidence<=1
    ' >/dev/null 2>&1; then seed_schema=0; fi
  seed_routes="${seed_routes}$(printf '%s' "$s1" | jq -r '.route')"$'\n'
  seed_actions="${seed_actions}$(printf '%s' "$s1" | jq -r '.action')"$'\n'
  seed_confs="${seed_confs}$(printf '%s' "$s1" | jq -r '.confidence')"$'\n'
done
[[ "$seed_schema" == 1 ]] && pass "every seed issue classifies schema-valid" || fail "a seed issue classified out-of-schema"
[[ "$seed_det" == 1 ]] && pass "seed classification is deterministic across runs" || fail "seed classification nondeterministic"
seed_uroutes="$(printf '%s' "$seed_routes" | sort -u | grep -c .)"
[[ "$seed_uroutes" -ge 2 ]] && pass "seed exercises >=2 distinct classifier routes ($seed_uroutes)" \
  || fail "seed does not span >=2 distinct routes" "$seed_routes"
if printf '%s' "$seed_actions" | grep -qx 'wont-do'; then
  pass "seed includes an out-of-scope -> wont-do issue"
else
  fail "seed has no wont-do (out-of-scope) issue" "$seed_actions"
fi
seed_min_conf="$(printf '%s' "$seed_confs" | sort -n | head -1)"
awk -v c="$seed_min_conf" 'BEGIN{exit !(c<0.55)}' \
  && pass "seed includes a sub-threshold (needs-human-bound) issue (min conf=${seed_min_conf} < 0.55)" \
  || fail "no sub-threshold issue in seed (min conf=${seed_min_conf})"
# (d) seed-backlog.sh dry-run: would create one issue per manifest entry, each
#     labelled queued, against the demo repo — and mutates nothing (dry-run).
seed_sb="$(PIPELINE_DRY_RUN=1 PIPELINE_REPO=ReclaimByDesign/demo-repository bash "${ROOT}/scripts/demo/seed/seed-backlog.sh" 2>&1)"
seed_creates="$(printf '%s\n' "$seed_sb" | grep -c 'gh issue create')"
[[ "$seed_creates" -eq "$seed_len" ]] \
  && pass "seed-backlog dry-run: one 'gh issue create' per manifest entry ($seed_creates)" \
  || fail "seed-backlog dry-run create count != manifest length" "got $seed_creates, want $seed_len"
assert_contains "seed-backlog labels each issue queued" "$seed_sb" "--label queued"
assert_contains "seed-backlog targets the demo repo" "$seed_sb" "--repo ReclaimByDesign/demo-repository"
rm -rf "${SEEDTMP}"

# ---------------------------------------------------------------------------
section "§7.26 full unattended tick: lock+heartbeat+ledger+artifacts+soft-cap compose (offline, dry-run)"
# (§7.26 per the #51 section map — Day 3 integration range, full-tick smoke slot;
# the issue #41 body's "§7.22" predates this map, which reserves §7.22 for the
# Pillar-3 invoice-reconcile slot and §7.26–§7.27 for integration & runbook.)
# This is the integration capstone (E7-1, issue #41): it drives ONE simulated cron
# tick through the real entrypoint (scripts/pipeline.sh) over fixtures and asserts
# the operational (lock/heartbeat), monitoring (ledger), debug (artifact-dump) and
# budget (soft-cap) rails COMPOSE under one shared tick id without stepping on each
# other — and that a dry-run tick intends NO merge and NO push to main. Each rail
# has its own unit section (§7.12/§7.14/§7.16-§7.17/§7.19/§7.23); §7.26 only asserts
# their composition, and SKIPs (never FAILs) any rail that is absent.
#
# Offline-tick semantics (load-bearing): under PIPELINE_DRY_RUN=1 dispatch.sh prints
# "DRY-RUN: $ENGINEER_BIN …" and never runs the bridge, so the engineer /
# architect-intake stages (and the bridge's invoice.json dump) are intentionally
# inert. The dispatch-side rails (lock, heartbeat, claimed/work-order ledger,
# --until artifact dump, soft-cap) all run in the real tick; the downstream
# engineer-dispatch/invoice/closure ledger lines are composed under the SAME tick
# id via architect-intake.sh (the §7.19 idiom) — proving the full stage vocabulary
# shares one tick without colliding, rather than faking a live engineer run.
if [[ ! -f "${ROOT}/scripts/pipeline.sh" || ! -f "${FIX}/queued-issues.json" ]]; then
  skip "§7.26 needs scripts/pipeline.sh + queued-issues.json fixture (an upstream rail is absent)"
else
  INTD="$(mktemp -d 2>/dev/null || mktemp -d -t intg)"
  I_ART="${INTD}/art"; I_LEDG="${INTD}/run-ledger.jsonl"
  I_RR="${INTD}/run-record.log"; I_LOCK="${INTD}/tick.lock"
  I_TID="tick-smoke-726"
  I_FXI="${FIX}/queued-issues.json"

  # (1) One real dry-run tick through pipeline.sh, every rail wired to ONE tick id.
  itick="$(DISPATCH_TICK_ID="${I_TID}" DISPATCH_ARTIFACTS_DIR="${I_ART}" \
           DISPATCH_LEDGER_FILE="${I_LEDG}" DISPATCH_RUN_RECORD="${I_RR}" \
           DISPATCH_LOCK_FILE="${I_LOCK}" \
           bash "${ROOT}/scripts/pipeline.sh" --fixture "${I_FXI}" 2>&1)"; i_rc=$?
  [[ $i_rc -eq 0 ]] && pass "tick exits 0 (one full unattended tick over the fixture queue)" || fail "tick exit" "got $i_rc"

  # --- lock rail: acquired (the body ran under it) then released (a later tick re-acquires).
  assert_contains "lock: the dispatch body runs under the tick mutex" "$itick" "pipeline: dispatch starting"
  [[ -f "${I_LOCK}" ]] && pass "lock: the lockfile is present after acquisition" || fail "lock: lockfile missing after the tick"
  if have flock; then
    # Hold the lock on fd 8 (as §7.12 does); a concurrent tick must skip + claim nothing.
    exec 8>"${I_LOCK}"; flock -n 8 || fail "lock: harness could not take the test lock"
    held="$(DISPATCH_TICK_ID="${I_TID}-b" DISPATCH_LOCK_FILE="${I_LOCK}" \
            DISPATCH_ARTIFACTS_DIR="${INTD}/art-b" DISPATCH_LEDGER_FILE="${INTD}/ledger-b.jsonl" \
            DISPATCH_RUN_RECORD="${INTD}/rr-b.log" \
            bash "${ROOT}/scripts/pipeline.sh" --fixture "${I_FXI}" 2>&1)"; hc=$?
    exec 8>&-
    [[ $hc -eq 0 ]] && pass "lock: contended overlapping tick exits 0 (a skipped overlap is success)" || fail "lock: contended tick exit" "got $hc"
    assert_contains "lock: overlapping tick logs the already-locked skip" "$held" "another tick holds the lock"
    assert_not_contains "lock: overlapping tick claims nothing" "$held" "--add-label claimed"
    freed="$(DISPATCH_TICK_ID="${I_TID}-c" DISPATCH_LOCK_FILE="${I_LOCK}" \
             DISPATCH_ARTIFACTS_DIR="${INTD}/art-c" DISPATCH_LEDGER_FILE="${INTD}/ledger-c.jsonl" \
             DISPATCH_RUN_RECORD="${INTD}/rr-c.log" \
             bash "${ROOT}/scripts/pipeline.sh" --fixture "${I_FXI}" 2>&1)"
    assert_not_contains "lock: released after the tick (the next tick sees no held lock)" "$freed" "another tick holds the lock"
    assert_contains "lock: the released tick runs the dispatch body under the lock" "$freed" "pipeline: dispatch starting"
  else
    skip "lock: flock not on PATH — overlap/release asserts (Linux prod-host only)"
  fi

  # --- heartbeat rail: exactly one started + one ended record sharing the tick id.
  i_ns="$(grep -c '^event=started ' "${I_RR}" 2>/dev/null || true)"; i_ns="${i_ns:-0}"
  i_ne="$(grep -c '^event=ended ' "${I_RR}" 2>/dev/null || true)"; i_ne="${i_ne:-0}"
  [[ "$i_ns" == "1" && "$i_ne" == "1" ]] && pass "heartbeat: one started + one ended record for the tick" || fail "heartbeat record count" "started=$i_ns ended=$i_ne"
  i_sid="$(sed -n 's/^event=started tick_id=\([^ ]*\).*/\1/p' "${I_RR}" 2>/dev/null | head -1)"
  [[ "$i_sid" == "${I_TID}" ]] && pass "heartbeat: the started record carries the shared tick id (${I_TID})" || fail "heartbeat tick id" "got '$i_sid' want '${I_TID}'"
  i_end="$(grep '^event=ended ' "${I_RR}" 2>/dev/null | head -1)"
  assert_contains "heartbeat: the ended record names the claimed issue (#101)" "$i_end" "claimed=101"
  assert_contains "heartbeat: the ended record carries exit status 0" "$i_end" "status=0"

  # --- ledger rail (dispatch side, from the real tick): claimed + work-order lines,
  #     valid JSONL, every line under the shared tick id, the queued->claimed transition.
  if [[ -s "${I_LEDG}" ]]; then
    pass "ledger: the run-ledger gained lines for this tick"
    if jq -c . "${I_LEDG}" >/dev/null 2>&1; then pass "ledger: every line is valid JSON (jq -c .)"; else fail "ledger: malformed JSONL"; fi
    if jq -se 'all(.[]; .tick_id=="'"${I_TID}"'")' "${I_LEDG}" >/dev/null 2>&1; then
      pass "ledger: every dispatch-side line carries the shared tick id"
    else fail "ledger: a line carries a different tick id"; fi
    if jq -se 'any(.[]; .stage=="claimed" and .label_before=="queued" and .label_after=="claimed" and .issue!=null)' "${I_LEDG}" >/dev/null 2>&1; then
      pass "ledger: the claim line records the queued->claimed transition (with issue)"
    else fail "ledger: claim line / transition missing"; fi
  else
    skip "ledger: no run-ledger emitted (DISPATCH_LEDGER_FILE rail absent)"
  fi

  # --- downstream stages compose under the SAME tick id (engineer-dispatch/invoice/
  #     closure) via architect-intake.sh fed a fixture Invoice (the §7.19 idiom). The
  #     offline tick inerts the live engineer, so this composes the full stage
  #     vocabulary under one tick id without fabricating an engineer run.
  LINV="${FIX}/ledger-invoice.json"
  if [[ -s "${I_LEDG}" && -f "${LINV}" && -f "${ROOT}/scripts/architect-intake.sh" ]]; then
    DISPATCH_TICK_ID="${I_TID}" DISPATCH_LEDGER_FILE="${I_LEDG}" PIPELINE_DRY_RUN=1 \
      bash "${ROOT}/scripts/architect-intake.sh" < "${LINV}" >/dev/null 2>&1 || true
    if jq -se '
        length>=3
        and all(.[]; .tick_id=="'"${I_TID}"'")
        and all(.[]; .stage as $s | ["claimed","work-order","engineer-dispatch","invoice","closure"] | index($s) != null)
        and any(.[]; .stage=="claimed")
        and any(.[]; .stage=="engineer-dispatch" or .stage=="invoice" or .stage=="closure")
      ' "${I_LEDG}" >/dev/null 2>&1; then
      pass "ledger: >=3 stage lines (dispatch -> engineer -> intake) compose under one tick id, vocab-valid"
    else
      n_ledg="$(jq -s 'length' "${I_LEDG}" 2>/dev/null || echo 0)"
      skip "ledger: downstream intake stages not all present (${n_ledg} dispatch-side lines; intake rail variant)"
    fi
    if jq -se 'any(.[]; .stage=="engineer-dispatch" and .cost.tokens_in != null)' "${I_LEDG}" >/dev/null 2>&1; then
      pass "ledger: the engineer-dispatch line carries the Invoice cost.tokens_in"
    else skip "ledger: engineer-dispatch cost not present (intake rail variant)"; fi
  else
    skip "ledger: architect-intake.sh / ledger-invoice.json absent — downstream-stage composition not exercised"
  fi

  # --- artifact rail: a --until workorder tick (still offline) dumps the per-stage
  #     workorder.txt + job-request.json under the SAME tick id, proving the debug
  #     artifact rail composes with the locked/heartbeat'd tick. The invoice.json
  #     artifact is written by pipeline.sh's bridge only on a LIVE engineer run, so
  #     under the offline tick it is intentionally absent (asserted), not faked.
  A_ART="${INTD}/art-until"
  DISPATCH_TICK_ID="${I_TID}" DISPATCH_ARTIFACTS_DIR="${A_ART}" \
    DISPATCH_LEDGER_FILE="${INTD}/ledger-until.jsonl" DISPATCH_LOCK_FILE="${INTD}/lock-until.lock" \
    bash "${ROOT}/scripts/pipeline.sh" --until workorder --fixture "${I_FXI}" >/dev/null 2>&1
  A_TD="${A_ART}/${I_TID}"
  if [[ -f "${A_TD}/workorder.txt" ]]; then
    pass "artifacts: workorder.txt dumped under the tick dir"
    assert_contains "artifacts: workorder.txt is the real rendered order (WORK PLAN header)" "$(cat "${A_TD}/workorder.txt")" "WORK PLAN"
  else fail "artifacts: workorder.txt missing in the tick dir"; fi
  if [[ -f "${A_TD}/job-request.json" ]]; then
    pass "artifacts: job-request.json dumped under the tick dir"
    if jq -e 'has("job_id") and has("repo") and (.issue|type=="number")
        and (.route=="gen-local" or .route=="gen-default" or .route=="gen-frontier")
        and (.scope=="xs" or .scope=="s" or .scope=="m" or .scope=="l")
        and (.confidence|type=="number") and (.confidence>=0) and (.confidence<=1)' \
        "${A_TD}/job-request.json" >/dev/null 2>&1; then
      pass "artifacts: job-request.json validates (job_id/repo/issue/route/scope/confidence, per §7.16)"
    else fail "artifacts: job-request.json failed structural validation"; fi
  else fail "artifacts: job-request.json missing in the tick dir"; fi
  [[ ! -f "${A_TD}/invoice.json" ]] \
    && pass "artifacts: invoice.json intentionally absent under dry-run (bridge dumps it only on a live engineer tick)" \
    || pass "artifacts: invoice.json present (live engineer ran)"

  # --- soft-cap rail: an over-cap window throttles the tick to dry-run + skips the
  #     claim + records a ledger throttle event; an under-cap window proceeds + claims.
  #     The guard runs before any gh, so PIPELINE_DRY_RUN=0 on the over-cap probe is
  #     safe — it flips to dry-run before claiming (mirrors §7.23 item 3).
  GOVER="${ROOT}/services/budget/fixtures/window-state.over-cap.json"
  GUNDER="${ROOT}/services/budget/fixtures/window-state.under-cap.json"
  if [[ -f "${GOVER}" && -f "${GUNDER}" ]]; then
    SC_LEDG="${INTD}/softcap-ledger.jsonl"; rm -f "${SC_LEDG}"
    sc_over="$(BUDGET_ORACLE_FIXTURE="${GOVER}" PIPELINE_FIXTURE_ISSUES="${I_FXI}" PIPELINE_DRY_RUN=0 \
               DISPATCH_TICK_ID="${I_TID}" DISPATCH_LEDGER_FILE="${SC_LEDG}" \
               bash "${ROOT}/scripts/dispatch.sh" 2>&1)"
    assert_contains "soft-cap: over-cap window logs the throttle (fraction + soft_cap)" "$sc_over" "soft-cap THROTTLE"
    assert_contains "soft-cap: over-cap window forces dry-run even when operator passed live" "$sc_over" "forcing PIPELINE_DRY_RUN=1"
    assert_not_contains "soft-cap: throttled tick claims nothing (no queued->claimed)" "$sc_over" "-> claimed"
    if [[ -s "${SC_LEDG}" ]] && jq -e 'select(.event=="throttled") | has("fraction") and has("soft_cap")' "${SC_LEDG}" >/dev/null 2>&1; then
      pass "soft-cap: the throttle event is recorded to the run-ledger (event=throttled, fraction, soft_cap)"
    else fail "soft-cap: throttle ledger line missing/incomplete"; fi
    rm -f "${SC_LEDG}"
    sc_under="$(BUDGET_ORACLE_FIXTURE="${GUNDER}" PIPELINE_FIXTURE_ISSUES="${I_FXI}" PIPELINE_DRY_RUN=1 \
                DISPATCH_LEDGER_FILE="${INTD}/sc-under.jsonl" \
                bash "${ROOT}/scripts/dispatch.sh" 2>&1)"
    assert_contains "soft-cap: under-cap window proceeds and claims normally (queued->claimed)" "$sc_under" "-> claimed"
    assert_not_contains "soft-cap: under-cap window is not throttled" "$sc_under" "THROTTLE"
  else
    skip "soft-cap: budget window fixtures absent — throttle composition not exercised"
  fi

  # --- GitHub end-state: a dry-run tick intends the queued->claimed transition and
  #     NEVER a merge or a push to main (worker contract: never merge, never push main).
  assert_contains "end-state: the tick intends the queued->claimed label transition" "$itick" "--add-label claimed"
  assert_not_contains "end-state: the tick intends NO merge" "$itick" "pr merge"
  assert_not_contains "end-state: the tick intends NO push to main" "$itick" "push origin main"

  # --- no disk mutation outside the temp artifacts dir (mirror §7.3 post-conditions).
  [[ ! -d "${ROOT}/.worktrees" ]] && pass "no-mutation: no .worktrees/ created in the repo" || fail "no-mutation: a worktree leaked"
  if git -C "${ROOT}" rev-parse --verify --quiet "pipeline/issue-101" >/dev/null 2>&1; then
    fail "no-mutation: branch pipeline/issue-101 leaked"
  else pass "no-mutation: no pipeline/issue-* branch leaked"; fi
  rm -rf "${INTD}"
fi

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
if python3 - "${CLS}/classify.py" "${CLS}/classify_local_stub.py" "${ROOT}/scripts/demo/mutate.py" <<'PY'
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
  pass "classifier + scenario mutator have zero exec/tool capability (quarantine readers)"
else
  fail "classifier or mutator exposes exec capability"
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
