#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# mock-engineer.sh — stub Engineer for offline roundtrip testing.
#
# Interface (schemas/job-request.json → schemas/invoice.json):
#   Input:  Job Request JSON as $1 (or on stdin)
#   Output: Invoice JSON on stdout
#   Exit:   0 on success, 1 on bad input
#
# Swap-in: ENGINEER_BIN=scripts/mock-engineer.sh
# Swap-out: ENGINEER_BIN=ruflo  (or any binary with the same interface)
#
# Issue → fixture mapping (cycles all three status paths):
#   101 → invoice-completed.json       (xs scope, gen-local, CI green)
#   102 → invoice-failed.json          (m scope, fix-ladder cap hit)
#   103 → invoice-needs-human.json     (vague issue, returned immediately)
#   104 → invoice-large-completed.json (l scope, gen-frontier, CMS migration)
#   any → needs-human with an explanatory error
#
# Override: MOCK_INVOICE_STATUS=completed|failed|needs-human forces a
# specific status regardless of issue number (useful for targeted tests).
# ---------------------------------------------------------------------------
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIX="${SCRIPT_DIR}/fixtures"

# Accept Job Request from first arg or stdin.
if [[ $# -ge 1 && -n "$1" ]]; then
  job_json="$1"
else
  job_json="$(cat)"
fi

# Validate we have parseable JSON with an issue field.
issue_num="$(printf '%s' "${job_json}" | jq -r '.issue // empty' 2>/dev/null)"
if [[ -z "${issue_num}" ]]; then
  printf 'mock-engineer: could not parse .issue from Job Request\n' >&2
  exit 1
fi

log_stub() { printf '[mock-engineer] %s\n' "$*" >&2; }
log_stub "received job_id=$(printf '%s' "${job_json}" | jq -r '.job_id // "?"') issue=#${issue_num} route=$(printf '%s' "${job_json}" | jq -r '.route // "?"')"

# Status override for targeted single-path testing.
if [[ -n "${MOCK_INVOICE_STATUS:-}" ]]; then
  log_stub "MOCK_INVOICE_STATUS override: ${MOCK_INVOICE_STATUS}"
  case "${MOCK_INVOICE_STATUS}" in
    completed)   cat "${FIX}/invoice-completed.json" ;;
    failed)      cat "${FIX}/invoice-failed.json" ;;
    needs-human) cat "${FIX}/invoice-needs-human.json" ;;
    *) printf 'mock-engineer: unknown MOCK_INVOICE_STATUS=%s\n' "${MOCK_INVOICE_STATUS}" >&2; exit 1 ;;
  esac
  exit 0
fi

# Route by issue number → cycles all status paths.
case "${issue_num}" in
  101) log_stub "→ completed (xs, gen-local)";   cat "${FIX}/invoice-completed.json" ;;
  102) log_stub "→ failed (m, fix-ladder cap)";  cat "${FIX}/invoice-failed.json" ;;
  103) log_stub "→ needs-human (vague issue)";   cat "${FIX}/invoice-needs-human.json" ;;
  104) log_stub "→ completed (l, gen-frontier)"; cat "${FIX}/invoice-large-completed.json" ;;
  *)
    log_stub "→ needs-human (no fixture for #${issue_num})"
    jq -n \
      --argjson issue "${issue_num}" \
      --arg     ts    "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '1970-01-01T00:00:00Z')" \
      '{invoice_id:   ("issue-" + ($issue|tostring) + "-mock"),
        issue:        $issue,
        repo:         "ReclaimByDesign/dispatch",
        status:       "needs-human",
        branch:       null,
        pr_number:    null,
        scope_actual: "xs",
        route_used:   "gen-local",
        cost:         {tokens_in: 0, tokens_out: 0, duration_seconds: 0},
        artifacts:    [],
        errors:       ["No fixture defined for issue #" + ($issue|tostring)],
        summary:      ("No mock fixture for issue #" + ($issue|tostring) + "; returning needs-human."),
        timestamp:    $ts}' ;;
esac
