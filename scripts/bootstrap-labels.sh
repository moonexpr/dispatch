#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bootstrap-labels.sh — create the pipeline label vocabulary (HANDOFF §5.6)
#
# OWNS: label *existence* only (create/upsert). It NEVER applies labels to
# issues — state transitions are owned by dispatch.sh / fix-dispatch.sh /
# workflow steps / closure.sh.
#
# Idempotent: `gh label create --force` upserts, so a second run is a no-op
# (exit 0). Dry-run prints intended `gh` calls and mutates nothing (§7.1).
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

main() {
  is_dry_run || require_tool "${GH_BIN}"
  local count=0
  while IFS= read -r name; do
    [[ -z "${name}" ]] && continue
    gh_mutate label create "${name}" \
      --color "$(label_color "${name}")" \
      --description "$(label_desc "${name}")" \
      --force
    count=$((count + 1))
  done < <(all_pipeline_labels)
  log "bootstrap-labels: ${count} labels ensured ($(is_dry_run && echo dry-run || echo applied))."
}

main "$@"
