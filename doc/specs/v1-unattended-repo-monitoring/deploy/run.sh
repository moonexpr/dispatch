#!/usr/bin/env bash
# Unattended dispatch-1.0 sprint runner — billy.maic, invoked by cron 4x/day.
# Dedicated clone, single-instance lock, staged-merge flag, dated logs.
#   Kill switch:   touch ~/sprint-runner/PAUSE
#   Enable merge:  touch ~/sprint-runner/MERGE_ENABLED   (default: PR-only)
#   Remote ctrl:   touch ~/sprint-runner/RC_ENABLED      (monitor/steer the run
#                  from claude.ai/code + the Claude app, with mobile push; off by
#                  default — needs a full-scope `claude /login`, NOT an API key /
#                  inference-only token. See code.claude.com/docs/en/remote-control)
#   Per-run cap:   MAX_ISSUES=N (default 4)
set -uo pipefail

ROOT="$HOME/sprint-runner"
REPO="$ROOT/dispatch"
REMOTE="git@github.com:ReclaimByDesign/dispatch.git"
LOGDIR="$ROOT/logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/run-$(date +%Y%m%d-%H%M%S).log"
exec >>"$LOG" 2>&1

echo "=== runner start $(date -Is) ==="

# Kill switch
if [ -f "$ROOT/PAUSE" ]; then echo "PAUSE file present; exiting 0."; exit 0; fi

# Single-instance lock — no overlapping ticks
exec 9>"$ROOT/.lock"
if ! flock -n 9; then echo "another runner holds the lock; exiting 0."; exit 0; fi

# Clone-if-absent
if [ ! -d "$REPO/.git" ]; then
  echo "cloning dispatch into $REPO ..."
  git clone "$REMOTE" "$REPO" || { echo "clone FAILED"; exit 1; }
fi

# Hard-sync the dedicated clone to origin/main (runner owns this checkout)
cd "$REPO" || { echo "cd $REPO failed"; exit 1; }
git fetch origin --prune || { echo "fetch FAILED"; exit 1; }
git checkout -q -B main origin/main || { echo "checkout main FAILED"; exit 1; }
git reset --hard origin/main
git clean -fdq
echo "synced to $(git rev-parse --short HEAD) on $(git branch --show-current)"

# Staged-merge directive
if [ -f "$ROOT/MERGE_ENABLED" ]; then
  MERGE_STATE="auto-merge"
  DIRECTIVE="MERGE: when the PR is green (local smoke + shellcheck, and CI if it runs), merge it with: gh pr merge <n> --squash --delete-branch ; then update the issue/labels and continue to the next ready issue."
else
  MERGE_STATE="PR-only"
  DIRECTIVE="MERGE MODE = PR-ONLY (staged rollout): do NOT merge any PR. Open the PR, confirm local smoke + shellcheck are green, comment 'ready for human review', label it, and continue to the next ready issue. A human enables auto-merge later by creating the MERGE_ENABLED flag."
fi

MAX_ISSUES="${MAX_ISSUES:-4}"
PROMPT="$(cat "$ROOT/runner-prompt.txt")

RUN LIMITS: do at most ${MAX_ISSUES} issue(s) this run, then post the digest and stop.
${DIRECTIVE}"

# Remote Control (rc): when RC_ENABLED is present, register this unattended run
# as a Remote Control session so the operator can monitor/steer it (and get
# mobile push) from claude.ai/code or the Claude app. Requires Claude Code
# >= 2.1.51 and a full-scope claude.ai OAuth login; an inference-only
# CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY cannot establish RC, so this stays
# opt-in to keep the default cron path unbreakable.
CLAUDE_ARGS=(-p "$PROMPT" --dangerously-skip-permissions)
if [ -f "$ROOT/RC_ENABLED" ]; then
  CLAUDE_ARGS+=(--remote-control "dispatch-sprint-runner")
  RC_STATE="rc=on"
else
  RC_STATE="rc=off (touch $ROOT/RC_ENABLED to enable)"
fi

echo "=== invoking claude (max_issues=${MAX_ISSUES}, merge=${MERGE_STATE}, ${RC_STATE}) ==="
timeout 5400 claude "${CLAUDE_ARGS[@]}"
rc=$?
echo "=== runner end $(date -Is) claude_exit=${rc} ==="
exit 0
