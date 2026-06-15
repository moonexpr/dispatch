#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# fix-dispatch.sh — CI-failure fix ladder (HANDOFF §5.5)
#
# Triggered when CI fails on a pipeline PR. The retry counter lives in
# GitHub labels. Ladder (by the attempt about to run):
#     1 -> gen-local   2 -> gen-default   3 -> gen-frontier   >3 -> needs-human
#
# OWNS these label transitions:
#   fix-attempt-(N-1) -> fix-attempt-N   (increment on each new failure, N<=3)
#   fix-attempt-3     -> needs-human     (retry cap exceeded; escalate)
#
# Attempt resolution (first match wins):
#   1. PIPELINE_FIX_ATTEMPT env (explicit override)
#   2. payload .attempt field (webhook-supplied counter)
#   3. (max fix-attempt-N label present) + 1
#   4. default 1
#
# Dry-run (default) prints intended gh/claude calls; mutates nothing.
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# load_payload: PIPELINE_FIXTURE_PR > $1 (file) > stdin.
load_payload() {
  if [[ -n "${PIPELINE_FIXTURE_PR}" ]]; then
    cat "${PIPELINE_FIXTURE_PR}"
  elif [[ "${1:-}" && -f "${1:-}" ]]; then
    cat "$1"
  else
    cat -
  fi
}

pr_labels() {  # emit label names (handles string or {name} objects)
  jq -r '(.labels // []) | map(if type=="object" then .name else . end) | .[]' <<<"$1"
}

max_fix_attempt() {
  local payload="$1" m=0 n
  while IFS= read -r lbl; do
    case "${lbl}" in
      fix-attempt-*)
        n="${lbl#fix-attempt-}"
        [[ "${n}" =~ ^[0-9]+$ ]] && (( n > m )) && m="${n}" ;;
    esac
  done < <(pr_labels "${payload}")
  echo "${m}"
}

resolve_attempt() {
  local payload="$1" pa
  if [[ -n "${PIPELINE_FIX_ATTEMPT}" ]]; then echo "${PIPELINE_FIX_ATTEMPT}"; return; fi
  pa="$(jq -r '.attempt // empty' <<<"${payload}")"
  if [[ -n "${pa}" && "${pa}" != "null" ]]; then echo "${pa}"; return; fi
  echo "$(( $(max_fix_attempt "${payload}") + 1 ))"
}

main() {
  local payload pr attempt tier brief conclusion
  payload="$(load_payload "${1:-}")"
  pr="$(jq -r '.pr // .pull_request // empty' <<<"${payload}")"
  [[ -n "${pr}" ]] || die "payload has no .pr"
  conclusion="$(jq -r '.conclusion // "failure"' <<<"${payload}")"
  brief="$(jq -r '.brief // "(see CI logs)"' <<<"${payload}")"
  attempt="$(resolve_attempt "${payload}")"
  tier="$(tier_for_attempt "${attempt}")"

  log "fix-dispatch: PR #${pr} conclusion=${conclusion} attempt=${attempt} -> tier=${tier} (dry_run=${PIPELINE_DRY_RUN})"

  if [[ "${conclusion}" == "success" ]]; then
    log "PR #${pr} CI is green; fix-dispatch is a no-op (closure handles success). Exiting 0."
    return 0
  fi

  if [[ "${tier}" == "needs-human" ]]; then
    # Retry cap exceeded (>3): escalate, label the PR, post a comment for the operator.
    log "PR #${pr}: attempt ${attempt} exceeds cap (3) -> escalating to operator."
    gh_mutate pr edit "${pr}" --add-label needs-human
    gh_mutate pr comment "${pr}" \
      --body "Pipeline: fix-attempt cap exceeded (attempt ${attempt} > 3). Brief: ${brief}. Needs human."
    log "operator-notification path taken for PR #${pr}."
    return 0
  fi

  # Increment the retry counter label (prev -> current).
  local prev=$(( attempt - 1 ))
  if (( prev >= 1 )); then
    gh_mutate pr edit "${pr}" --remove-label "fix-attempt-${prev}" --add-label "fix-attempt-${attempt}"
  else
    gh_mutate pr edit "${pr}" --add-label "fix-attempt-${attempt}"
  fi
  log "PR #${pr}: labeled fix-attempt-${attempt} (tier ${tier}); engineer handles CI fix."
}

main "$@"
