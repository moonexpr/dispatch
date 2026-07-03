#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# debug-classify.sh — dump live GitHub issue JSON and run the triage classifier
#
# A read-only debug helper for inspecting how src/classifier/classify.py
# triages real issues. For each issue number it:
#   1. fetches the issue via `gh issue view N --json number,title,body,labels`
#      (against PIPELINE_REPO / --repo, exactly like dispatch.sh),
#   2. prints the fetched JSON normalised to the classifier-fixture shape
#      ({number,title,body,labels:[string]}), and
#   3. runs classify.py on (title, body) and prints the TriageResult, plus the
#      dispatch routing that result would trigger (dispatch.sh §5.1 rules).
#
# This script NEVER mutates GitHub state — no labels, no comments. It only
# reads. Use it to reproduce / debug a dispatch classification offline.
#
# Usage:
#   scripts/debug-classify.sh [--repo owner/repo] [--save-fixtures DIR] [N ...]
#
#   N ...              issue numbers to inspect (default: 1 2 3)
#   --repo owner/repo  target repo (default: $PIPELINE_REPO, else gh default)
#   --save-fixtures D   also write each dump to D/issue_<N>.json (classifier
#                       fixture shape, usable with classify.py --issue-json)
#
# Examples:
#   PIPELINE_REPO=ReclaimByDesign/dispatch-testrepo-a scripts/debug-classify.sh
#   scripts/debug-classify.sh --repo ReclaimByDesign/dispatch-testrepo-a 1 2 3
#   scripts/debug-classify.sh --save-fixtures /tmp/dump 1 2 3
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

CLASSIFIER="${PIPELINE_ROOT}/src/classifier/classify.py"
THRESHOLD="${PIPELINE_CONFIDENCE_THRESHOLD}"

SAVE_DIR=""
ISSUES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)          PIPELINE_REPO="$2"; shift 2 ;;
    --repo=*)        PIPELINE_REPO="${1#*=}"; shift ;;
    --save-fixtures) SAVE_DIR="$2"; shift 2 ;;
    --save-fixtures=*) SAVE_DIR="${1#*=}"; shift ;;
    -h|--help)       sed -n '2,33p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --) shift; while [[ $# -gt 0 ]]; do ISSUES+=("$1"); shift; done ;;
    -*) die "unknown flag: $1" ;;
    *)  ISSUES+=("$1"); shift ;;
  esac
done
[[ ${#ISSUES[@]} -gt 0 ]] || ISSUES=(1 2 3)

require_tool "${GH_BIN}"
require_tool "${PYTHON_BIN}"
[[ -n "${SAVE_DIR}" ]] && mkdir -p "${SAVE_DIR}"

# What dispatch.sh would do with this classification (mirrors §5.1 routing).
routing_verdict() {  # action confidence
  local action="$1" confidence="$2"
  if [[ "${action}" == "decompose" ]]; then
    echo "-> wontdo_parent_issue (epic/container; links child issues, not escalated)"
  elif [[ "${action}" != "implement" ]]; then
    echo "-> needs-human (action='${action}' not implementable autonomously)"
  elif awk -v c="${confidence}" -v t="${THRESHOLD}" 'BEGIN { exit (c+0 < t+0) ? 0 : 1 }'; then
    echo "-> needs-human (confidence ${confidence} < threshold ${THRESHOLD})"
  else
    echo "-> claimed (route eligible, confidence ${confidence} >= ${THRESHOLD})"
  fi
}

log "repo=${PIPELINE_REPO:-<gh default>}  threshold=${THRESHOLD}  issues=${ISSUES[*]}"

for num in "${ISSUES[@]}"; do
  echo
  echo "======================================================================"
  echo "ISSUE #${num}"
  echo "======================================================================"

  REPO_ARGS=(); gh_repo_args
  raw="$("${GH_BIN}" issue view "${num}" --json number,title,body,labels \
        ${REPO_ARGS[@]+"${REPO_ARGS[@]}"} 2>/dev/null)" || {
    log "#${num}: gh issue view failed (missing issue / no access / bad repo)"
    continue
  }

  # Normalise to the classifier-fixture shape: labels as plain strings
  # (gh emits {name} objects; fixtures use strings), matching dispatch.sh.
  dump="$(jq '{number, title, body,
               labels: [.labels[]? | if type=="object" then .name else . end]}' \
          <<<"${raw}")"

  echo "--- issue JSON (classifier-fixture shape) ---"
  echo "${dump}"

  if [[ -n "${SAVE_DIR}" ]]; then
    echo "${dump}" > "${SAVE_DIR}/issue_${num}.json"
    log "#${num}: saved -> ${SAVE_DIR}/issue_${num}.json"
  fi

  title="$(jq -r '.title // ""' <<<"${dump}")"
  body="$(jq -r '.body // ""'  <<<"${dump}")"

  if ! result="$("${PYTHON_BIN}" "${CLASSIFIER}" --title "${title}" --body "${body}")"; then
    log "#${num}: classifier failed (see stderr)"
    continue
  fi

  echo "--- classifier result ---"
  echo "${result}"

  action="$(jq -r '.action' <<<"${result}")"
  confidence="$(jq -r '.confidence' <<<"${result}")"
  echo "--- dispatch routing ---"
  routing_verdict "${action}" "${confidence}"
done

echo
log "done (read-only; no GitHub state changed)."
