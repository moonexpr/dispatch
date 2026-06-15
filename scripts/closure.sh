#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# closure.sh — success path (HANDOFF §5.8)
#
# Two phases, selected by the payload:
#
#   Phase A (success payload: CI green + review approved, .merged != true):
#     1. flip in-review -> done-pending-merge
#     2. gh pr merge --auto --squash   (branch protection holds the merge for
#        the operator's approval tap — no custom gating code; §5.8)
#     3. post a closure summary comment
#
#   Phase B (merged event: .merged == true):
#     - post a summary comment on the (auto-closed) issue
#     - flip the issue to done
#
# OWNS: in-review -> done-pending-merge -> done.
# NEVER bypasses branch protection. Dry-run aware (default ON); §7.8.
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

load_payload() {
  if [[ -n "${PIPELINE_FIXTURE_PR}" ]]; then
    cat "${PIPELINE_FIXTURE_PR}"
  elif [[ "${1:-}" && -f "${1:-}" ]]; then
    cat "$1"
  else
    cat -
  fi
}

phase_success() {
  local pr="$1" issue="$2" conclusion="$3" review="$4"
  if [[ "${conclusion}" != "success" || "${review}" != "approved" ]]; then
    log "closure: PR #${pr} not ready (conclusion=${conclusion}, review=${review}); no-op exit 0."
    return 0
  fi
  log "closure(success): PR #${pr} (Closes #${issue}) -> auto-merge."
  gh_mutate pr edit "${pr}" --remove-label in-review --add-label done-pending-merge

  # Arm auto-merge (squash). Branch protection still requires the human approval tap.
  gh_mutate pr merge "${pr}" --auto --squash

  # Closure summary comment.
  gh_mutate pr comment "${pr}" \
    --body "Pipeline closure summary: CI green, auto-merge armed (squash). Awaiting the required human approval; merging will close #${issue}."

  log "closure(success): PR #${pr} armed; waiting on human approval tap."
}

phase_merged() {
  local pr="$1" issue="$2"
  log "closure(merged): PR #${pr} merged; finalizing issue #${issue}."
  gh_mutate issue comment "${issue}" \
    --body "Pipeline summary: PR #${pr} merged and this issue auto-closed. Closing out as done."
  gh_mutate issue edit "${issue}" --add-label "done" --remove-label done-pending-merge
  log "closure(merged): issue #${issue} is DONE."
}

main() {
  local payload pr issue conclusion review merged
  payload="$(load_payload "${1:-}")"
  pr="$(jq -r '.pr // empty' <<<"${payload}")"
  issue="$(jq -r '.issue // empty' <<<"${payload}")"
  conclusion="$(jq -r '.conclusion // "failure"' <<<"${payload}")"
  review="$(jq -r '.review_state // "none"' <<<"${payload}")"
  merged="$(jq -r '.merged // false' <<<"${payload}")"
  [[ -n "${pr}" ]] || die "payload has no .pr"
  [[ -n "${issue}" ]] || die "payload has no .issue"

  if [[ "${merged}" == "true" ]]; then
    phase_merged "${pr}" "${issue}"
  else
    phase_success "${pr}" "${issue}" "${conclusion}" "${review}"
  fi
}

main "$@"
