#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# dispatch.sh — deterministic dispatch engine (HANDOFF §5.1)
#
# OWNS these label transitions:
#   queued -> claimed       (issue picked up for implementation)
#   queued -> needs-human   (low confidence or non-implement action)
#
# Flow per HANDOFF §5.1:
#   1. list `queued` issues (gh, or PIPELINE_FIXTURE_ISSUES in tests)
#   2. classify each via services/classifier/classify.py -> {action,scope,route,confidence}
#   3. confidence < THRESHOLD or action != implement  -> needs-human + rationale, skip
#   4. else, respecting concurrency (v0 = 1): post route JSON as a durable
#      comment, swap queued->claimed, create a worktree, and dispatch to
#      the configured Engineer (ENGINEER_BIN).
#
# Dry-run (default ON) prints every intended gh/git/claude call and mutates
# nothing (§7.5).
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

CLASSIFIER="${PIPELINE_ROOT}/services/classifier/classify.py"

# --- issue sources (live gh vs. fixture) -----------------------------------
load_queued_issues() {
  if [[ -n "${PIPELINE_FIXTURE_ISSUES}" ]]; then
    cat "${PIPELINE_FIXTURE_ISSUES}"
  else
    require_tool "${GH_BIN}"
    local REPO_ARGS=(); gh_repo_args
    "${GH_BIN}" issue list --label queued --state open \
      --json number,title,body,labels --limit 50 "${REPO_ARGS[@]}"
  fi
}

count_claimed() {
  if [[ -n "${PIPELINE_FIXTURE_ISSUES}" ]]; then
    jq '[.[] | select((.labels // []) | index("claimed"))] | length' \
      "${PIPELINE_FIXTURE_ISSUES}"
  else
    require_tool "${GH_BIN}"
    local REPO_ARGS=(); gh_repo_args
    "${GH_BIN}" issue list --label claimed --state open --json number \
      --limit 50 "${REPO_ARGS[@]}" | jq 'length'
  fi
}

# --- threshold comparison (float) ------------------------------------------
below_threshold() {  # below_threshold <confidence> <threshold> ; exit 0 if below
  awk -v c="$1" -v t="$2" 'BEGIN { exit (c + 0 < t + 0) ? 0 : 1 }'
}

# --- transitions -----------------------------------------------------------
to_needs_human() {
  local num="$1" reason="$2" action="${3:-}"
  # Surface the classifier's verdict as a secondary label where applicable
  # (so the `wont-do` / `duplicate` vocabulary is actually used).
  local extra=()
  case "${action}" in
    wont-do)      extra=(--add-label wont-do) ;;
    "duplicate?") extra=(--add-label duplicate) ;;
  esac
  log "#${num} -> needs-human: ${reason}"
  gh_mutate issue edit "${num}" --remove-label queued --add-label needs-human ${extra[@]+"${extra[@]}"}
  gh_mutate issue comment "${num}" \
    --body "Pipeline dispatch routed this to **needs-human**. Rationale: ${reason}"
}

claim_issue() {
  local num="$1" route="$2" result_json="$3"
  log "#${num} -> claimed (route=${route})"
  # Heartbeat seam (E1-2): record which issue this tick claimed so the
  # pipeline's tick_record_end can report it without re-querying GitHub. No-op
  # when not run under pipeline.sh (DISPATCH_CLAIMED_FILE unset).
  tick_record_claim "${num}"
  # Durable routing decision (machine-findable marker) as an issue comment.
  gh_mutate issue comment "${num}" \
    --body "<!-- pipeline:route --> Routing decision: ${result_json}"
  gh_mutate issue edit "${num}" --remove-label queued --add-label claimed
  # Worktree isolation for the worker session (§5.4 step 3).
  run git -C "${PIPELINE_ROOT}" worktree add \
    "${PIPELINE_WORKTREE_ROOT}/issue-${num}" -b "pipeline/issue-${num}"
  # Submit a Job Request to the Engineer (identity governed by ENGINEER_BIN).
  local args; args="$(jq -nc \
    --argjson issue  "${num}"        \
    --arg     repo   "${PIPELINE_REPO:-}" \
    --arg     title  "${title}"      \
    --arg     body   "${body}"       \
    --arg     route  "${route}"      \
    --arg     scope  "${scope}"      \
    --argjson conf   "${confidence}" \
    '{job_id: ("issue-\($issue)"),
      issue: $issue, repo: $repo, title: $title, body: $body,
      route: $route, scope: $scope, confidence: $conf}')"
  engineer_dispatch "${args}"
}

main() {
  local issues claimed concurrency
  issues="$(load_queued_issues)"
  claimed="$(count_claimed)"
  concurrency="${PIPELINE_CONCURRENCY}"
  local n; n="$(jq 'length' <<<"${issues}")"
  log "dispatch: ${n} queued issue(s); ${claimed} already claimed; concurrency=${concurrency}; dry_run=${PIPELINE_DRY_RUN}"

  [[ "${n}" -gt 0 ]] || { log "nothing queued; exiting 0."; return 0; }

  local i issue num title body result action scope route confidence
  for ((i = 0; i < n; i++)); do
    issue="$(jq -c ".[$i]" <<<"${issues}")"
    num="$(jq -r '.number' <<<"${issue}")"
    title="$(jq -r '.title // ""' <<<"${issue}")"
    body="$(jq -r '.body // ""' <<<"${issue}")"

    # Classify (quarantine reader; offline in tests, HF live in prod).
    if ! result="$("${PYTHON_BIN}" "${CLASSIFIER}" --title "${title}" --body "${body}")"; then
      die "classifier failed for #${num} (see stderr)"
    fi
    action="$(jq -r '.action' <<<"${result}")"
    scope="$(jq -r '.scope' <<<"${result}")"
    route="$(jq -r '.route' <<<"${result}")"
    confidence="$(jq -r '.confidence' <<<"${result}")"
    log "#${num} classified: action=${action} scope=${scope} route=${route} confidence=${confidence}"

    if [[ "${action}" != "implement" ]]; then
      to_needs_human "${num}" "classifier action='${action}' (not implementable autonomously)" "${action}"
      continue
    fi
    if below_threshold "${confidence}" "${PIPELINE_CONFIDENCE_THRESHOLD}"; then
      to_needs_human "${num}" "confidence ${confidence} < threshold ${PIPELINE_CONFIDENCE_THRESHOLD}"
      continue
    fi
    if [[ "${claimed}" -ge "${concurrency}" ]]; then
      log "#${num} eligible (route=${route}) but concurrency ${concurrency} reached; leaving queued."
      continue
    fi
    claim_issue "${num}" "${route}" "${result}"
    claimed=$((claimed + 1))
  done
  log "dispatch: complete."
}

main "$@"
