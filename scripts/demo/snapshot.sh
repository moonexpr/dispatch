#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# snapshot.sh — read-only capture of the live demo queue into a drop-in fixture
# (E8-1 / issue #43). This is the ONLY harness phase that touches GitHub, and
# it is strictly read-only: it wraps `gh issue list --json …` against the demo
# repo and writes the raw array verbatim to a committed snapshot under
# scripts/demo/snapshots/<slug>.json.
#
# The captured field-set is the SUPERSET both offline consumers need, so one
# snapshot is a drop-in for both:
#   - dispatch.sh load_queued_issues() / `./dispatch --fixture`
#       needs number,title,body,labels                 (./pipeline devtools dispatch:34)
#   - intake.py intake_from_repo() via INTAKE_FIXTURE_REPO
#       needs number,title,body,labels,assignees,url    (intake.py:181)
# `labels` is preserved as the raw gh object shape ([{name,…}]) because that is
# exactly what intake.py flattens via lbl['name'] (intake.py:196); a flattened
# string array would break that consumer.
#
# Default capture is ALL open issues (no `--label queued` filter) so the
# snapshot faithfully mirrors the repo; scenarios (E8-4) decide which issues
# carry `queued`. This is the one deliberate divergence from load_queued_issues
# ()'s live `--label queued` filter.
#
# Least-privilege: this needs only read scope and NEVER runs a mutating gh
# subcommand. An empty / absent / 404 demo queue yields `[]` plus a non-fatal
# notice pointing at E0-1 (#47) — the script must never 404-crash on a fresh
# repo.
#
# Usage:
#   scripts/demo/snapshot.sh [--repo OWNER/REPO] [--out FILE] [--slug SLUG] [--label L]
# Defaults: --repo ReclaimByDesign/demo-repository, --slug demo-current.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/../lib/common.sh"   # GH_BIN, log/warn/die

REPO="ReclaimByDesign/demo-repository"
SLUG="demo-current"
OUT=""
LABEL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)  REPO="$2"; shift 2 ;;
    --slug)  SLUG="$2"; shift 2 ;;
    --out)   OUT="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    -h|--help)
      grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

require_tool jq

SNAP_DIR="${SCRIPT_DIR}/snapshots"
mkdir -p "${SNAP_DIR}"
: "${OUT:=${SNAP_DIR}/${SLUG}.json}"
PROV="${SNAP_DIR}/${SLUG}.provenance.json"
FIELDS="number,title,body,labels,assignees,url,state"

log "snapshot: capturing open issues from ${REPO} (read-only) -> ${OUT}"

# Read-only capture. The field-set is the dual-consumer superset; `labels` comes
# back from gh as {name,…} objects and is written verbatim. Tolerate a missing /
# 404 / unauthenticated repo: degrade to an empty snapshot rather than crashing.
gh_args=(issue list --repo "${REPO}" --state open --json "${FIELDS}" --limit 50)
[[ -n "${LABEL}" ]] && gh_args+=(--label "${LABEL}")

raw=""
if ! raw="$("${GH_BIN}" "${gh_args[@]}" 2>/dev/null)"; then
  warn "snapshot: '${GH_BIN} issue list' failed for ${REPO} (absent / 404 / no auth?) — writing empty snapshot."
  warn "snapshot: seed the live queue first — see E0-1 (#47), the bootstrap of ReclaimByDesign/demo-repository."
  raw="[]"
fi
[[ -n "${raw}" ]] || raw="[]"

count="$(printf '%s' "${raw}" | jq 'length' 2>/dev/null || echo 0)"
if [[ "${count}" -eq 0 ]]; then
  warn "snapshot: ${REPO} returned no open issues — writing empty snapshot ([])."
  warn "snapshot: seed a representative 'queued' backlog first — see E0-1 (#47)."
fi

# Write the array verbatim (pretty-printed for review); labels stay object-shaped.
printf '%s' "${raw}" | jq '.' > "${OUT}"

# Provenance sidecar: who/when/which-HEAD captured this, for auditable replay.
# All probes are read-only and degrade to "" when offline so capture never fails.
captured_at="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")"
head_sha="$("${GH_BIN}" api "repos/${REPO}/commits/main" --jq .sha 2>/dev/null || echo "")"
gh_login="$("${GH_BIN}" api user --jq .login 2>/dev/null || echo "")"

jq -n \
  --arg captured_at "${captured_at}" \
  --arg repo "${REPO}" \
  --arg head_sha "${head_sha}" \
  --arg gh_login "${gh_login}" \
  --arg fields "${FIELDS}" \
  '{captured_at:$captured_at, repo:$repo, head_sha:$head_sha, gh_login:$gh_login, fields:$fields}' \
  > "${PROV}"

log "snapshot: wrote ${count} issue(s) to ${OUT} (+ provenance ${PROV})"
