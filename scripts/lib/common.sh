# shellcheck shell=bash
# ---------------------------------------------------------------------------
# common.sh — shared contract for the Unattended Engineering Pipeline (v0)
#
# Sourced by every script under scripts/. Defines the SINGLE source of truth
# for: env-var defaults, the label vocabulary + state machine, the
# scope->route map, the fix-ladder, dry-run semantics, and the thin wrappers
# around `gh` / `claude` that make those engines honour PIPELINE_DRY_RUN.
#
# Design rules (see HANDOFF-pipeline-v0.md §2):
#   - We do NOT build engines. These helpers only *shape calls* to existing
#     engines (gh, claude, python).
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
# Save vars that callers may have set on the command line before sourcing, so
# pipeline.env defaults do not clobber explicit overrides (e.g. PIPELINE_DRY_RUN=0).
_pre_dry_run="${PIPELINE_DRY_RUN:-}"
_pre_concurrency="${PIPELINE_CONCURRENCY:-}"
_pre_repo="${PIPELINE_REPO:-}"
_pre_engineer="${ENGINEER_BIN:-}"
if [[ -f "${PIPELINE_ROOT}/pipeline.env" ]]; then
  set -a
  # shellcheck disable=SC1091  # operator-provided file; absent at lint time
  source "${PIPELINE_ROOT}/pipeline.env"
  set +a
fi
[[ -n "${_pre_dry_run}"    ]] && export PIPELINE_DRY_RUN="${_pre_dry_run}"
[[ -n "${_pre_concurrency}" ]] && export PIPELINE_CONCURRENCY="${_pre_concurrency}"
[[ -n "${_pre_repo}"       ]] && export PIPELINE_REPO="${_pre_repo}"
[[ -n "${_pre_engineer}"   ]] && export ENGINEER_BIN="${_pre_engineer}"
unset _pre_dry_run _pre_concurrency _pre_repo _pre_engineer

# --------------------------- Env-var defaults ------------------------------
# Safety-critical: dry-run is ON unless explicitly disabled by the operator.
: "${PIPELINE_DRY_RUN:=1}"
: "${PIPELINE_REPO:=}"                       # owner/repo for gh; empty => gh default
: "${PIPELINE_CONFIDENCE_THRESHOLD:=0.55}"   # below this => needs-human
: "${PIPELINE_CONCURRENCY:=1}"               # v0: one claimed issue at a time

# Tool binaries (overridable so tests / odd PATHs work).
: "${GH_BIN:=gh}"
: "${CLAUDE_BIN:=claude}"
: "${PYTHON_BIN:=python3}"
: "${FLOCK_BIN:=flock}"                       # tick mutex; absent on macOS dev hosts

# --------------------------- Tick concurrency lock -------------------------
# Single-host mutex around a whole tick so overlapping cron invocations can't
# both claim the same queued issue (E1-1). Repo-scoped so distinct target
# repos don't block each other. DISPATCH_LOCK_WAIT=0 (default) = non-blocking:
# a second concurrent tick skips and exits 0; a positive value waits that many
# seconds for the lock (passed to flock -w).
: "${DISPATCH_LOCK_FILE:=${TMPDIR:-/tmp}/dispatch-${PIPELINE_REPO//\//-}.lock}"
: "${DISPATCH_LOCK_WAIT:=0}"

# --------------------------- Per-tick run record (E1-2) --------------------
# Heartbeat seam: pipeline.sh emits one `started` and one `ended` key=value line
# per tick to DISPATCH_RUN_RECORD so that a tick that ran leaves a durable local
# trace (today log() goes to stderr and is lost). A crashed tick leaves a
# `started` with no matching success `ended` — exactly the signal the E1-3
# reaper and the later E4 JSONL run-ledger key off. The record is ALWAYS written
# (even under dry-run — a dry-run tick still produced a run worth recording) and
# never calls `gh`. Default sink lives under DISPATCH_ARTIFACTS_DIR (or .dispatch).
: "${DISPATCH_RUN_RECORD:=${DISPATCH_ARTIFACTS_DIR:-.dispatch}/run-record.log}"

# How `claude` workflow args are passed. The CURRENT CLI has no `--args` flag
# (verified against code.claude.com/docs); the documented headless form embeds
# the args in the prompt and the workflow reads the `args` global. We default
# to that ("prompt"); set CLAUDE_ARGS_MODE=flag to use `--args` if/when the
# flag ships. See OPEN-QUESTIONS.md.
: "${CLAUDE_ARGS_MODE:=prompt}"

# Worktree isolation root for worker sessions (§5.1 / §5.4 step 3).
: "${PIPELINE_WORKTREE_ROOT:=${PIPELINE_ROOT}/.worktrees}"

# Test seams (only consulted when set). Let smoke.sh drive scripts without a
# live GitHub / HF / Gateway. Never required in production.
: "${PIPELINE_FIXTURE_ISSUES:=}"   # JSON array of queued issues for dispatch
: "${PIPELINE_FIXTURE_PR:=}"       # JSON object describing a PR for fix/closure
: "${PIPELINE_FIX_ATTEMPT:=}"      # explicit attempt override for fix-dispatch
: "${CLASSIFIER_OFFLINE:=}"        # 1 => deterministic offline classification

export PIPELINE_DRY_RUN PIPELINE_REPO PIPELINE_CONFIDENCE_THRESHOLD \
       PIPELINE_CONCURRENCY GH_BIN CLAUDE_BIN PYTHON_BIN FLOCK_BIN \
       CLAUDE_ARGS_MODE PIPELINE_WORKTREE_ROOT \
       DISPATCH_LOCK_FILE DISPATCH_LOCK_WAIT DISPATCH_RUN_RECORD DISPATCH_LEDGER_FILE

# ------------------------------- Logging -----------------------------------
_ts() { date -u +%H:%M:%SZ 2>/dev/null || echo "--:--:--Z"; }
_iso8601() { date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "1970-01-01T00:00:00Z"; }
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

# with_tick_lock CMD [ARGS...]: run CMD under an exclusive single-host flock so
# overlapping ticks can't double-claim a queued issue (E1-1). The lock is held
# for CMD's entire lifetime and released by fd-close when the subshell exits —
# even on a `set -e` failure inside CMD — so no manual trap can be skipped.
#
# Contention is governed by DISPATCH_LOCK_WAIT: 0 (default) is non-blocking — a
# second concurrent tick logs and exits 0 (a skipped overlap is success, not a
# failure cron should alarm on); a positive N waits up to N seconds (flock -w N).
# If flock is absent (e.g. a macOS dev host; prod cron is Linux), WARN naming
# the missing tool and run CMD UNLOCKED rather than aborting the tick.
with_tick_lock() {
  local lock="${DISPATCH_LOCK_FILE}" waitsec="${DISPATCH_LOCK_WAIT:-0}"
  if ! have_tool "${FLOCK_BIN}"; then
    warn "${FLOCK_BIN} not on PATH — running tick UNLOCKED (overlapping ticks not serialized)"
    "$@"
    return $?
  fi
  # Create the lockfile lazily; if its directory is unwritable, degrade to
  # unlocked rather than failing the tick.
  if ! ( umask 077; : >>"${lock}" ) 2>/dev/null; then
    warn "cannot open lock file ${lock} — running tick UNLOCKED"
    "$@"
    return $?
  fi
  local fargs=(-n)
  [[ "${waitsec}" =~ ^[1-9][0-9]*$ ]] && fargs=(-w "${waitsec}")
  # fd 9 is a fresh open-file-description per subshell; flock on it conflicts
  # with any other tick's lock and is dropped when the subshell exits.
  (
    if ! "${FLOCK_BIN}" "${fargs[@]}" 9; then
      log "pipeline: another tick holds the lock; exiting 0"
      exit 0
    fi
    "$@"
  ) 9>>"${lock}"
}

# --------------------------- Tick run-record helpers (E1-2) ----------------
# tick_record_start / tick_record_end emit the per-tick heartbeat. Both write a
# single structured key=value line to DISPATCH_RUN_RECORD and NEVER call `gh`,
# so they are safe on the dry-run path. pipeline.sh calls them INSIDE the E1-1
# lock so the record reflects exactly one serialized tick. The format is stable
# (key=value, one record per line) so the E4 ledger can parse it unchanged.

# _tick_claimed_file: the tick-scoped sink dispatch.sh appends claimed issue
# numbers to, so tick_record_end reports them without re-querying GitHub.
# Keyed by the shared DISPATCH_TICK_ID under DISPATCH_ARTIFACTS_DIR (or .dispatch).
_tick_claimed_file() {
  echo "${DISPATCH_CLAIMED_FILE:-${DISPATCH_ARTIFACTS_DIR:-.dispatch}/${DISPATCH_TICK_ID:-tick-unknown}.claimed}"
}

# tick_record_start: write the `started` record (tick-id, UTC start, repo,
# dry-run flag) and (re)initialise this tick's claimed sink. Exports
# DISPATCH_CLAIMED_FILE so child scripts append to the very same path.
tick_record_start() {
  : "${DISPATCH_TICK_ID:=tick-$(date -u +%Y%m%dT%H%M%SZ)}"; export DISPATCH_TICK_ID
  DISPATCH_CLAIMED_FILE="$(_tick_claimed_file)"; export DISPATCH_CLAIMED_FILE
  mkdir -p "$(dirname "${DISPATCH_RUN_RECORD}")" "$(dirname "${DISPATCH_CLAIMED_FILE}")" 2>/dev/null || true
  : >"${DISPATCH_CLAIMED_FILE}" 2>/dev/null || true   # fresh sink per tick
  printf 'event=started tick_id=%s ts=%s repo=%s dry_run=%s\n' \
    "${DISPATCH_TICK_ID}" "$(_iso8601)" "${PIPELINE_REPO:-}" "${PIPELINE_DRY_RUN}" \
    >>"${DISPATCH_RUN_RECORD}"
}

# tick_record_claim NUM: append a claimed issue number to this tick's sink.
tick_record_claim() {
  local f="${DISPATCH_CLAIMED_FILE:-$(_tick_claimed_file)}"
  [[ -n "${f}" ]] || return 0
  printf '%s\n' "$1" >>"${f}" 2>/dev/null || true
}

# tick_record_end STATUS: write the `ended` record (UTC end, exit status, and
# the count + numbers of issues claimed this tick). A non-zero status — or, when
# the tick is hard-killed, a missing `ended` entirely — marks a stuck tick.
tick_record_end() {
  local status="${1:-0}" f claimed_csv="" claimed_count=0
  f="${DISPATCH_CLAIMED_FILE:-$(_tick_claimed_file)}"
  if [[ -n "${f}" && -s "${f}" ]]; then
    claimed_count="$(grep -c . "${f}" 2>/dev/null)"; claimed_count="${claimed_count:-0}"
    claimed_csv="$(tr '\n' ',' <"${f}" 2>/dev/null | sed 's/,$//')"
  fi
  mkdir -p "$(dirname "${DISPATCH_RUN_RECORD}")" 2>/dev/null || true
  printf 'event=ended tick_id=%s ts=%s status=%s claimed_count=%s claimed=%s\n' \
    "${DISPATCH_TICK_ID:-tick-unknown}" "$(_iso8601)" "${status}" "${claimed_count}" "${claimed_csv}" \
    >>"${DISPATCH_RUN_RECORD}"
}

# ------------------------------ Run-ledger (E4 / #36) ----------------------
# ledger_emit STAGE [ISSUE] [FIELDS_JSON]: append one stage-transition line to
# the append-only JSONL run-ledger via services/ledger/ledger.py. STAGE is one
# of the canonical transition stages (claimed | work-order | engineer-dispatch |
# invoice | closure). FIELDS_JSON is an optional JSON object carrying
# label_before/label_after, explicit cost values, or an `invoice` path whose
# cost.* block populates the engineer-stage cost fields.
#
# Unlike run()/gh_mutate this is NOT gated by is_dry_run — the ledger is a local
# file write that always records (a dry-run tick still produced a transition); it
# stamps dry_run:true|false on each line, never calls `gh`, and never fails the
# tick (errors are swallowed so observability can't break the pipeline). The
# ledger path is DISPATCH_LEDGER_FILE (read by ledger.py from the environment),
# else ${DISPATCH_ARTIFACTS_DIR:-./.artifacts}/run-ledger.jsonl.
: "${LEDGER_PY:=${PIPELINE_ROOT}/services/ledger/ledger.py}"
ledger_emit() {
  local stage="$1" issue="${2:-}" fields="${3:-}" dry=true
  [[ -n "${fields}" ]] || fields='{}'
  is_dry_run || dry=false
  "${PYTHON_BIN}" "${LEDGER_PY}" --stage "${stage}" --issue "${issue}" \
    --tick-id "${DISPATCH_TICK_ID:-tick-unknown}" --dry-run "${dry}" \
    --fields "${fields}" --quiet >/dev/null 2>&1 || true
}

# ----------------------- Debug stage vocabulary (E2/#31) -------------------
# The canonical five tick stages, in execution order. `--until <stage>` (E2-2)
# halts the tick after the named stage; `--from <stage>` (E2-3) resumes from it.
# stage_ord echoes a stage's 1-based ordinal so call sites can compare positions
# without hard-coding numbers; it fails (non-zero, no output) on an unknown name.
DISPATCH_STAGES=(intake workorder engineer intake-invoice closure)
stage_ord() {
  local s="$1" i
  for i in "${!DISPATCH_STAGES[@]}"; do
    [[ "${DISPATCH_STAGES[$i]}" == "${s}" ]] && { echo $((i + 1)); return 0; }
  done
  return 1
}

# ------------------------- Engine call wrappers ----------------------------
# gh_repo_args: set the global REPO_ARGS array to `--repo owner/repo` (or empty)
# so call sites can splice it without word-splitting (security: no unquoted
# expansion of operator/attacker-influenced values).
gh_repo_args() {
  REPO_ARGS=()
  if [[ -n "${PIPELINE_REPO}" ]]; then
    REPO_ARGS=(--repo "${PIPELINE_REPO}")
  fi
}

# gh_mutate: a gh call that changes GitHub state -> always via run() (dry-run aware).
# Note: bash 3.2 (macOS default) raises nounset on "${arr[@]}" when arr is empty.
# The ${arr[@]+"${arr[@]}"} form is safe on bash 3.2+.
gh_mutate() {
  local REPO_ARGS=(); gh_repo_args
  run "${GH_BIN}" "$@" ${REPO_ARGS[@]+"${REPO_ARGS[@]}"}
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

# ------------------------- Engineer interface -------------------------------
# ENGINEER_BIN: the configured Engineer backend. Swapping this swaps the
# entire Engineer identity without changing any Architect code.
#   scripts/mock-engineer.sh → offline test stub (returns fixture invoices)
#   ruflo                    → production Ruflo CLI
: "${ENGINEER_BIN:=}"
export ENGINEER_BIN

# engineer_dispatch: submit a Job Request JSON to the Engineer.
#   $1 = Job Request JSON string (must satisfy schemas/job-request.json)
# Non-dry-run: calls ENGINEER_BIN and returns Invoice JSON on stdout.
# Dry-run:     prints the intended call; no Invoice is produced.
# The Architect is blind to which Engineer backs this call.
engineer_dispatch() {
  local job_request_json="$1"
  if is_dry_run; then
    # Surface the Job Request we *would* hand the Engineer. The backend binary
    # is a live-path requirement (like require_tool), so do not hard-fail when
    # ENGINEER_BIN is unset — just show the intended call (matches this
    # function's contract above).
    run "${ENGINEER_BIN:-<ENGINEER_BIN>}" "${job_request_json}"
    return 0
  fi
  [[ -n "${ENGINEER_BIN}" ]] || die "ENGINEER_BIN is not set; configure it in pipeline.env"
  run "${ENGINEER_BIN}" "${job_request_json}"
}
