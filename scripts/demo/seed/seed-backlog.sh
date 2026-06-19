#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# seed-backlog.sh — seed the demo repo's 'queued' backlog from backlog.json
# (E0-1, #47).
#
# OPERATOR step. Provisions ReclaimByDesign/demo-repository with a representative
# 'queued' web-dev backlog so the live pipeline has a meaningful mix to drain and
# the snapshot phase (E8-1) has issues to capture.
#
# ADDITIVE ONLY: it only *creates* new issues from the committed manifest. It
# never edits, closes, or re-labels the demo repo's pre-existing issues
# (#11–#15 etc.) — satisfying #47's "seeding only adds" criterion.
#
# Provision the labels first (so the `queued` label exists), then seed:
#   PIPELINE_REPO=ReclaimByDesign/demo-repository PIPELINE_DRY_RUN=0 scripts/bootstrap-labels.sh
#   PIPELINE_REPO=ReclaimByDesign/demo-repository PIPELINE_DRY_RUN=0 scripts/demo/seed/seed-backlog.sh
#
# Dry-run is the default (§8): it prints the intended `gh issue create` calls and
# mutates nothing. Set PIPELINE_DRY_RUN=0 to create the issues for real.
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../lib/common.sh
source "${SCRIPT_DIR}/../../lib/common.sh"

MANIFEST="${SEED_MANIFEST:-${SCRIPT_DIR}/backlog.json}"

main() {
  is_dry_run || require_tool "${GH_BIN}"
  require_tool jq
  [[ -f "${MANIFEST}" ]] || die "seed manifest not found: ${MANIFEST}"

  local count
  count="$(jq 'length' "${MANIFEST}")"

  local i title body labels_csv
  for ((i = 0; i < count; i++)); do
    title="$(jq -r ".[${i}].title" "${MANIFEST}")"
    body="$(jq -r ".[${i}].body" "${MANIFEST}")"
    # gh accepts a comma-separated list for --label.
    labels_csv="$(jq -r ".[${i}].labels | join(\",\")" "${MANIFEST}")"
    gh_mutate issue create --title "${title}" --body "${body}" --label "${labels_csv}"
  done

  log "seed-backlog: ${count} issues seeded ($(is_dry_run && echo dry-run || echo applied))."
}

main "$@"
