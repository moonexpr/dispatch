# Unattended ticking (app/installer/)

Run the dispatch engine on a schedule — **one live tick every 2 hours (12×/day)**.
Each tick runs `./dispatch --live` against `$PIPELINE_REPO`, working the first open
ready issue: clone → branch → engineer (agents commit locally) → admin squashes
into one commit, pushes, opens a PR → label ladder / squash-merge.

## Files

| File | Role |
|------|------|
| `dispatch-tick.sh` | Shared ExecStart: self-locates the repo, sources `pipeline.env`, runs one `./dispatch --live` tick. |
| `dispatch.service` + `dispatch.timer` | systemd (Linux) — the unit + the every-2h schedule. |
| `com.reclaimbydesign.dispatch.plist` | launchd (macOS / **WORKSTATION**, where systemd is not native) — same 12×/day schedule. |

## Prerequisites

`pipeline.env` at the repo root (gitignored) must define the target + auth:

```sh
CLAUDE_CODE_OAUTH_TOKEN=...     # subscription auth for the architect/engineer agents
MODELS_BACKEND=cli              # draw inference from the Claude Code subscription
PIPELINE_REPO=owner/repo        # the repo whose issues this engine works
# optional:
# DISPATCH_ENGINE=websitewf            # use the WebsiteWF overlay engine (default: baseworkflow)
# WEBSITEWF_USECASE=scaffold-foundation # a specific WebsiteWF use-case overlay
```

`gh` must be authenticated for the target repo (the engine pushes branches + opens PRs).

## Install — Linux (systemd, user units)

```sh
mkdir -p ~/.config/systemd/user
cp app/installer/dispatch.service app/installer/dispatch.timer ~/.config/systemd/user/
# edit dispatch.service ExecStart to point at <your-repo>/app/installer/dispatch-tick.sh
systemctl --user daemon-reload
systemctl --user enable --now dispatch.timer
systemctl --user list-timers dispatch.timer      # confirm next fire
journalctl --user -u dispatch.service -f          # watch a tick
```

## Install — macOS / WORKSTATION (launchd)

```sh
cp app/installer/com.reclaimbydesign.dispatch.plist ~/Library/LaunchAgents/
# edit the ProgramArguments path to <your-repo>/app/installer/dispatch-tick.sh
launchctl load -w ~/Library/LaunchAgents/com.reclaimbydesign.dispatch.plist
tail -f /tmp/dispatch-tick.log
```

## Safety

`dispatch-tick.sh` runs `--live` — it **mutates GitHub** every tick (pushes branches,
opens/merges PRs). To rehearse without mutations, drop `--live` from the wrapper (or set
`PIPELINE_DRY_RUN=1`). Start with a low-traffic `PIPELINE_REPO`; watch the first few ticks
before leaving it unattended.
