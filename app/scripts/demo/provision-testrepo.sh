#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# provision-testrepo.sh — provision a disposable pipeline TEST TARGET by
# mirroring a source repo (code + issues + labels) into a fresh repo.
#
#   Usage: scripts/demo/provision-testrepo.sh [DEST] [SRC]
#     DEST  destination repo owner/name (default: ReclaimByDesign/dispatch-testrepo-b)
#     SRC   source repo owner/name       (default: ReclaimByDesign/demo-repository)
#
# A working replacement for clone-to-testrepo.sh, which breaks here because the
# scripts/lib gh_mutate wrapper appends `--repo <cwd-repo>` to EVERY gh call —
# fatal for `gh repo create` (unknown flag) and dangerous for `gh issue create`
# (a trailing --repo silently retargets issues to the cwd repo). This script uses
# explicit `--repo` per call and never sources gh_mutate.
#
# Steps:
#   1. create DEST (private) if absent
#   2. mirror SRC default-branch code -> DEST
#   3. clone SRC issue labels -> DEST
#   4. copy SRC issues #1..N in ASCENDING order (numbers line up 1:1 so epic
#      `- [ ] #N` checklists stay valid), preserving title/body + original labels
#      MINUS the pipeline state-machine labels (so DEST starts un-queued and the
#      pipeline's own intake exercises the queueing). Closed-in-SRC issues are
#      created open too, to keep the numbering contiguous.
#
# Outward-facing: creates a repo + issues. Run deliberately.
# ---------------------------------------------------------------------------
set -euo pipefail

DEST="${1:-ReclaimByDesign/dispatch-testrepo-b}"
SRC="${2:-ReclaimByDesign/demo-repository}"
GH="${GH_BIN:-gh}"

# Pipeline state-machine labels to strip from copied issues (DEST starts fresh).
STATE_LABELS="queued claimed in-progress pr-open in-review docs-pending done done-pending-merge needs-human needs-research wontdo_parent_issue blocked"

say() { printf '[provision-testrepo] %s\n' "$*" >&2; }

# 1) create DEST if absent
if "${GH}" repo view "${DEST}" >/dev/null 2>&1; then
  say "DEST ${DEST} already exists — skipping create"
else
  say "creating ${DEST} (private)"
  "${GH}" repo create "${DEST}" --private \
    --description "Disposable test target; mirror of ${SRC} for engineer_sdk live testing." >/dev/null
fi

# 2) mirror code (full clone so the push is not shallow)
tmp="$(mktemp -d)"; trap 'rm -rf "${tmp}"' EXIT
say "cloning ${SRC} code"
"${GH}" repo clone "${SRC}" "${tmp}/src" >/dev/null 2>&1
def="$(git -C "${tmp}/src" rev-parse --abbrev-ref HEAD)"
say "pushing ${def} -> ${DEST}"
git -C "${tmp}/src" push "https://github.com/${DEST}.git" "${def}:${def}" >/dev/null 2>&1 \
  || say "code push reported nothing to push (source may be code-empty)"

# 3) clone labels
say "cloning labels ${SRC} -> ${DEST}"
"${GH}" label clone "${SRC}" --repo "${DEST}" --force >/dev/null 2>&1 || say "label clone warned"

# 4) copy issues #1..N ascending, preserving numbering
say "copying issues ${SRC} -> ${DEST} (ascending, numbering preserved)"
nums="$("${GH}" issue list --repo "${SRC}" --state all --limit 200 --json number --jq 'sort_by(.number)[].number')"
for n in ${nums}; do
  title="$("${GH}" issue view "${n}" --repo "${SRC}" --json title --jq '.title')"
  "${GH}" issue view "${n}" --repo "${SRC}" --json body --jq '.body' > "${tmp}/body.md"
  # original labels minus the pipeline state-machine labels
  labels="$("${GH}" issue view "${n}" --repo "${SRC}" --json labels --jq '[.labels[].name]|join("\n")' \
    | grep -vxF -f <(printf '%s\n' ${STATE_LABELS}) | paste -sd, -)"
  args=(issue create --repo "${DEST}" --title "${title}" --body-file "${tmp}/body.md")
  [ -n "${labels}" ] && args+=(--label "${labels}")
  url="$("${GH}" "${args[@]}")"
  say "  #${n} -> ${url##*/}  ${title}"
done

say "done. DEST=${DEST}"
