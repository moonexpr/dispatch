# shellcheck shell=bash
# ---------------------------------------------------------------------------
# common.sh — shared contract for the Unattended Engineering Pipeline (v0)
#
# Sourced by every script under scripts/. Defines the SINGLE source of truth
# for: env-var defaults, the label vocabulary + state machine, the
# scope->route map, the fix-ladder, dry-run semantics, and the thin wrappers
# around `gh` / `claude` / `openclaw` that make those engines honour
# PIPELINE_DRY_RUN.
#
# Design rules (see HANDOFF-pipeline-v0.md §2):
#   - We do NOT build engines. These helpers only *shape calls* to existing
#     engines (gh, claude, openclaw, litellm, python).
#   - Dry-run defaults ON (§8). Nothing mutates unless PIPELINE_DRY_RUN=0.
#   - All durable state lives in GitHub (labels/comments/PRs), never on disk.
#
# This file has no top-level `set -e`; it is safe to source. Each entry-point
# script sets its own strict mode.
# ---------------------------------------------------------------------------

# Resolve repo root (this file lives at <root>/scripts/lib/common.sh).
PIPELINE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIPELINE_ROOT="$(cd "${PIPELINE_LIB_DIR}/../.." && pwd)"
export PIPELINE_ROOT

# --- Load pipeline.env if present (never overrides already-exported vars) ---
# Operators copy pipeline.env.example -> pipeline.env (gitignored) and fill it.
if [[ -f "${PIPELINE_ROOT}/pipeline.env" ]]; then
  set -a
  # shellcheck disable=SC1091  # operator-provided file; absent at lint time
  source "${PIPELINE_ROOT}/pipeline.env"
  set +a
fi

# --------------------------- Env-var defaults ------------------------------
# Safety-critical: dry-run is ON unless explicitly disabled by the operator.
: "${PIPELINE_DRY_RUN:=1}"
: "${PIPELINE_REPO:=}"                       # owner/repo for gh; empty => gh default
: "${PIPELINE_CONFIDENCE_THRESHOLD:=0.55}"   # below this => needs-human
: "${PIPELINE_CONCURRENCY:=1}"               # v0: one claimed issue at a time

# Tool binaries (overridable so tests / odd PATHs work).
: "${GH_BIN:=gh}"
: "${CLAUDE_BIN:=claude}"
: "${OPENCLAW_BIN:=openclaw}"
: "${PYTHON_BIN:=python3}"

# How `claude` workflow args are passed. The CURRENT CLI has no `--args` flag
# (verified against code.claude.com/docs); the documented headless form embeds
# the args in the prompt and the workflow reads the `args` global. We default
# to that ("prompt"); set CLAUDE_ARGS_MODE=flag to use `--args` if/when the
# flag ships. See OPEN-QUESTIONS.md.
: "${CLAUDE_ARGS_MODE:=prompt}"

# Worktree isolation root for worker sessions (§5.1 / §5.4 step 3).
: "${PIPELINE_WORKTREE_ROOT:=${PIPELINE_ROOT}/.worktrees}"

# OpenClaw operational defaults (real values come from pipeline.env; these
# fallbacks keep scripts safe under `set -u` when pipeline.env is absent).
: "${OPENCLAW_BIND_ADDR:=127.0.0.1}"
: "${OPENCLAW_OPERATOR_CHANNEL:=}"
: "${OPENCLAW_DISPATCH_SCHEDULE:=*/10 * * * *}"
: "${OPENCLAW_DIGEST_SCHEDULE:=0 8 * * *}"
: "${OPENCLAW_MODEL_CHEAP:=haiku-tier}"

# Test seams (only consulted when set). Let smoke.sh drive scripts without a
# live GitHub / HF / Gateway. Never required in production.
: "${PIPELINE_FIXTURE_ISSUES:=}"   # JSON array of queued issues for dispatch
: "${PIPELINE_FIXTURE_PR:=}"       # JSON object describing a PR for fix/closure
: "${PIPELINE_FIX_ATTEMPT:=}"      # explicit attempt override for fix-dispatch
: "${CLASSIFIER_OFFLINE:=}"        # 1 => deterministic offline classification

export PIPELINE_DRY_RUN PIPELINE_REPO PIPELINE_CONFIDENCE_THRESHOLD \
       PIPELINE_CONCURRENCY GH_BIN CLAUDE_BIN OPENCLAW_BIN PYTHON_BIN \
       CLAUDE_ARGS_MODE PIPELINE_WORKTREE_ROOT

# ------------------------------- Logging -----------------------------------
_ts() { date -u +%H:%M:%SZ 2>/dev/null || echo "--:--:--Z"; }
log()  { printf '[%s] %s\n'      "$(_ts)" "$*" >&2; }
warn() { printf '[%s] WARN: %s\n' "$(_ts)" "$*" >&2; }
err()  { printf '[%s] ERROR: %s\n' "$(_ts)" "$*" >&2; }
die()  { err "$*"; exit 1; }

# --------------------------- Dry-run plumbing ------------------------------
is_dry_run() { [[ "${PIPELINE_DRY_RUN}" != "0" ]]; }

# run: execute a mutating command, or print it (greppable) under dry-run.
# Reads as plain tokens so smoke.sh can assert on substrings.
run() {
  if is_dry_run; then
    echo "DRY-RUN: $*"
  else
    "$@"
  fi
}

# require_tool: hard-fail if a binary is missing (used on the live path only).
require_tool() {
  command -v "$1" >/dev/null 2>&1 || die "required tool not found on PATH: $1"
}

# have_tool: soft check.
have_tool() { command -v "$1" >/dev/null 2>&1; }

# ------------------------- Engine call wrappers ----------------------------
# gh_repo_flag: emit `--repo owner/repo` when PIPELINE_REPO is set.
gh_repo_flag() { [[ -n "${PIPELINE_REPO}" ]] && printf -- '--repo %s' "${PIPELINE_REPO}"; }

# gh_mutate: a gh call that changes GitHub state -> always via run() (dry-run aware).
gh_mutate() {
  # shellcheck disable=SC2046
  run "${GH_BIN}" "$@" $(gh_repo_flag)
}

# claude_invoke: launch a saved dynamic workflow headlessly (dry-run aware).
#   $1 = workflow name (without leading slash), $2 = JSON args string.
# Keeps "/<workflow>" and the JSON visible in both modes so the routing
# decision is auditable in dry-run output (acceptance §7.5/§7.8).
claude_invoke() {
  local workflow="$1" args_json="$2"
  case "${CLAUDE_ARGS_MODE}" in
    flag)
      run "${CLAUDE_BIN}" -p "/${workflow}" --args "${args_json}" ;;
    prompt|*)
      run "${CLAUDE_BIN}" -p "Run /${workflow} with args ${args_json}" ;;
  esac
}

# openclaw_announce: notify the operator channel (dry-run aware).
openclaw_announce() {
  local message="$1"
  run "${OPENCLAW_BIN}" announce \
      ${OPENCLAW_OPERATOR_CHANNEL:+--channel "${OPENCLAW_OPERATOR_CHANNEL}"} \
      --message "${message}"
}

# --------------------- Label vocabulary / state machine --------------------
# §5.6: queued -> claimed -> pr-open -> in-review -> docs-pending -> done
# Transition owners are documented in each script header.
PIPELINE_STATE_LABELS=(queued claimed pr-open in-review docs-pending "done" done-pending-merge)
PIPELINE_FLAG_LABELS=(needs-human wont-do duplicate)
PIPELINE_FIX_LABELS=(fix-attempt-1 fix-attempt-2 fix-attempt-3)

# label_color / label_desc: presentation metadata for bootstrap-labels.sh.
label_color() {
  case "$1" in
    queued)             echo "0e8a16" ;;  # green: ready to claim
    claimed)            echo "fbca04" ;;  # yellow: in flight
    pr-open)            echo "1d76db" ;;  # blue
    in-review)          echo "5319e7" ;;  # purple
    docs-pending)       echo "0052cc" ;;  # darker blue
    done)               echo "c2e0c6" ;;  # pale green
    done-pending-merge) echo "bfd4f2" ;;  # pale blue
    needs-human)        echo "b60205" ;;  # red: escalation
    wont-do)            echo "e99695" ;;  # pink-red
    duplicate)          echo "cccccc" ;;  # grey
    fix-attempt-1)      echo "fef2c0" ;;  # pale yellow
    fix-attempt-2)      echo "fbca04" ;;  # yellow
    fix-attempt-3)      echo "d93f0b" ;;  # orange-red
    *)                  echo "ededed" ;;
  esac
}
label_desc() {
  case "$1" in
    queued)             echo "Intake: ready for dispatch/classification" ;;
    claimed)            echo "Dispatch claimed this issue (concurrency=1 in v0)" ;;
    pr-open)            echo "Worker opened a PR; CI + review running" ;;
    in-review)          echo "Adversarial review submitted (advisory)" ;;
    docs-pending)       echo "Closure: docs/CHANGELOG update in progress" ;;
    done)               echo "Issue merged + closed by the pipeline" ;;
    done-pending-merge) echo "Auto-merge armed; waiting on human approval tap" ;;
    needs-human)        echo "Escalated to operator (low confidence / retry cap)" ;;
    wont-do)            echo "Classifier/operator declined the work" ;;
    duplicate)          echo "Suspected duplicate; needs human confirmation" ;;
    fix-attempt-1)      echo "CI fix attempt 1 (ladder tier: gen-local)" ;;
    fix-attempt-2)      echo "CI fix attempt 2 (ladder tier: gen-default)" ;;
    fix-attempt-3)      echo "CI fix attempt 3 (ladder tier: gen-frontier)" ;;
    *)                  echo "pipeline label" ;;
  esac
}

# all_pipeline_labels: ordered list for bootstrap + smoke.
all_pipeline_labels() {
  printf '%s\n' "${PIPELINE_STATE_LABELS[@]}" \
                "${PIPELINE_FLAG_LABELS[@]}" \
                "${PIPELINE_FIX_LABELS[@]}"
}

# ------------------------- Routing / ladder logic --------------------------
# route_for_scope: classifier scope -> generation model group (§5.2 / §5.3).
route_for_scope() {
  case "$1" in
    xs|s) echo "gen-local" ;;
    m)    echo "gen-default" ;;
    l)    echo "gen-frontier" ;;
    *)    echo "gen-default" ;;
  esac
}

# tier_for_attempt: fix-ladder (§5.5). attempt>3 => the cap sentinel.
#   1 -> gen-local, 2 -> gen-default, 3 -> gen-frontier, >3 -> needs-human
tier_for_attempt() {
  case "$1" in
    1) echo "gen-local" ;;
    2) echo "gen-default" ;;
    3) echo "gen-frontier" ;;
    *) echo "needs-human" ;;   # >3 (or 0/invalid) => escalate
  esac
}

# critic_route: pick a cross-family critic group opposite the generator (§5.2).
critic_route() {
  case "$1" in
    gen-local)    echo "gen-default" ;;   # local generator -> hosted critic
    gen-default)  echo "gen-frontier" ;;
    gen-frontier) echo "gen-default" ;;
    *)            echo "critic" ;;
  esac
}
