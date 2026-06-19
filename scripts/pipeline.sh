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
  -u, --until     STAGE   halt the tick AFTER <stage> completes, exit 0, and
                          leave the dumped artifacts on disk for inspection.
                          Valid stages (in order):
                            intake workorder engineer intake-invoice closure
                          (closure == the full tick == no flag.)
      --from      STAGE   resume the tick AT <stage>, injecting --artifact as
                          that stage's input and SKIPPING every earlier stage
                          (no intake, no re-classification). Requires --artifact.
                          Valid stages: engineer | intake-invoice | closure.
                          Composes with --until (which caps the forward run).
  -a, --artifact  FILE    captured artifact fed to the --from stage: a Job
                          Request (schemas/job-request.json) for `engineer`; an
                          Invoice (schemas/invoice.json) for `intake-invoice` /
                          `closure`. Validated against its schema as DATA before
                          the stage runs; never executed or eval'd.
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

  # Replay exactly the engineer stage on a captured Job Request:
  bash scripts/pipeline.sh --from engineer \
       --artifact .dispatch/artifacts/tick-XXXX/job-request.json --until engineer
USAGE
  exit 0
}

# ---------------------------------------------------------------------------
# _validate_artifact FILE SCHEMA — guard a replay --artifact (E2-3/#32).
# The artifact is UNTRUSTED DATA: it is parsed and field-checked with jq, never
# executed or eval'd (worker-contract security posture). This is not a full
# JSON-Schema validator — it enforces (a) the file exists, (b) it parses as a
# JSON object, and (c) every key listed in the schema's "required" array is
# present, which is the invariant the resumed stage depends on. Returns 0 when
# valid; on any failure it prints a clear error to stderr and returns non-zero.
_validate_artifact() {
  local file="$1" schema="$2"
  local label="${schema##*/}"
  [[ -f "${schema}" ]] || { err "internal: schema not found: ${schema}"; return 1; }
  [[ -f "${file}" ]]   || { err "--artifact file not found: ${file}"; return 1; }
  jq -e 'type == "object"' "${file}" >/dev/null 2>&1 \
    || { err "--artifact is not valid JSON (object expected): ${file}"; return 1; }
  local missing
  missing="$(jq -r --slurpfile s "${schema}" \
    '(($s[0].required // []) - keys) | join(" ")' "${file}" 2>/dev/null)" || missing=""
  if [[ -n "${missing}" ]]; then
    err "--artifact ${file} is not schema-valid (${label}): missing required field(s): ${missing}"
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Parse flags — applied after common.sh has sourced pipeline.env.
_bootstrap=0
_engineer="${ENGINEER_BIN:-${SCRIPT_DIR}/mock-engineer.sh}"
_until=""
_from=""
_artifact=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    -b|--bootstrap)  _bootstrap=1; shift ;;
    -r|--repo)       export PIPELINE_REPO="$2"; shift 2 ;;
    -l|--live)       export PIPELINE_DRY_RUN=0; shift ;;
    -e|--engineer)   _engineer="$2"; shift 2 ;;
    -f|--fixture)    export PIPELINE_FIXTURE_ISSUES="$2"; shift 2 ;;
    -u|--until)      _until="$2"; shift 2 ;;
    --from)          _from="$2"; shift 2 ;;
    -a|--artifact)   _artifact="$2"; shift 2 ;;
    -h|--help)       _usage ;;
    *) die "unknown flag: $1 (try --help)" ;;
  esac
done

# Validate --until against the canonical stage vocabulary (stage_ord from
# common.sh). An unknown stage is a usage error: exit non-zero and list the
# valid stages on stderr. The decision is exported so dispatch.sh (halt before
# the engineer) and the bridge (halt before architect-intake) honour it.
if [[ -n "${_until}" ]]; then
  if ! _until_ord="$(stage_ord "${_until}")"; then
    err "unknown --until stage: ${_until}"
    err "valid stages (in order): ${DISPATCH_STAGES[*]}"
    exit 2
  fi
  export DISPATCH_UNTIL_STAGE="${_until}" DISPATCH_UNTIL_ORD="${_until_ord}"
fi

# Validate --from/--artifact (E2-3/#32). --from resumes the tick AT a captured
# stage, injecting --artifact as that stage's input and skipping every earlier
# stage. --from REQUIRES --artifact, and the artifact is schema-validated as data
# BEFORE any stage runs. The stage selects which schema the artifact must match.
if [[ -n "${_from}" ]]; then
  case "${_from}" in
    engineer)               _from_schema="${SCRIPT_DIR}/../schemas/job-request.json" ;;
    intake-invoice|closure) _from_schema="${SCRIPT_DIR}/../schemas/invoice.json" ;;
    *) err "unknown --from stage: ${_from}"
       err "valid --from stages: engineer intake-invoice closure"
       exit 2 ;;
  esac
  [[ -n "${_artifact}" ]] || { err "--from ${_from} requires --artifact <file>"; exit 2; }
  _validate_artifact "${_artifact}" "${_from_schema}" || exit 2
  # --from and --until are mutually composable, but a --until stage that precedes
  # --from is contradictory (nothing would run). Reject it loudly.
  _from_ord="$(stage_ord "${_from}")"
  if [[ -n "${_until}" && "${_until_ord}" -lt "${_from_ord}" ]]; then
    err "--until ${_until} (stage ${_until_ord}) precedes --from ${_from} (stage ${_from_ord}); nothing to run"
    exit 2
  fi
elif [[ -n "${_artifact}" ]]; then
  err "--artifact given without --from <stage>"
  exit 2
fi

# Resolve relative engineer paths to absolute so the bridge script works from
# any cwd.
if [[ "${_engineer}" != /* && -f "${_engineer}" ]]; then
  _engineer="$(cd "$(dirname "${_engineer}")" && pwd)/$(basename "${_engineer}")"
fi

log "pipeline: repo=${PIPELINE_REPO:-<gh default>}  dry_run=${PIPELINE_DRY_RUN}  engineer=${_engineer##*/}"

# One stable tick id per tick, shared by every stage's artifact dump (E2-1) so
# all of a tick's artifacts (workorder.txt / job-request.json / invoice.json)
# land under the same ${DISPATCH_ARTIFACTS_DIR}/<tick-id>/ directory.
export DISPATCH_TICK_ID="${DISPATCH_TICK_ID:-tick-$(date -u +%Y%m%dT%H%M%SZ)}"

# ---------------------------------------------------------------------------
# Stage 0 — bootstrap labels (optional, idempotent).
if [[ "${_bootstrap}" -eq 1 ]]; then
  log "pipeline: provisioning labels on ${PIPELINE_REPO:-<gh default>}"
  bash "${SCRIPT_DIR}/bootstrap-labels.sh"
fi

# ---------------------------------------------------------------------------
# E2-3 (#32) — resume/replay a single stage from a captured artifact.
#
# Bypasses intake → ranking → claim entirely (no GitHub reads, no
# re-classification): the validated <artifact> is injected AT <stage> and the
# tick runs forward to closure (or to the --until cap). Replay deliberately
# EXECUTES the resumed stage — that is its purpose — while downstream GitHub
# mutations still honour PIPELINE_DRY_RUN. The source artifact is read-only DATA;
# the replay writes its own artifacts under a FRESH tick dir (E2-1) and never
# mutates or re-dumps the source.
_pipeline_resume() {
  local stage="$1" artifact="$2"
  local artifact_json; artifact_json="$(cat "${artifact}")"
  local _td=""
  if [[ -n "${DISPATCH_ARTIFACTS_DIR:-}" ]]; then
    _td="${DISPATCH_ARTIFACTS_DIR}/${DISPATCH_TICK_ID}"
    mkdir -p "${_td}"
  fi
  log "pipeline: resume from stage: ${stage} (artifact: ${artifact})"

  case "${stage}" in
    engineer)
      # Feed the captured Job Request to the Engineer → Invoice (mirrors the
      # bridge), tapping the Invoice into the fresh tick dir.
      local invoice; invoice="$("${_engineer}" "${artifact_json}")"
      if [[ -n "${_td}" ]]; then
        printf '%s\n' "${invoice}" >"${_td}/invoice.json"
      fi
      # Compose with --until: halt AFTER the engineer stage (ordinal 3).
      if [[ -n "${DISPATCH_UNTIL_ORD:-}" && "${DISPATCH_UNTIL_ORD}" -le 3 ]]; then
        log "pipeline: halted after stage: ${DISPATCH_UNTIL_STAGE}"
        return 0
      fi
      bash "${SCRIPT_DIR}/architect-intake.sh" "${invoice}"
      ;;
    intake-invoice|closure)
      # Feed the captured Invoice straight to architect-intake — the stage that
      # handles intake AND performs the closure label/ledger transition.
      bash "${SCRIPT_DIR}/architect-intake.sh" "${artifact_json}"
      ;;
  esac
  log "pipeline: done."
}

if [[ -n "${_from}" ]]; then
  _pipeline_resume "${_from}" "${_artifact}"
  exit 0
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
# Per-stage artifact dump (E2-1): tap the Invoice into the tick dir, read-only.
# Pure observability — the Invoice still flows engineer -> architect-intake below.
if [[ -n "\${DISPATCH_ARTIFACTS_DIR:-}" ]]; then
  _td="\${DISPATCH_ARTIFACTS_DIR}/\${DISPATCH_TICK_ID:-tick-unknown}"
  mkdir -p "\${_td}"
  printf '%s\n' "\${invoice}" > "\${_td}/invoice.json"
fi
# Stage gate (E2-2/#31): halt AFTER the engineer stage (ordinal 3), before
# architect-intake (the intake-invoice stage), when --until requested it.
if [[ -n "\${DISPATCH_UNTIL_ORD:-}" && "\${DISPATCH_UNTIL_ORD}" -le 3 ]]; then
  exit 0
fi
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
  # Heartbeat seam (E1-2): emit a `started` record before the dispatch body and
  # an `ended` record after it, both INSIDE the E1-1 lock so the record reflects
  # exactly one serialized tick. dispatch.sh appends the issues it claims to the
  # tick-scoped sink that tick_record_start initialised, so tick_record_end can
  # name them without re-querying GitHub. Records are local-file only (no gh).
  tick_record_start
  # Test seam (never set in production): simulate a tick that crashes right after
  # starting, so it leaves a `started` record with NO `ended` — exactly the
  # stuck-tick signal the E1-3 reaper and E4 ledger key off.
  [[ -n "${DISPATCH_CRASH_AFTER_START_TEST:-}" ]] && \
    die "injected post-start crash (DISPATCH_CRASH_AFTER_START_TEST test seam)"
  log "pipeline: dispatch starting"
  # Crash reaper (E1-3) runs at the TOP of dispatch.sh's main() — i.e. here,
  # inside this E1-1 lock and before any claim — re-queuing issues stranded in
  # `claimed` by a crashed engineer. Its re-queue actions are appended to the
  # tick-scoped reaped sink (exported by tick_record_start) so tick_record_end
  # records them (reaped_count/reaped) in the heartbeat below.
  local _rc=0
  bash "${SCRIPT_DIR}/dispatch.sh" || _rc=$?
  tick_record_end "${_rc}"
  # Stage gate (E2-2/#31): when --until halted the tick before closure, name the
  # stage we stopped at and point the operator at the dumped artifacts.
  if [[ -n "${DISPATCH_UNTIL_STAGE:-}" && "${DISPATCH_UNTIL_STAGE}" != "closure" ]]; then
    log "pipeline: halted after stage: ${DISPATCH_UNTIL_STAGE} (artifacts: ${DISPATCH_ARTIFACTS_DIR:-<unset>})"
  fi
  log "pipeline: done."
  return "${_rc}"
}
with_tick_lock _pipeline_tick
