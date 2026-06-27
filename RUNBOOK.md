# RUNBOOK.md — operating dispatch unattended

The single operator page for running the dispatch pipeline on a schedule,
inspecting a tick mid-flight, reading what it did, tuning its budget, and
recovering it when an issue gets stuck. Every command here is **offline /
dry-run-safe** unless explicitly flagged as a live action.

This runbook documents only rails that exist today (Pillars 1, 4, 5 plus the
budget guard). Its companion machine proof is `scripts/smoke.sh` **§7.26**
(`full unattended tick: lock+heartbeat+ledger+artifacts+soft-cap compose`),
which asserts the same rails compose end-to-end; this runbook's own
doc-consistency check is **§7.27**. The full env surface every
command below draws on is `pipeline.env.example` (copy it to `pipeline.env`,
which `scripts/lib/common.sh` auto-sources). Secrets live **only** in env —
never in a command line, log, or commit.

The six canonical tick stages, in execution order, are:

```
intake → workorder → prep → engineer → intake-invoice → closure
```

`./entrypoint.sh [flags]` runs one tick (it forwards to `scripts/pipeline.sh`);
`./entrypoint.sh report [flags]` renders the digest (read-only, offline).

---

## Schedule

Run the pipeline a few times a day from the host `crontab`, each tick guarded by
`flock` so an overlapping invocation can never double-claim a queued issue. The
committed, copy-pasteable example is **`examples/dispatch.crontab`** (and
`examples/dispatch.launchd.plist` for macOS `launchd`); install with
`crontab examples/dispatch.crontab` after editing the paths and repo.

The tick line (4×/day at 00:07, 06:07, 12:07, 18:07):

```cron
7 0,6,12,18 * * * cd /opt/dispatch && PIPELINE_REPO=owner/your-repo PIPELINE_DRY_RUN=0 \
  flock -n /tmp/dispatch.cron.lock ./entrypoint.sh --repo owner/your-repo \
  >> /opt/dispatch/dispatch.cron.log 2>&1
```

Notes:

- **Dry-run is the default.** `PIPELINE_DRY_RUN=0` on the line above is the
  *explicit* opt-in to mutate GitHub. Drop it (or set `=1`) to keep previewing.
- **Two locks, by design.** The pipeline serializes overlapping ticks with its
  own in-process flock on `DISPATCH_LOCK_FILE` (default: a repo-scoped path
  under `TMPDIR`; set it to pin the path). The outer `flock -n
  /tmp/dispatch.cron.lock` on the cron line is a belt-and-suspenders guard so a
  long tick never stacks.
- **Cadence** is 3–5×/day for the prototype; the example uses 4.

**Documented alternatives** (not the prototype default): the **OpenClaw
Gateway** (scheduler + webhook receiver + operator chat I/O — see the
`OPENCLAW_*` vars in `pipeline.env.example`) and the always-on **`billy.maic`
VPS** (remote host; see issue **#15**). Both replace the host crontab; neither
is required for 1.0.

---

## Inspect mid-flight

Three rails let you watch or freeze a tick without running it end-to-end. All
are offline/dry-run-safe.

- **`DISPATCH_ARTIFACTS_DIR`** — when set, every stage dumps its artifact under
  `${DISPATCH_ARTIFACTS_DIR}/<tick-id>/`: `workorder.txt` (the rendered work
  order), `job-request.json` (what the engineer is handed), and `invoice.json`
  (what the engineer returned). The tick id is minted once per tick
  (`tick-<UTC-timestamp>`).

- **`--until <stage>`** — halt the tick *after* `<stage>` completes, exit 0, and
  leave the dumped artifacts on disk. Example — render and dump the work order,
  then stop before the engineer:

  ```bash
  DISPATCH_ARTIFACTS_DIR=./.artifacts \
    scripts/pipeline.sh --until workorder --fixture scripts/fixtures/queued-issues.json
  # → leaves ./.artifacts/<tick-id>/workorder.txt ; no invoice.json (engineer did not run)
  ```

- **`--from <stage> --artifact <file>`** — resume *at* `<stage>`, injecting a
  previously-captured artifact as that stage's input and skipping every earlier
  stage (no intake, no re-classification). `--from` requires `--artifact`; the
  file is validated against its schema as data and never executed. Valid
  resume stages: `engineer` (input: a Job Request), `intake-invoice` / `closure`
  (input: an Invoice). Combined with `--until`, this runs exactly one stage:

  ```bash
  # Replay only the engineer on a captured Job Request:
  DISPATCH_ARTIFACTS_DIR=./.artifacts scripts/pipeline.sh \
    --from engineer --artifact ./.artifacts/<tick-id>/job-request.json --until engineer
  ```

  Replay is deterministic and writes its own artifacts under a fresh tick dir,
  never clobbering the source file.

---

## Digest & ledger

Two local-file, never-`gh` records back the monitoring surface:

- The **run-ledger** (`DISPATCH_LEDGER_FILE`, default
  `${DISPATCH_ARTIFACTS_DIR:-./.artifacts}/run-ledger.jsonl`) — append-only
  JSONL, one line per stage transition with `tick_id`, `issue`, `stage`,
  `cost.*` (`tokens_in`, `tokens_out`, `duration_seconds`, `model`),
  `label_before` / `label_after`, `timestamp`, and `dry_run`.
- The **run-record / heartbeat** (`DISPATCH_RUN_RECORD`) — one `event=started`
  and one `event=ended` line per tick (a `started` with no matching `ended`
  is the stuck-tick signal the reaper keys off).

Render the operator **digest** from the ledger (read-only, offline — never
claims, mutates, or calls a model):

```bash
./entrypoint.sh report --format markdown            # human digest to stdout
./entrypoint.sh report --ledger ./.artifacts/run-ledger.jsonl --limit 20
```

`report` also accepts `--format text` and `--comment` (post the digest as a
GitHub comment — a live action). Ad-hoc query the ledger directly, e.g. every
closure transition and its token cost:

```bash
jq -r 'select(.stage=="closure") | "#\(.issue) \(.label_before)->\(.label_after) tokens_out=\(.cost.tokens_out)"' \
  ./.artifacts/run-ledger.jsonl
```

---

## Budget (plan tier + soft-cap)

The budget guard (E5) reconciles each tick against the Claude Code usage window
and **auto-flips the tick to dry-run** when the window is too close to the plan
cap. The plan tier, the resolved 5-hour-window token limit, and the soft-cap
fraction are committed in **`src/tuning.json`** under `budget.window`
(`plan_tier`, `window_token_limit`, `soft_cap_fraction`); point at a different
file with the `DISPATCH_TUNING_FILE` env var.

Per-5h-window token caps by tier (`budget.plan_limits`):

| Plan tier | Per-5h-window token cap |
|-----------|-------------------------|
| `pro`     | 44,000                  |
| `max5`    | 88,000                  |
| `max20`   | 220,000                 |
| `custom`  | `custom_limit_tokens`, else the **P90 of the last 192h** of actual usage |

The committed default is `max20` / `220000` / soft-cap `0.80` — i.e. a tick
auto-throttles to dry-run once the rolling window passes 80% of 220k tokens.

Per decision **D2**, the pipeline does **not** re-implement usage tracking:
the oracle (`src/budget/oracle.py`) consumes **`claude-monitor`**
(`pip install claude-monitor`) as the external usage-limit truth source, while
the dispatch run-ledger is only per-job attribution. For offline testing,
`BUDGET_ORACLE_FIXTURE` points the oracle at a window-state JSON instead of
`claude-monitor` (no subprocess, no network); `CLAUDE_MONITOR_BIN` overrides the
monitor binary name.

---

## Recover a stuck issue (reaper)

An issue gets **stuck in `claimed`** when an engineer/dispatch crash strands it
with no Invoice and no open PR. The crash reaper (E1-3) recovers it: at the top
of **every** tick — inside the tick lock, before any new claim — it re-queues
issues stuck in `claimed` past the timeout **with no open PR** (`claimed →
queued` plus a provenance comment). It is recovery only (decision **D1**): it
never opens, escalates, or re-dispatches, and an issue with an open PR is left
alone as in-flight work.

- **Timeout:** `recovery.reaper_timeout_hours` in `src/tuning.json`
  (committed default: **4** hours). Override per-run with
  `DISPATCH_CLAIM_TIMEOUT_HOURS`.
- **Disable:** `DISPATCH_REAPER_ENABLED=0` skips the reaper step.
- **Engineer-failure policy:** `recovery.engineer_failure_policy` is
  `architect-rescaffold` — on an engineer non-zero exit / `failed` status, the
  issue is re-submitted to the architect for a fresh (possibly finer-grained)
  work order rather than retrying the identical order or escalating immediately;
  `needs-human` only if re-scaffolding yields no workable plan.

Because the reaper runs first in every tick, the **manual recovery command is
just a tick** — preview which stuck issues would be re-queued without mutating
anything by running it in dry-run:

```bash
PIPELINE_DRY_RUN=1 ./entrypoint.sh --repo owner/your-repo
# reaper logs: "reaper: inspecting N claimed issue(s) (timeout 4h)" and the
# claimed -> queued recovery it WOULD perform. Drop PIPELINE_DRY_RUN=1 to apply.
```

---

## See also

- `scripts/smoke.sh` **§7.26** — the automated proof that lock + heartbeat +
  ledger + artifacts + soft-cap compose (this runbook's machine companion).
- `pipeline.env.example` — the canonical, commented env surface.
- Issue **#15** — `billy.maic` remote deployment (schedule alternative).
- Issue **#16** — CI / smoke that the `§7.27` doc-consistency check runs under.
