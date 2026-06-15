#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# architect-intake.sh — process an Invoice returned by the Engineer.
#
# This script is the Architect's only Invoice handler. It is decoupled from
# the Engineer identity: any process that returns a valid Invoice JSON
# (schemas/invoice.json) can trigger this handler.
#
# Input:  Invoice JSON as $1 (or on stdin)
# Output: dry-run-aware gh/claude calls (same as other pipeline scripts)
# Exit:   0 on all handled paths (including escalation); non-zero on bad input
#
# Status → action mapping:
#   completed   → post invoice summary + arm auto-merge on the PR
#   partial     → same as failed (work done but CI still red)
#   failed      → add fix-attempt-1 label; fix-dispatch.sh handles progression
#   needs-human → add needs-human label + post issue comment
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# Accept Invoice from first arg or stdin.
if [[ $# -ge 1 && -n "$1" ]]; then
  invoice_json="$1"
else
  invoice_json="$(cat)"
fi

# Parse required fields.
status="$(    jq -r '.status     // empty' <<<"${invoice_json}")"
issue="$(     jq -r '.issue      // empty' <<<"${invoice_json}")"
pr_number="$( jq -r '.pr_number // empty' <<<"${invoice_json}")"
summary="$(   jq -r '.summary   // ""'    <<<"${invoice_json}")"
route_used="$(jq -r '.route_used // "gen-local"' <<<"${invoice_json}")"

if [[ -z "${status}" || -z "${issue}" ]]; then
  echo "architect-intake: Invoice missing required fields (status, issue)" >&2
  exit 1
fi

log "architect-intake: issue=#${issue} status=${status} pr=${pr_number:-none}"

case "${status}" in

  completed)
    log "#${issue}: completed — arming auto-merge"
    if [[ -n "${pr_number}" ]]; then
      gh_mutate pr comment "${pr_number}" \
        --body "**Engineer invoice:** ${summary}"
      # Arm auto-merge; branch protection still requires the human approval tap.
      gh_mutate pr merge "${pr_number}" --auto --squash \
        --subject "Closes #${issue}"
    fi
    gh_mutate issue edit "${issue}" \
      --remove-label claimed --add-label done-pending-merge
    ;;

  partial|failed)
    log "#${issue}: ${status} — labeling fix-attempt-1; fix-dispatch.sh handles progression"
    gh_mutate issue edit "${issue}" --add-label fix-attempt-1
    if [[ -n "${pr_number}" ]]; then
      gh_mutate pr comment "${pr_number}" \
        --body "**Engineer invoice (${status}):** ${summary}"
    fi
    ;;

  needs-human)
    log "#${issue}: needs-human — escalating to operator"
    gh_mutate issue edit "${issue}" \
      --remove-label claimed --add-label needs-human
    gh_mutate issue comment "${issue}" \
      --body "**Engineer returned needs-human.** ${summary}"
    ;;

  *)
    echo "architect-intake: unknown Invoice status '${status}'" >&2
    exit 1
    ;;
esac

log "architect-intake: done (issue=#${issue} status=${status})"
