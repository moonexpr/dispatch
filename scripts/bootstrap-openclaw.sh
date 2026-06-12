#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# bootstrap-openclaw.sh — register OpenClaw jobs + webhook (HANDOFF §5.1)
#
# Idempotent: checks `openclaw cron list` before adding. Dry-run prints the
# intended `openclaw` calls and mutates nothing.
#
# Registers:
#   1. pipeline-dispatch  — COMMAND cron, every 10m -> scripts/dispatch.sh
#                           (exit-code semantics; non-zero => operator notify;
#                            no agent turn, no tokens).
#   2. pipeline-digest    — ISOLATED agent cron, 08:00, cheap model alias,
#                           --announce to the operator channel.
#   3. github webhook     — inbound /webhook/github (bearer-token auth) that
#                           routes CI/review results to an isolated session
#                           running fix-dispatch.sh (failure) or closure.sh
#                           (success).
#
# NOTE: exact `openclaw` flags are a moving surface (HANDOFF §10); verify
# against current Gateway docs before going live. See OPEN-QUESTIONS.md.
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

# cron_exists: true if a job with the given --name is already registered.
cron_exists() {
  local name="$1"
  have_tool "${OPENCLAW_BIN}" || return 1
  "${OPENCLAW_BIN}" cron list 2>/dev/null | grep -qE "(^|[[:space:]])${name}([[:space:]]|$)"
}

ensure_dispatch_cron() {
  if cron_exists "pipeline-dispatch"; then
    log "cron pipeline-dispatch already registered; skipping."
    return 0
  fi
  run "${OPENCLAW_BIN}" cron add \
    --name "pipeline-dispatch" \
    --schedule "${OPENCLAW_DISPATCH_SCHEDULE}" \
    --type command \
    --command "${PIPELINE_ROOT}/scripts/dispatch.sh" \
    --notify-on-failure
}

ensure_digest_cron() {
  if cron_exists "pipeline-digest"; then
    log "cron pipeline-digest already registered; skipping."
    return 0
  fi
  run "${OPENCLAW_BIN}" cron add \
    --name "pipeline-digest" \
    --schedule "${OPENCLAW_DIGEST_SCHEDULE}" \
    --type isolated \
    --model "${OPENCLAW_MODEL_CHEAP}" \
    --announce \
    ${OPENCLAW_OPERATOR_CHANNEL:+--channel "${OPENCLAW_OPERATOR_CHANNEL}"} \
    --prompt "Daily pipeline digest: list open jobs by label, flag stuck jobs (claimed/pr-open with no movement, needs-human), and report fix-attempt and spend-relevant counts."
}

ensure_github_webhook() {
  # Inbound webhook -> isolated session that branches on CI/review outcome.
  run "${OPENCLAW_BIN}" webhook add \
    --path "/webhook/github" \
    --auth bearer \
    --bearer-token-env "OPENCLAW_WEBHOOK_BEARER_TOKEN" \
    --type isolated \
    --handler "${PIPELINE_ROOT}/scripts/fix-dispatch.sh" \
    --on-success-handler "${PIPELINE_ROOT}/scripts/closure.sh"
}

main() {
  is_dry_run || require_tool "${OPENCLAW_BIN}"
  ensure_dispatch_cron
  ensure_digest_cron
  ensure_github_webhook
  log "bootstrap-openclaw: dispatch + digest cron and github webhook ensured."
  log "Hardening reminder (§8): bind ${OPENCLAW_BIND_ADDR:-<unset>} must be loopback/tailnet; channel pairing on; exec allowlist = gh,git,python,claude."
}

main "$@"
