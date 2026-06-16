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
    | (["implement","needs-human","wont-do","duplicate?"] | index($r.action)) != null
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
