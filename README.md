# dispatch

**dispatch** selects engineering jobs from a GitHub Issues queue and runs each
through a **Workflow** — a Controller that drives Actions across a
`spec → work → build` cycle — then processes the Invoice it returns, closing the
loop or escalating based on outcome.

Three responsibilities, nothing more:

1. **Issue intake** — fetch and classify queued issues, claim one at a time.
2. **Job issuance** — the Workflow's Architect phase composes a structured Job
   Request (issue, route, scope); the Engineering phase runs it as an Action —
   the engineer (`ENGINEER_BIN`).
3. **Work plan approval** — receive the engineer's Invoice; on success arm
   auto-merge (awaiting human approval); on failure advance the fix-attempt
   ladder; on ambiguity escalate to the operator via a GitHub comment.

> Operate it unattended: [`RUNBOOK.md`](./RUNBOOK.md) ·
> Design & roadmap: [`doc/specs/v1-unattended-repo-monitoring/`](./doc/specs/v1-unattended-repo-monitoring/) ·
> Build decisions: [`OPEN-QUESTIONS.md`](./OPEN-QUESTIONS.md) ·
> Worker contract: [`CLAUDE.md`](./CLAUDE.md) ·
> Invoice schema: [`schemas/invoice.json`](./schemas/invoice.json) ·
> Job Request schema: [`schemas/job-request.json`](./schemas/job-request.json)

## Roles

dispatch runs one **Workflow** (`BaseWorkflow`) per work unit — a top-level
**Controller** whose phases drive **Actions** over a `spec → work → build` cycle:

| Phase | What it does | Actions |
|-------|-------------|---------|
| **Architect** | classify → decompose → compose the Job Request / work order | `Procedure` + `Inference` |
| **Engineering** | execute the work order in a git worktree, return an Invoice | the engineer (`ENGINEER_BIN`), wrapped as an `Inference` |
| **Admin** | store results, update docs, run adversarial review, approve or escalate | `Procedure` + `Inference` |

The engineer is decoupled from the workflow: `ENGINEER_BIN` can be any binary
that reads a Job Request JSON and writes an Invoice JSON to stdout.

## Flow

> **Engine status.** `BaseWorkflow` (the `engine/` + `app/config/` automata
> substrate) is the authoring engine for the workorder stage. On every tick the
> visitor's `visit_workorder` runs a BaseWorkflow (via
> `src/orchestration/baseworkflow_bridge.py`) over the visitor-built Job Request
> and folds the authored `orchestration_script` / `work_plan` into it before the
> engineer (`engineer_sdk.py`) consumes it — fail-safe, falling back to the
> original request on any error. The historical `DISPATCH_ENGINE=visitor`
> authoring fallback (skip BaseWorkflow, run the pure-visitor path unchanged) is
> **deprecated and disabled for release** — it is no longer selectable; an
> explicit `DISPATCH_ENGINE=visitor` is ignored with a warning and BaseWorkflow
> runs anyway.
> The GitHub lifecycle of every tick (claim → labels → PR → closure) is still
> owned by the `src/orchestration` visitor pipeline
> (`src/orchestration/pipeline.py`), and the engineer is still `engineer_sdk.py`;
> BaseWorkflow authors the spec/orchestration_script that feeds the engineer, it
> does not own the lifecycle or replace the engineer. Read the diagram below as
> the architecture of the default authoring path.

```
GitHub Issues (queued)
        │
        ▼
  [INTAKE] fetch → classify → claim one issue (queued → claimed)
        │
        ▼ Job Request {issue, route, scope, confidence}
  [WORKFLOW] BaseWorkflow — a Controller driving Actions (spec → work → build)
    Architect (compose work order) → Engineering (execute) → Admin (store · docs · review)
        │
        ▼ Invoice {status, pr_number, cost, summary, …}
  [APPROVAL]
    completed   → post summary + arm auto-merge (human approves to merge)
    failed      → fix-attempt ladder (up to 3 attempts)
    needs-human → label + comment on the issue for the operator
```

All durable state lives in **GitHub** (issues, labels, PRs, comments). Within a
run, the workflow's **Shelves** (`input` / `deliverables` / `shared`) hold the
working state; GitHub is the source of truth across runs.

---

## Architecture

Under the operational pipeline, the dispatch **engine** models work as
**Controllers** — control structures whose body is Sequences and Loops over
**Actions**:

| Action | Role |
|--------|------|
| `Procedure` | deterministic leaf (milliseconds) |
| `Inference` | agent leaf — a Claude Agent SDK call (seconds–minutes) |
| `Program` | a nested Controller — the sole recursion point |

Each Controller carries three **Shelves** (`input`, `deliverables`, `shared`),
and **Governors** (decorators) enforce budget, permission (deny-by-default), and
iteration caps over every Action uniformly. `BaseController` fixes a
`spec → work → build` phase skeleton; `BaseWorkflow` fills it with the
Architect/Admin/Engineering cycle, where the Architect emits a serialized
orchestration script that the Admin deserializes and runs.

**Control-flow formalism — Harel statecharts (HFSM).** Per
[ADR-001](docs/adr/001-hfsm-automata.md), controller control flow and
inter-controller coordination are an HFSM (hierarchy + orthogonality + broadcast
+ history + guards, under run-to-completion semantics):

- **Externalized transitions** — `(event, guard, source, target, action)`
  objects, not control flow buried in a Controller body.
- **A single run-to-completion (RTC) interpreter** owns the agenda over a
  **serializable active configuration**; Controllers never invoke one another
  directly.
- **Addressable state IDs** so any transition can target any Controller/Action.
- **Deep history (H\*)** → resume a crashed run from its deepest active position
  (the Issues-as-durable-state-machine bias).
- **Orthogonal regions** → `parallel` phases; **broadcast events + `in(state)`
  guards** → future peer communication.

The serialized statechart (SCXML / UML-aligned) doubles as the Architect's
orchestration script. v1 **builds the seams** (externalized transitions,
addressable IDs, RTC loop, a reserved event queue) while the broadcast bus and
cross-region guards stay **stubbed** until multiple Controllers run
concurrently. Full rationale, the options weighed (plain call stack, flat FSM,
behavior tree), and action items live in
[`docs/adr/001-hfsm-automata.md`](docs/adr/001-hfsm-automata.md).

---

## Usage

```bash
bash entrypoint.sh [OPTIONS]          # run one tick (intake → engineer → approval)

  -b, --bootstrap         provision pipeline labels on PIPELINE_REPO first
  -r, --repo  owner/repo  target repo
  -l, --live              PIPELINE_DRY_RUN=0 — mutate GitHub (default: dry-run)
  -e, --engineer  BIN     Engineer binary (required in live mode)
  -f, --fixture   FILE    offline issue list JSON
  -u, --until     STAGE   halt AFTER <stage> (intake|workorder|prep|engineer|intake-invoice|closure)
      --from      STAGE   resume AT <stage>, replaying --artifact as its input
  -a, --artifact  FILE    captured Job Request / Invoice fed to --from
  -h, --help

bash entrypoint.sh report [OPTIONS]   # render the operator digest from the run-ledger
```

`entrypoint.sh` wires the full cycle — intake → engineer → approval — in one
command. (`scripts/pipeline.sh` is the same thing; each component script
remains independently runnable.) `entrypoint.sh report` is the read-only,
offline digest renderer. The `DISPATCH_ARTIFACTS_DIR` / `--until` / `--from`
debug rails and the cron/budget/reaper operations are documented end-to-end in
[`RUNBOOK.md`](./RUNBOOK.md).

### Quickstart for a new repo

```bash
# 1. Copy and fill secrets
cp pipeline.env.example pipeline.env
# edit pipeline.env: set PIPELINE_REPO, ENGINEER_BIN, tokens

# 2. Dry-run preview — prints what would happen, mutates nothing
bash entrypoint.sh --repo owner/target-repo

# 3. Provision labels, then run live
bash entrypoint.sh --bootstrap --repo owner/target-repo --live --engineer /path/to/engineer
```

The only prerequisite on the target repo: add the `queued` label to any issue
you want the pipeline to pick up. `--bootstrap` creates the full label
vocabulary (idempotent).

### Offline / fixture testing (no GitHub)

```bash
# Fully offline with the mock engineer and fixture issues:
bash entrypoint.sh --fixture scripts/fixtures/queued-issues.json
```

### Reusable demo testing (snapshot → mutate → replay)

`scripts/demo/` exercises the pipeline against a representative web-dev backlog
entirely offline — no GitHub, no model, no mutations — via a **snapshot → mutate
→ replay** loop against `ReclaimByDesign/demo-repository`:

1. **`snapshot.sh`** captures the live demo queue (read-only) into the committed
   `scripts/demo/snapshots/demo-current.json` (a hand-built sample ships, so the
   loop works before the live repo is seeded).
2. **`mutate.py`** resolves that base snapshot **+ an overlay scenario** into a
   fully-resolved fixture (deterministic, never mutates the base).
3. **`replay.sh`** runs one offline dry-run tick through `./dispatch --fixture`
   and dumps the round's artifacts under the gitignored `rounds/` tree.

```bash
# Replay a committed scenario (writes scripts/demo/rounds/<scenario>/<n>/):
scripts/demo/replay.sh feature-with-acceptance
# Reset a scenario's transient rounds (committed files untouched):
scripts/demo/replay.sh --reset feature-with-acceptance
```

The committed scenario catalog (clear bug, well-specified feature, oversized
refactor, vague defer, decline-shaped ask) and its mechanics are documented in
[`scripts/demo/README.md`](scripts/demo/README.md).

---

## Engine

dispatch ships a small **workflow engine** — controllers, actions, and a
run-to-completion interpreter — under `engine/`, driven by declarative YAML
under `app/config/`.

| Concern | Where |
|---------|-------|
| Workflow / controller runtime | `engine/workflow/` (controller · loader · visitor · registry) |
| Actions, governors, interpreter | `engine/actions/` (action · control · governor · interpreter · shelf · statechart) |
| Workflow definitions | `app/workflows/*.yml` (`baseworkflow.yml`, `websitewf.yml`, `engineer.yml`) + `app/config/actions/**` (one file per Action) |
| Agent definitions | `app/agents/*.yaml` (architect · coder · reviewer · tester · security-architect) |
| Model routing | `engine/models.py` + `app/config/models.yml` (LiteLLM · Anthropic · HF · CLI) |
| Issue classification | deterministic keyword classifier (`src/classifier/classify.py`) |
| Queue + state machine | GitHub Issues + labels |
| Job execution | the engineer Action (`ENGINEER_BIN` — any Job Request → Invoice binary) |
| CI gate | GitHub Actions (`.github/workflows/ci.yml`) |

**Known limitations (live-engine migration, Phase 2).** The workflow engine is
mid-migration onto the live tick (`run_live`); two gaps are tracked here rather
than as issues:

- **`needs-human` derivation.** `admin:intake_invoice` derives the invoice
  status from the engineer's `engineering_result` (`{ok, value, meta}`). The
  bare shape only expresses `completed`/`partial`/`failed`; **`needs-human` is
  reachable only via an explicit `meta.status`**. A richer Engineer that sets
  `meta.status` drives the escalation transition; until then the derived mapping
  cannot.
- **Permission enforcement for `procedure` actions.** The `PermissionGovernor`
  gates on an action's required tool set, which a `procedure` does not expose —
  so a side-effecting procedure's declared `permission:` list (e.g.
  `admin:intake_invoice`'s `gh:*` capabilities) is **auditable metadata, not yet
  enforced**. Real safety on the gh-write path is the **`ctx.dry_run` gate** in
  the binding (no network under dry-run). Procedure-level capability enforcement
  is future work.

## Label state machine

`queued → claimed → pr-open → in-review → done-pending-merge → done`

Escalation flags: `needs-human`, `wont-do`, `duplicate`, `fix-attempt-1..3`.
Each transition has exactly one owner, documented in the relevant script header.

## Safety

`PIPELINE_DRY_RUN=1` is the default. Every script prints what it *would* do
and mutates nothing. Flip to `0` only after completing setup.

```bash
# See exactly what dispatch would do against fixtures, mutating nothing:
PIPELINE_FIXTURE_ISSUES=scripts/fixtures/queued-issues.json bash scripts/dispatch.sh
```

## Verify

```bash
bash scripts/smoke.sh
```

Offline + dry-run acceptance runner; exit 0 = all assertions pass. No network,
no GitHub, no model calls. This is also what CI runs.

## Inspecting a tick (`DISPATCH_ARTIFACTS_DIR`)

Set `DISPATCH_ARTIFACTS_DIR` to capture a tick's intermediate state for
inspection (the substrate the `--until` / replay debug tooling reads). Each
tick writes its artifacts under a tick-scoped subdirectory:

```
$DISPATCH_ARTIFACTS_DIR/
├── issue-dag.json          # static dependency view (also from ./dispatch --dag)
├── issue-dag.md            # Mermaid + table, human-browsable
└── <tick-id>/              # one dir per tick (tick-<UTC>, or a threaded id)
    ├── workorder.txt       # the rendered work order (WORK PLAN …)
    ├── job-request.json    # the Job Request handed to the Engineer (schemas/job-request.json)
    └── invoice.json        # the Invoice the Engineer returned (live ticks)
```

```bash
# Dump one tick's work order + job request from the offline fixture queue:
DISPATCH_ARTIFACTS_DIR=./artifacts ./dispatch --fixture src/architect/fixtures/play-queue.json
ls ./artifacts/tick-*/
```

Dumping is a pure side-effect: gated on `DISPATCH_ARTIFACTS_DIR` being set, it
never changes stdout or GitHub state and runs under `PIPELINE_DRY_RUN=1` too
(observability, not a mutation). When unset, behaviour is unchanged.
`pipeline.sh` threads one `DISPATCH_TICK_ID` across a tick so every stage's
artifacts share the same `<tick-id>/` directory.

## Scheduling unattended ticks

The recommended 1.0 prototype scheduler is a plain host **`crontab`** (Linux) or
**`launchd`** (macOS) entry that runs `entrypoint.sh` a few times a day. Committed
examples: [`examples/dispatch.crontab`](examples/dispatch.crontab) and
[`examples/dispatch.launchd.plist`](examples/dispatch.launchd.plist) — edit the
paths and repo, then install:

```bash
# Linux: every 2 hours (12x/day) at 00:07, 02:07, 04:07, … 22:07
crontab examples/dispatch.crontab

# macOS:
cp examples/dispatch.launchd.plist ~/Library/LaunchAgents/com.dispatch.tick.plist
launchctl load ~/Library/LaunchAgents/com.dispatch.tick.plist
```

Both examples:

- set `PIPELINE_REPO` and `PIPELINE_DRY_RUN` **explicitly** — **dry-run is the
  default**, so a tick only mutates GitHub once you deliberately set
  `PIPELINE_DRY_RUN=0`. A copy-paste never goes live by accident.
- redirect output to a log file (`log()` writes to stderr); the per-tick run
  record (E1-2, `DISPATCH_RUN_RECORD`) adds a structured start/end heartbeat.
- rely on the **in-process `flock`** the pipeline ships (E1-1,
  `DISPATCH_LOCK_FILE`): an overlapping tick is safely serialized — it logs and
  exits 0 without double-claiming. The crontab example adds an outer `flock -n`
  as a belt-and-suspenders guard.

**Upgrade paths** (not the 1.0 default):

- **(a) OpenClaw Gateway** — adds webhook receive + operator chat I/O on top of
  scheduling; adopt when you want operator notifications in the loop.
- **(c) `billy.maic` VPS** — an always-on remote host for the schedule; see
  existing issue #15 (remote deployment) rather than duplicating it here.

## Digest, budget & recovery

Three rails make an unattended run observable and self-protecting. Each is
local-file / offline and configured in `pipeline.env` or `app/config/tuning.yml`;
[`RUNBOOK.md`](./RUNBOOK.md) is the operator walkthrough for all three.

- **Run-ledger + digest.** Every stage transition appends one JSONL line to the
  run-ledger (`DISPATCH_LEDGER_FILE`: `tick_id`, `issue`, `stage`, `cost.*`,
  `label_before/after`, …). Render the operator digest with `entrypoint.sh
  report` (read-only; never claims or calls a model).
- **Budget soft-cap.** The guard reconciles each tick against the Claude Code
  usage window and **auto-flips it to dry-run** past the soft-cap. Plan tier,
  the 5h window limit, and the soft-cap fraction are committed in
  `app/config/tuning.yml` (`budget.window`); `claude-monitor` is the usage oracle
  (decision D2), and the ledger is per-job attribution.
- **Crash reaper.** At the top of every tick the reaper re-queues issues stranded
  in `claimed` past the timeout with no open PR (`recovery.reaper_timeout_hours`,
  default 4h; `recovery.engineer_failure_policy = architect-rescaffold`).

Research-mode work orders (the architect emits a `docs/research/<topic>.md`
deliverable when an issue is under-specified) round out the five 1.0 capabilities.

## Layout

```
entrypoint.sh            ← run one tick (intake → workflow → approval)
dispatch · dispatch.py   ← workflow / work-order entrypoints (./dispatch --fixture …)
RUNBOOK.md               operator guide: schedule · inspect · digest · budget · reaper
schemas/                 invoice.json · job-request.json
engine/                  the dispatch workflow engine
  workflow/              controller · loader · visitor · registry
  actions/               action · control · governor · interpreter · shelf · statechart
  models.py · proc.py · filesys.py · runtime.py
app/workflows/           baseworkflow.yml · websitewf.yml · engineer.yml  (workflow definitions)
app/agents/              architect · coder · reviewer · tester · security-architect (*.yaml)
app/config/              actions/** · models.yml · state_machine.yml · tuning.yml · adversary.yml …
src/baseworkflow/        baseworkflow.py — loads app/workflows into a Controller · bindings/  [WORKFLOW]
src/tuning.py            shared tuning surface (used by both the engine and the visitor lineage)
src/visitor/             the legacy visitor-pattern lineage (non-workflow), consolidated:  [VISITOR]
  orchestration/         StageVisitor tick driver (visitors · stages · statemachine · pipeline …)
  architect/             work-order generation (decompose · strategy · verify · resources …)
  classifier/            classify.py (deterministic keyword triage)
  budget/                oracle · guard · reconcile (soft-cap)
  intake/                intake.py · pipeline.py · ranker.py
  ledger/ · reports/     run-ledger · reporting
scripts/                 claude-engineer.sh · mock-engineer.sh · smoke.sh · debug-classify.sh
examples/                dispatch.crontab · dispatch.launchd.plist (schedulers)
.github/workflows/       ci.yml
```

## Setup (first time)

1. `cp pipeline.env.example pipeline.env` — fill secrets (gitignored).
2. Set `ENGINEER_BIN` in `pipeline.env` to your engineer binary.
3. `bash scripts/bootstrap-labels.sh` (requires `gh` authed, `PIPELINE_DRY_RUN=0`).
4. Configure branch protection on `main`: require CI + one human review; no
   token may merge directly. This is what auto-merge waits on.
5. Verify with `bash scripts/smoke.sh`, then set `PIPELINE_DRY_RUN=0`.
