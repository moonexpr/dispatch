#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# clone-to-testrepo.sh — provision a disposable pipeline TEST TARGET by mirroring
# the demo repo (code + issues + labels) into a fresh repo.
#
#   Usage: [PIPELINE_DRY_RUN=0] scripts/demo/clone-to-testrepo.sh [DEST] [SRC]
#     DEST  destination repo owner/name (default: ReclaimByDesign/dispatch-testrepo-a)
#     SRC   source repo owner/name       (default: ReclaimByDesign/demo-repository)
#
# Faithful mirror, NO pipeline pre-queuing: issues are copied with their ORIGINAL
# labels (Candidate / epic / task) and NONE are labelled `queued`. The dispatch
# pipeline's own intake/queueing stage is therefore exercised by the test run.
# The pipeline state labels (queued/claimed/...) are still *provisioned* on DEST
# (via bootstrap-labels.sh) so the pipeline can apply them itself.
#
# Steps (all mutations gated by PIPELINE_DRY_RUN — default 1 = preview only):
#   1. create DEST (private) if absent
#   2. mirror SRC default-branch code → DEST
#   3. clone SRC issue labels → DEST
#   4. copy SRC open issues → DEST in ascending order (numbers line up 1:1, so
#      epic `- [ ] #N` checklists stay valid), preserving title/body/labels
#   5. bootstrap the dispatch pipeline label vocabulary on DEST
#
# Set PIPELINE_DRY_RUN=0 to actually create the repo, push code, and open issues.
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../lib/common.sh
source "${SCRIPT_DIR}/../lib/common.sh"

DEST="${1:-ReclaimByDesign/dispatch-testrepo-a}"
SRC="${2:-ReclaimByDesign/demo-repository}"

main() {
  require_tool "${GH_BIN}"; require_tool git; require_tool jq

  log "clone-to-testrepo: SRC=${SRC} → DEST=${DEST} (dry_run=${PIPELINE_DRY_RUN})"

  # 1) Create DEST if it doesn't already exist.
  if "${GH_BIN}" repo view "${DEST}" >/dev/null 2>&1; then
    log "DEST ${DEST} already exists — skipping create"
  else
    gh_mutate repo create "${DEST}" --private \
      --description "Disposable test target for the dispatch 16-tick run; faithful mirror of ${SRC} (code + issues)."
  fi

  # 2) Mirror code (default branch) from SRC → DEST.
  local tmp src_dir def
  tmp="$(mktemp -d)"; src_dir="${tmp}/src"
  log "cloning ${SRC} code → ${src_dir}"
  if is_dry_run; then
    echo "DRY-RUN: git clone git@github.com:${SRC}.git ${src_dir} && push default branch → ${DEST}"
  else
    git clone "git@github.com:${SRC}.git" "${src_dir}" >/dev/null 2>&1
    def="$(git -C "${src_dir}" rev-parse --abbrev-ref HEAD)"
    git -C "${src_dir}" push "git@github.com:${DEST}.git" "${def}:${def}" >/dev/null 2>&1 \
      || warn "code push to ${DEST} reported nothing to push (source may be code-empty)"
  fi
  rm -rf "${tmp}" 2>/dev/null || true

  # 3) Clone issue labels SRC → DEST (so issue copy can apply them).
  gh_mutate label clone "${SRC}" --repo "${DEST}" --force

  # 4) Copy open issues in ascending number order (numbers line up on a fresh repo).
  local issues count i title body labels_csv body_file
  # Sort ascending so issue numbers line up 1:1 on the fresh DEST (keeps epic
  # `- [ ] #N` checklist references valid).
  issues="$("${GH_BIN}" issue list --repo "${SRC}" --state open --limit 200 \
            --json number,title,body,labels | jq 'sort_by(.number)')"
  count="$(jq 'length' <<<"${issues}")"
  log "copying ${count} open issue(s) ${SRC} → ${DEST}"
  for ((i = 0; i < count; i++)); do
    title="$(jq -r ".[${i}].title"  <<<"${issues}")"
    body="$( jq -r ".[${i}].body // \"\"" <<<"${issues}")"
    labels_csv="$(jq -r ".[${i}].labels | map(.name) | join(\",\")" <<<"${issues}")"
    body_file="$(mktemp)"; printf '%s' "${body}" > "${body_file}"
    if [[ -n "${labels_csv}" ]]; then
      gh_mutate issue create --repo "${DEST}" --title "${title}" --body-file "${body_file}" --label "${labels_csv}"
    else
      gh_mutate issue create --repo "${DEST}" --title "${title}" --body-file "${body_file}"
    fi
    rm -f "${body_file}"
  done

  # 5) Provision the dispatch pipeline label vocabulary on DEST (no issues carry
  #    them yet — the pipeline applies `queued` etc. itself during the test run).
  log "bootstrapping pipeline labels on ${DEST}"
  PIPELINE_REPO="${DEST}" "${SCRIPT_DIR}/../bootstrap-labels.sh"

  log "clone-to-testrepo: done ($(is_dry_run && echo DRY-RUN || echo applied)). DEST=${DEST}"
}

main "$@"
