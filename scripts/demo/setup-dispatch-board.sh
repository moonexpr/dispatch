#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup-dispatch-board.sh — provision a dedicated ProjectV2 board for testing the
# dispatch-field selection gate (src/intake/pipeline.py --dispatch).
#
#   Usage: scripts/demo/setup-dispatch-board.sh [REPO] [OWNER] [TITLE] [VALUE]
#     REPO   source repo whose open issues populate the board
#            (default: ReclaimByDesign/dispatch-testrepo-a)
#     OWNER  ProjectV2 owner (default: ReclaimByDesign)
#     TITLE  board title (default: "Dispatch Test - testrepo-a")
#     VALUE  Dispatch field value to set on every item (default: Denied)
#
# Creates the board, adds a "Dispatch" single-select field (options: Denied,
# Allowed), adds every open issue of REPO, and sets each item's Dispatch field to
# VALUE. Prints the board number so it can be wired as INTAKE_PROJECT=OWNER/NUM.
#
# NOT dry-run gated — this is an explicit operator provisioning action that only
# creates a NEW board (never touches existing boards).
# ---------------------------------------------------------------------------
set -euo pipefail

REPO="${1:-ReclaimByDesign/dispatch-testrepo-a}"
OWNER="${2:-ReclaimByDesign}"
TITLE="${3:-Dispatch Test - testrepo-a}"
VALUE="${4:-Denied}"
GH="${GH_BIN:-gh}"

say() { printf '[setup-dispatch-board] %s\n' "$*" >&2; }

# 1) Create the board.
say "creating board '${TITLE}' under ${OWNER}"
proj_json="$("${GH}" project create --owner "${OWNER}" --title "${TITLE}" --format json)"
PNUM="$(jq -r '.number' <<<"${proj_json}")"
PID="$(jq -r '.id' <<<"${proj_json}")"
say "board #${PNUM} (id ${PID})"

# 2) Add the Dispatch single-select field with Denied / Allowed options.
say "adding Dispatch single-select field (Denied, Allowed)"
"${GH}" project field-create "${PNUM}" --owner "${OWNER}" \
  --name "Dispatch" --data-type SINGLE_SELECT \
  --single-select-options "Denied,Allowed" --format json >/dev/null

# Re-read the field to resolve the field id + the option id for VALUE reliably.
field_json="$("${GH}" project field-list "${PNUM}" --owner "${OWNER}" --format json \
  | jq '.fields[] | select(.name=="Dispatch")')"
FIELD_ID="$(jq -r '.id' <<<"${field_json}")"
OPT_ID="$(jq -r --arg v "${VALUE}" '.options[] | select(.name==$v) | .id' <<<"${field_json}")"
[[ -n "${FIELD_ID}" && -n "${OPT_ID}" ]] || { say "ERROR: could not resolve Dispatch field/option ids"; exit 1; }
say "Dispatch field ${FIELD_ID}; '${VALUE}' option ${OPT_ID}"

# 3) Add every open issue, then set its Dispatch field to VALUE.
mapfile -t URLS < <("${GH}" issue list --repo "${REPO}" --state open --limit 200 \
  --json url --jq '.[].url')
say "adding ${#URLS[@]} issue(s) from ${REPO} and setting Dispatch=${VALUE}"
n=0
for url in "${URLS[@]}"; do
  item_id="$("${GH}" project item-add "${PNUM}" --owner "${OWNER}" --url "${url}" \
    --format json | jq -r '.id')"
  "${GH}" project item-edit --id "${item_id}" --project-id "${PID}" \
    --field-id "${FIELD_ID}" --single-select-option-id "${OPT_ID}" >/dev/null
  n=$((n + 1))
done

say "done: board ${OWNER}/${PNUM} — ${n} item(s), Dispatch=${VALUE}"
printf '%s\n' "INTAKE_PROJECT=${OWNER}/${PNUM}"
