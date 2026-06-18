#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# pipeline.sh — kernel entrypoint for one full pipeline tick.
#
# Wires together: [bootstrap] → dispatch (select + claim) → engineer → intake.
# Each component (dispatch.sh, engineer, architect-intake.sh) remains
# independently runnable; this script is the single-command path for both
# manual testing and production one-shot ticks.
#
# Usage:
#   bash scripts/pipeline.sh [OPTIONS]
#
# Options:
#   -b, --bootstrap         provision pipeline labels on PIPELINE_REPO first
#   -r, --repo  owner/repo  target repo; sets PIPELINE_REPO
#   -l, --live              PIPELINE_DRY_RUN=0 — mutate GitHub (default: dry-run)
#   -e, --engineer  BIN     Engineer binary (default: scripts/mock-engineer.sh)
#                           Must read a Job Request JSON as $1, write Invoice JSON
#                           to stdout, exit 0 on completion.
#   -f, --fixture   FILE    PIPELINE_FIXTURE_ISSUES path (offline issue list)
#   -h, --help              show this help and exit
#
# Environment variables (pipeline.env or shell) are loaded first; flags win.
# ENGINEER_BIN is protected from pipeline.env override when set before calling
# this script.
#
# How the wiring works:
#   dispatch.sh calls engineer_dispatch() → ENGINEER_BIN for each claimed issue.
#   pipeline.sh replaces ENGINEER_BIN with a bridge script that (a) calls the
#   real engineer, (b) immediately passes the returned Invoice to architect-intake.
#   In dry-run mode dispatch.sh prints "DRY-RUN: $ENGINEER_BIN …" and never
#   executes the bridge, so intake is also skipped — correct behaviour.
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# ---------------------------------------------------------------------------
_usage() {
  cat >&2 <<'USAGE'
usage: pipeline.sh [OPTIONS]

  -b, --bootstrap         provision pipeline labels on PIPELINE_REPO first
  -r, --repo  owner/repo  target repo; sets PIPELINE_REPO
  -l, --live              PIPELINE_DRY_RUN=0 (default: dry-run, no mutations)
  -e, --engineer  BIN     Engineer binary (default: scripts/mock-engineer.sh)
  -f, --fixture   FILE    issue list JSON for offline testing
  -h, --help              show this help

Examples:
  # Dry-run against a target repo using the mock engineer:
  bash scripts/pipeline.sh --repo owner/my-repo

  # Provision labels, then run live with the mock engineer:
  bash scripts/pipeline.sh --bootstrap --repo owner/my-repo --live

  # Live run with Ruflo as the Engineer:
  bash scripts/pipeline.sh --repo owner/my-repo --live --engineer ruflo

  # Fully offline smoke test:
  bash scripts/pipeline.sh --fixture scripts/fixtures/queued-issues.json
USAGE
  exit 0
}

# ---------------------------------------------------------------------------
# Parse flags — applied after common.sh has sourced pipeline.env.
_bootstrap=0
_engineer="${ENGINEER_BIN:-${SCRIPT_DIR}/mock-engineer.sh}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -b|--bootstrap)  _bootstrap=1; shift ;;
    -r|--repo)       export PIPELINE_REPO="$2"; shift 2 ;;
    -l|--live)       export PIPELINE_DRY_RUN=0; shift ;;
    -e|--engineer)   _engineer="$2"; shift 2 ;;
    -f|--fixture)    export PIPELINE_FIXTURE_ISSUES="$2"; shift 2 ;;
    -h|--help)       _usage ;;
    *) die "unknown flag: $1 (try --help)" ;;
  esac
done

# Resolve relative engineer paths to absolute so the bridge script works from
# any cwd.
if [[ "${_engineer}" != /* && -f "${_engineer}" ]]; then
  _engineer="$(cd "$(dirname "${_engineer}")" && pwd)/$(basename "${_engineer}")"
fi

log "pipeline: repo=${PIPELINE_REPO:-<gh default>}  dry_run=${PIPELINE_DRY_RUN}  engineer=${_engineer##*/}"

# ---------------------------------------------------------------------------
# Stage 0 — bootstrap labels (optional, idempotent).
if [[ "${_bootstrap}" -eq 1 ]]; then
  log "pipeline: provisioning labels on ${PIPELINE_REPO:-<gh default>}"
  bash "${SCRIPT_DIR}/bootstrap-labels.sh"
fi

# ---------------------------------------------------------------------------
# Stage 1+2+3 — dispatch → engineer → intake, wired through a bridge.
#
# The bridge wraps the real engineer: dispatch.sh calls ENGINEER_BIN (the
# bridge) for each claimed issue, the bridge calls the real engineer, then
# pipes the Invoice immediately to architect-intake.sh.  Intake runs once per
# issue, in claim order.

_bridge="$(mktemp /tmp/pipeline-bridge.XXXXXX.sh)"
trap 'rm -f "${_bridge}"' EXIT

cat > "${_bridge}" <<BRIDGE
#!/usr/bin/env bash
set -euo pipefail
invoice="\$("${_engineer}" "\$1")"
bash "${SCRIPT_DIR}/architect-intake.sh" "\${invoice}"
BRIDGE
chmod +x "${_bridge}"

export ENGINEER_BIN="${_bridge}"

# ---------------------------------------------------------------------------
# The tick body (dispatch → engineer → intake) runs under the single-host tick
# mutex (with_tick_lock, E1-1) so an overlapping cron invocation either waits or
# skips cleanly instead of double-claiming a queued issue. The reaper/heartbeat
# child issues hook off this same wrapped body.
_pipeline_tick() {
  log "pipeline: dispatch starting"
  bash "${SCRIPT_DIR}/dispatch.sh"
  log "pipeline: done."
}
with_tick_lock _pipeline_tick
