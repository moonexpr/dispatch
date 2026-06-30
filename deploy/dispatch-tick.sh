#!/usr/bin/env bash
# dispatch-tick.sh — run ONE unattended live engineering tick.
#
# The shared ExecStart for both the systemd service (Linux) and the launchd
# agent (macOS / WORKSTATION). It self-locates the repo (this script lives in
# <repo>/deploy/), sources the gitignored pipeline.env for secrets + target,
# and runs a single LIVE tick. The engine picks the first open ready issue when
# no issue is given; the target repo comes from $PIPELINE_REPO.
#
# pipeline.env (gitignored, repo root) must provide at least:
#   CLAUDE_CODE_OAUTH_TOKEN=...        # subscription auth for the engineer/architect agents
#   MODELS_BACKEND=cli                  # draw inference from the Claude Code subscription
#   PIPELINE_REPO=owner/repo            # the repo whose issues this engine works
# optional:
#   DISPATCH_ENGINE=websitewf           # use the WebsiteWF overlay engine (default: baseworkflow)
#   WEBSITEWF_USECASE=scaffold-foundation   # a specific WebsiteWF use-case overlay
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Locate the repo whether this wrapper sits at the repo root or in <repo>/deploy/:
if [ -x "$HERE/dispatch" ]; then
  REPO="$HERE"
else
  REPO="$(cd "$HERE/.." && pwd)"
fi
cd "$REPO"

# Source the env file: prefer .env, fall back to the legacy pipeline.env.
for envf in .env pipeline.env; do
  if [ -f "$envf" ]; then
    set -a; . "./$envf"; set +a
    break
  fi
done

if [ -z "${PIPELINE_REPO:-}" ]; then
  echo "dispatch-tick: PIPELINE_REPO is unset (set it in $REPO/.env)" >&2
  exit 2
fi

# --live: this mutates GitHub (clone -> branch -> squash commit -> push -> PR ->
# squash-merge per the label ladder). Drop --live for a dry-run tick.
exec ./dispatch --live "$@"
