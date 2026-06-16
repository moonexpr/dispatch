#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# gh-intake.sh — fetch issues from a GitHub ProjectV2 or repo and emit a
# normalized JSON array to stdout for downstream pipeline consumption.
#
# Modes:
#   --project ORG/NUM   query a GitHub ProjectV2 board (all items or filtered)
#   --repo OWNER/REPO   query a GitHub repo for open issues by label
#
# Env-var equivalents (pipeline.env):
#   INTAKE_PROJECT=ORG/NUM         project shorthand  (e.g. ReclaimByDesign/5)
#   INTAKE_REPO=OWNER/REPO         repo shorthand (falls back to PIPELINE_REPO)
#   INTAKE_STATUS_FILTER=<str>     project Status to include (regex, case-insensitive)
#   INTAKE_LABEL_FILTER=<label>    repo label to filter on
#   INTAKE_LIMIT=<n>               max items to fetch (default: 50)
#   INTAKE_FIXTURE_PROJECT=<path>  offline: path to raw gh project item-list JSON
#   INTAKE_FIXTURE_REPO=<path>     offline: path to raw gh issue list JSON
#
# Output schema (stdout, JSON array):
#   number          int      issue number within its repo
#   title           string
#   body            string
#   labels          string[]
#   repository      string   "owner/repo" slug
#   assignees       string[]
#   project_status  string   project Status field value (null in repo mode)
#   dispatch        string   project Dispatch field value (null if absent)
#   hours_estimate  number   project Hours Estimate field value (null if absent)
#   source_url      string   full GitHub URL to the issue
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# ----------------------------- Defaults ------------------------------------
: "${INTAKE_PROJECT:=}"
: "${INTAKE_REPO:=}"
: "${INTAKE_STATUS_FILTER:=}"
: "${INTAKE_LABEL_FILTER:=}"
: "${INTAKE_LIMIT:=50}"
: "${INTAKE_FIXTURE_PROJECT:=}"
: "${INTAKE_FIXTURE_REPO:=}"

# ----------------------------- CLI parsing ---------------------------------
mode=""
project_arg=""
repo_arg=""
output_file=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)  mode="project"; project_arg="$2"; shift 2 ;;
    --repo)     mode="repo";    repo_arg="$2";    shift 2 ;;
    --status)   INTAKE_STATUS_FILTER="$2";        shift 2 ;;
    --label)    INTAKE_LABEL_FILTER="$2";         shift 2 ;;
    --limit)    INTAKE_LIMIT="$2";                shift 2 ;;
    --output)   output_file="$2";                 shift 2 ;;
    --help|-h)
      grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//' | head -30
      exit 0
      ;;
    *) die "gh-intake: unknown flag: $1" ;;
  esac
done

# Resolve mode from env if not set via CLI.
if [[ -z "${mode}" ]]; then
  if [[ -n "${INTAKE_PROJECT}" ]]; then
    mode="project"; project_arg="${INTAKE_PROJECT}"
  elif [[ -n "${INTAKE_REPO:-${PIPELINE_REPO}}" ]]; then
    mode="repo"; repo_arg="${INTAKE_REPO:-${PIPELINE_REPO}}"
  else
    die "gh-intake: specify --project ORG/NUM or --repo OWNER/REPO (or set INTAKE_PROJECT / INTAKE_REPO)"
  fi
fi

# ----------------------------- Project mode --------------------------------
intake_from_project() {
  local org num raw
  org="${project_arg%%/*}"
  num="${project_arg##*/}"
  [[ -n "${org}" && "${org}" != "${num}" ]] || die "gh-intake: --project must be ORG/NUM (got '${project_arg}')"

  log "gh-intake: project ${org}/${num} status_filter='${INTAKE_STATUS_FILTER}' limit=${INTAKE_LIMIT}"

  if [[ -n "${INTAKE_FIXTURE_PROJECT}" ]]; then
    raw="$(cat "${INTAKE_FIXTURE_PROJECT}")"
  else
    require_tool "${GH_BIN}"
    raw="$("${GH_BIN}" project item-list "${num}" \
      --owner "${org}" \
      --format json \
      --limit "${INTAKE_LIMIT}")"
  fi

  local status_filter="${INTAKE_STATUS_FILTER}"
  jq -c \
    --arg sf "${status_filter}" '
    .items
    | if $sf != "" then
        map(select((.status // "") | test($sf; "i")))
      else . end
    | map(select(.content.type == "Issue"))
    | map({
        number:         (.content.number // 0),
        title:          (.content.title  // .title // ""),
        body:           (.content.body   // ""),
        labels:         [],
        repository:     (.content.repository // ""),
        assignees:      (.assignees // []),
        project_status: (.status // null),
        dispatch:       (.dispatch // null),
        hours_estimate: (."hours Estimate" // null),
        source_url:     (.content.url // "")
      })
  ' <<<"${raw}"
}

# ----------------------------- Repo mode -----------------------------------
intake_from_repo() {
  local repo="${repo_arg}" raw
  local label_args=()
  [[ -n "${INTAKE_LABEL_FILTER}" ]] && label_args=(--label "${INTAKE_LABEL_FILTER}")

  log "gh-intake: repo ${repo} label_filter='${INTAKE_LABEL_FILTER}' limit=${INTAKE_LIMIT}"

  if [[ -n "${INTAKE_FIXTURE_REPO}" ]]; then
    raw="$(cat "${INTAKE_FIXTURE_REPO}")"
  else
    require_tool "${GH_BIN}"
    local REPO_ARGS=()
    [[ -n "${repo}" ]] && REPO_ARGS=(--repo "${repo}")
    raw="$("${GH_BIN}" issue list \
      --state open \
      --json number,title,body,labels,assignees,url \
      --limit "${INTAKE_LIMIT}" \
      "${REPO_ARGS[@]}" \
      ${label_args[@]+"${label_args[@]}"})"
  fi

  local repo_slug="${repo}"
  jq -c \
    --arg repo "${repo_slug}" '
    map({
      number:         .number,
      title:          .title,
      body:           (.body // ""),
      labels:         [.labels[].name],
      repository:     $repo,
      assignees:      [.assignees[].login],
      project_status: null,
      dispatch:       null,
      hours_estimate: null,
      source_url:     (.url // "")
    })
  ' <<<"${raw}"
}

# ----------------------------- Main ----------------------------------------
main() {
  local result
  case "${mode}" in
    project) result="$(intake_from_project)" ;;
    repo)    result="$(intake_from_repo)"    ;;
    *)       die "gh-intake: unknown mode '${mode}'" ;;
  esac

  local count
  count="$(jq 'length' <<<"${result}")"

  if [[ -n "${output_file}" ]]; then
    echo "${result}" > "${output_file}"
    log "gh-intake: wrote ${count} item(s) to ${output_file}"
  else
    echo "${result}"
    log "gh-intake: emitted ${count} item(s)"
  fi
}

main "$@"
