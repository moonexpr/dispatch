#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# roundtrip.sh — fully mocked Architect → Engineer → Architect loop.
#
# Acceptance: Architect selects 1-3 issues, submits a Job Request to the
# Engineer for each, and processes the returned Invoices.
#
# Decoupling: Architect and Engineer are coupled ONLY through the two JSON
# schemas (schemas/job-request.json, schemas/invoice.json). To swap:
#   Engineer: set ENGINEER_BIN=<any binary that reads a Job Request and
#             writes an Invoice> (default: scripts/mock-engineer.sh)
#   Architect: replace this script with any process that classifies issues,
#              calls engineer_dispatch, and passes Invoices to architect-intake.
#
# Environment knobs:
#   ENGINEER_BIN             path to Engineer binary (default: mock-engineer.sh)
#   PIPELINE_CONCURRENCY     max issues to dispatch (1-3; default 3)
#   PIPELINE_FIXTURE_ISSUES  issue source JSON (default: fixtures/queued-issues.json)
#   CLASSIFIER_OFFLINE       1 = deterministic offline classifier (default 1 here)
#   PIPELINE_DRY_RUN         1 = print gh/claude calls, mutate nothing (default 1)
#   MOCK_INVOICE_STATUS      force a specific Invoice status (mock stub only)
#
# Usage:
#   scripts/roundtrip.sh
#   PIPELINE_CONCURRENCY=1 scripts/roundtrip.sh
#   ENGINEER_BIN=ruflo PIPELINE_DRY_RUN=0 scripts/roundtrip.sh
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# Roundtrip defaults — set AFTER source so pipeline.env doesn't clobber them.
# Use ROUNDTRIP_CONCURRENCY (not PIPELINE_CONCURRENCY) to avoid pipeline.env override.
: "${ENGINEER_BIN:=${SCRIPT_DIR}/mock-engineer.sh}"
: "${PIPELINE_FIXTURE_ISSUES:=${SCRIPT_DIR}/fixtures/queued-issues.json}"
CLASSIFIER_OFFLINE=1
PIPELINE_CONCURRENCY="${ROUNDTRIP_CONCURRENCY:-3}"
export ENGINEER_BIN CLASSIFIER_OFFLINE PIPELINE_FIXTURE_ISSUES PIPELINE_CONCURRENCY

CLASSIFIER="${PIPELINE_ROOT}/services/classifier/classify.py"
INTAKE="${SCRIPT_DIR}/architect-intake.sh"
BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; DIM=$'\033[2m'; NC=$'\033[0m'

banner()  { printf '\n%s━━ %s ━━%s\n' "${BOLD}" "$*" "${NC}"; }
ok()      { printf '  %s✔%s  %s\n' "${GREEN}" "${NC}" "$*"; }
warn()    { printf '  %s⚠%s  %s\n' "${YEL}"   "${NC}" "$*"; }
fail()    { printf '  %s✘%s  %s\n' "${RED}"    "${NC}" "$*"; }
detail()  { printf '     %s%s%s\n' "${DIM}"    "$*"   "${NC}"; }

# ---------------------------------------------------------------------------
banner "Architect: loading issues from ${PIPELINE_FIXTURE_ISSUES}"
issues="$(cat "${PIPELINE_FIXTURE_ISSUES}")"
total="$(jq 'length' <<<"${issues}")"
log "roundtrip: ${total} issue(s) in queue; concurrency=${PIPELINE_CONCURRENCY}; dry_run=${PIPELINE_DRY_RUN}"

# ---------------------------------------------------------------------------
banner "Architect: classify and select (offline, up to ${PIPELINE_CONCURRENCY})"

selected=()   # issue JSON objects that passed the threshold
skipped=()    # human-readable skip reasons

for ((i = 0; i < total; i++)); do
  issue="$(jq -c ".[$i]" <<<"${issues}")"
  num="$(   jq -r '.number'    <<<"${issue}")"
  title="$( jq -r '.title'     <<<"${issue}")"
  body="$(  jq -r '.body // ""' <<<"${issue}")"

  if ! result="$("${PYTHON_BIN}" "${CLASSIFIER}" --title "${title}" --body "${body}" 2>/dev/null)"; then
    warn "#${num}: classifier error — skipping"
    skipped+=("#${num}: classifier error")
    continue
  fi

  action="$(     jq -r '.action'     <<<"${result}")"
  scope="$(      jq -r '.scope'      <<<"${result}")"
  route="$(      jq -r '.route'      <<<"${result}")"
  confidence="$( jq -r '.confidence' <<<"${result}")"

  printf '  #%-4s  %-52s  action=%-12s scope=%-3s route=%-12s conf=%.2f\n' \
    "${num}" "${title:0:52}" "${action}" "${scope}" "${route}" "${confidence}"

  if [[ "${action}" != "implement" ]]; then
    warn "#${num}: action='${action}' — not dispatchable; routing to needs-human"
    skipped+=("#${num}: wont-do/duplicate")
    continue
  fi

  if awk -v c="${confidence}" -v t="${PIPELINE_CONFIDENCE_THRESHOLD}" \
       'BEGIN { exit (c + 0 < t + 0) ? 0 : 1 }'; then
    warn "#${num}: confidence ${confidence} < ${PIPELINE_CONFIDENCE_THRESHOLD} — needs-human"
    skipped+=("#${num}: low confidence (${confidence})")
    continue
  fi

  if [[ "${#selected[@]}" -ge "${PIPELINE_CONCURRENCY}" ]]; then
    detail "#${num}: concurrency limit reached; leaving queued"
    continue
  fi

  job_request="$(jq -nc \
    --argjson issue  "${num}"         \
    --arg     repo   "${PIPELINE_REPO:-ReclaimByDesign/dispatch}" \
    --arg     title  "${title}"       \
    --arg     body   "${body}"        \
    --arg     route  "${route}"       \
    --arg     scope  "${scope}"       \
    --argjson conf   "${confidence}"  \
    '{job_id:     ("issue-" + ($issue|tostring)),
      issue:      $issue,
      repo:       $repo,
      title:      $title,
      body:       $body,
      route:      $route,
      scope:      $scope,
      confidence: $conf}')"

  selected+=("${job_request}")
  ok "#${num}: selected → ${route}"
done

# ---------------------------------------------------------------------------
banner "Architect: ${#selected[@]} issue(s) selected for dispatch"

if [[ "${#selected[@]}" -eq 0 ]]; then
  warn "No issues dispatched."
  [[ "${#skipped[@]}" -gt 0 ]] && detail "Skipped: ${skipped[*]}"
  exit 0
fi

pass_count=0; fail_count=0

for job_request in "${selected[@]}"; do
  num="$(    jq -r '.issue' <<<"${job_request}")"
  route="$(  jq -r '.route' <<<"${job_request}")"
  title="$(  jq -r '.title' <<<"${job_request}")"

  banner "Issue #${num} → Engineer"
  detail "title:  ${title}"
  detail "route:  ${route}"
  detail "bin:    ${ENGINEER_BIN}"

  # ── Submit Job Request to Engineer ──────────────────────────────────────
  # The Engineer call is always real (mock-engineer.sh is local, no GitHub
  # side effects). architect-intake.sh respects PIPELINE_DRY_RUN for gh calls.
  if ! invoice="$("${ENGINEER_BIN}" "${job_request}" 2>/dev/null)"; then
    fail "#${num}: Engineer returned non-zero exit"
    fail_count=$((fail_count + 1))
    continue
  fi

  invoice_status="$(jq -r '.status // "unknown"' <<<"${invoice}")"
  ok "#${num}: Invoice received — status=${invoice_status}"
  detail "$(jq -r '.summary | split(".")[0]' <<<"${invoice}")"

  # ── Pass Invoice to Architect intake ─────────────────────────────────────
  banner "Issue #${num} ← Invoice intake (status=${invoice_status})"
  if bash "${INTAKE}" "${invoice}"; then
    ok "#${num}: intake complete"
    pass_count=$((pass_count + 1))
  else
    fail "#${num}: intake failed"
    fail_count=$((fail_count + 1))
  fi
done

# ---------------------------------------------------------------------------
banner "Roundtrip summary"
printf '  Selected:  %d / %d issues\n' "${#selected[@]}" "${total}"
printf '  Handled:   %s%d pass%s  %s%d fail%s\n' \
  "${GREEN}" "${pass_count}" "${NC}" "${RED}" "${fail_count}" "${NC}"
[[ "${#skipped[@]}" -gt 0 ]] && printf '  Skipped:   %s\n' "${skipped[*]}"
printf '  Engineer:  %s\n' "${ENGINEER_BIN}"
printf '  Dry-run:   %s\n' "${PIPELINE_DRY_RUN}"

[[ "${fail_count}" -eq 0 ]]
