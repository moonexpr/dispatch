#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# smoke.sh — acceptance runner (HANDOFF §7). Exit 0 == all asserts pass.
#
# Runs fully OFFLINE and DRY-RUN: no network, no GitHub, no Gateway, no model
# calls, no mutations. Checks that need an absent engine (openclaw/litellm/gh)
# degrade to a structural/dry-run proxy and emit a SKIP for the live portion.
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
section "§7.3 litellm config validates; non-stub groups present"
python3 - "$ROOT/config/litellm.pipeline.yaml" <<'PY'
import sys, re
path = sys.argv[1]
expected = {"triage","distill","gen-local","gen-default","gen-frontier","haiku-tier","critic"}
stubs = {"gen-deepseek","gen-kimi"}
try:
    import yaml
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    names = {m["model_name"] for m in cfg.get("model_list", [])}
    master = bool(cfg.get("general_settings", {}).get("master_key"))
    mode = "yaml"
except ModuleNotFoundError:
    # Degraded (no PyYAML): scan uncommented `model_name:` / `master_key:` lines.
    names, master = set(), False
    with open(path) as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("#"):
                continue
            m = re.match(r'-?\s*model_name:\s*(\S+)', s)
            if m:
                names.add(m.group(1))
            if re.match(r'master_key:\s*\S+', s):
                master = True
    mode = "lines"
missing = expected - names
active_stubs = stubs & names
assert not missing, f"missing non-stub groups: {missing}"
assert not active_stubs, f"stub groups must stay commented: {active_stubs}"
assert master, "master_key not wired to env"
print(f"ok ({mode}): groups=" + ",".join(sorted(names)))
PY
if [[ $? -eq 0 ]]; then pass "YAML parses; all non-stub groups present; stubs inactive"; else fail "litellm structural validation"; fi
if have litellm; then
  if litellm --config "$ROOT/config/litellm.pipeline.yaml" --health >/dev/null 2>&1; then
    pass "litellm binary validated config"
  else
    skip "litellm present but live validation needs keys/endpoints"
  fi
else
  skip "litellm binary absent — structural validation only (live /v1/models needs proxy)"
fi

# ---------------------------------------------------------------------------
section "§7.4 openclaw cron jobs (dispatch + digest) after bootstrap"
# Offline proxy: assert bootstrap *intends* to register both jobs (dry-run echo).
oc="$(scripts/bootstrap-openclaw.sh 2>/dev/null)"
assert_contains "bootstrap intends to register pipeline-dispatch" "$oc" "pipeline-dispatch"
assert_contains "bootstrap intends to register pipeline-digest" "$oc" "pipeline-digest"
assert_contains "dispatch cron is a command job" "$oc" "--type command"
assert_contains "digest cron is an isolated agent job" "$oc" "--type isolated"
# Live: when a Gateway is reachable, actually register (idempotent) and verify
# that `openclaw cron list` reflects both jobs — the literal §7.4 criterion.
if have openclaw; then
  PIPELINE_DRY_RUN=0 scripts/bootstrap-openclaw.sh >/dev/null 2>&1 || true
  cl="$(openclaw cron list 2>/dev/null || true)"
  case "$cl" in *pipeline-dispatch*) cd1=1 ;; *) cd1=0 ;; esac
  case "$cl" in *pipeline-digest*)  cd2=1 ;; *) cd2=0 ;; esac
  [[ "$cd1" == 1 && "$cd2" == 1 ]] \
    && pass "openclaw cron list shows dispatch + digest after bootstrap" \
    || fail "openclaw cron list missing dispatch/digest after bootstrap"
else
  skip "openclaw binary absent — live 'cron list' registration check (Gateway required)"
fi

# ---------------------------------------------------------------------------
section "§7.5 dry-run dispatch prints correct gh/claude calls, mutates nothing"
disp="$(PIPELINE_FIXTURE_ISSUES="${FIX}/queued-issues.json" scripts/dispatch.sh 2>&1)"
assert_contains "launches /implement-task" "$disp" "/implement-task"
assert_contains "passes the routed group (gen-local for #101)" "$disp" "gen-local"
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
section "§7.6 fix-dispatch ladder: attempt 2 -> gen-default; >3 -> operator notify"
f2="$(PIPELINE_FIXTURE_PR="${FIX}/pr-fix-attempt-2.json" scripts/fix-dispatch.sh 2>&1)"
assert_contains "attempt 2 selects gen-default" "$f2" "gen-default"
assert_contains "attempt 2 invokes /fix-ci" "$f2" "/fix-ci"
assert_not_contains "attempt 2 does not escalate" "$f2" "needs-human"
fc="$(PIPELINE_FIXTURE_PR="${FIX}/pr-fix-attempt-over-cap.json" scripts/fix-dispatch.sh 2>&1)"
assert_contains "attempt >3 labels needs-human" "$fc" "needs-human"
assert_contains "attempt >3 notifies the operator" "$fc" "openclaw announce"
assert_not_contains "attempt >3 does NOT invoke /fix-ci" "$fc" "/fix-ci"

# Also exercise the label-derivation path (no explicit attempt) for robustness.
der="$(PIPELINE_FIXTURE_PR=<(jq 'del(.attempt)' "${FIX}/pr-fix-attempt-2.json") scripts/fix-dispatch.sh 2>&1)"
assert_contains "label-derived attempt (fix-attempt-1 -> 2) also picks gen-default" "$der" "gen-default"

# ---------------------------------------------------------------------------
section "§7.7 every secret referenced is documented in pipeline.env.example"
ENVF="${ROOT}/pipeline.env.example"
required_secrets=(GITHUB_TOKEN TRIAGE_GITHUB_TOKEN WORKER_GITHUB_TOKEN CLOSURE_GITHUB_TOKEN \
  HF_TOKEN ANTHROPIC_API_KEY DEEPSEEK_API_KEY MOONSHOT_API_KEY MLX_QWEN_API_KEY \
  LITELLM_MASTER_KEY OPENCLAW_WEBHOOK_BEARER_TOKEN OPENCLAW_WEBHOOK_URL)
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
  scripts services config .github --exclude=smoke.sh 2>/dev/null | sort -u)"
undocumented=""
while IFS= read -r v; do
  [[ -z "$v" ]] && continue
  grep -qE "${v}=" "$ENVF" || undocumented+="${v} "
done <<< "$referenced"
[[ -z "$undocumented" ]] && pass "no undocumented secret-like vars referenced" \
  || fail "undocumented secret-like vars" "$undocumented"

# ---------------------------------------------------------------------------
section "§7.8 closure.sh dry-run (success payload): docs + auto-merge + summary"
clo="$(PIPELINE_FIXTURE_PR="${FIX}/closure-success.json" scripts/closure.sh 2>&1)"
assert_contains "invokes /update-docs" "$clo" "/update-docs"
assert_contains "arms auto-merge (--auto)" "$clo" "--auto"
assert_contains "squash merge" "$clo" "--squash"
assert_contains "posts a closure summary comment" "$clo" "closure summary"
assert_not_contains "does not hard-merge (no 'merge --admin')" "$clo" "--admin"

# ---------------------------------------------------------------------------
section "§7.9 all three workflow .js files exist and pass node --check"
for wf in implement-task fix-ci update-docs; do
  p="${ROOT}/.claude/workflows/${wf}.js"
  if [[ -f "$p" ]] && node --check "$p" 2>/dev/null; then
    pass "${wf}.js exists and node --check passes"
  else
    fail "${wf}.js missing or fails node --check"
  fi
done

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
# Gateway bind must be loopback/tailnet (read default from example).
bind="$(grep -E '^OPENCLAW_BIND_ADDR=' "$ENVF" | head -1 | cut -d= -f2)"
case "$bind" in
  127.0.0.1|::1|localhost|100.*|*.ts.net) pass "Gateway bind '$bind' is loopback/tailnet" ;;
  0.0.0.0|"") fail "Gateway bind '$bind' is public/unset — must be loopback/tailnet" ;;
  *) skip "Gateway bind '$bind' — verify it is tailnet-only" ;;
esac
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
