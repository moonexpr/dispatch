# dispatch

**dispatch** selects engineering jobs from a GitHub Issues queue, issues them
to a ruflo engineer agent, and processes the Invoice the agent returns —
closing the loop or escalating based on outcome.

Three responsibilities, nothing more:

1. **Issue intake** — fetch and classify queued issues via `gh-intake.sh`,
   store them in the ruflo task queue, claim one at a time.
2. **Job issuance** — the ruflo swarm coordinator hands a structured Job
   Request to an engineer agent with the issue, route, and scope.
3. **Work plan approval** — receive the agent's Invoice; on success arm
   auto-merge (awaiting human approval); on failure advance the fix-attempt
   ladder; on ambiguity escalate to the operator via a GitHub comment.

> Full design: [`HANDOFF-pipeline-v0.md`](./HANDOFF-pipeline-v0.md) ·
> Build decisions: [`OPEN-QUESTIONS.md`](./OPEN-QUESTIONS.md) ·
> Worker contract: [`CLAUDE.md`](./CLAUDE.md) ·
> Invoice schema: [`schemas/invoice.json`](./schemas/invoice.json) ·
> Job Request schema: [`schemas/job-request.json`](./schemas/job-request.json)

## Roles

| Role | What it does | Backed by |
|------|-------------|-----------|
| **Coordinator** | Intake → Job Request → Invoice review | ruflo swarm (hierarchical-mesh) |
| **Engineer** | Executes the job in a git worktree, returns an Invoice | ruflo engineer agent (`ENGINEER_BIN`) |

The two roles are fully decoupled. `ENGINEER_BIN` can be any binary that reads
a Job Request JSON and writes an Invoice JSON to stdout; the default is a ruflo
engineer agent spawned by the coordinator via the claude-flow MCP.

## Flow

```
GitHub Issues (queued)
        │
        ▼
  [INTAKE] gh-intake.sh → intake-to-ruflo bridge
    fetch → normalize → store in ruflo task queue
        │
        ▼
  [COORDINATOR] ruflo hierarchical swarm
    classify → route → claim one issue (SubagentStart hook: queued → claimed)
        │
        ▼ Job Request {issue, route, scope, confidence}
  [ENGINEER] ruflo engineer agent
    implement → test → open PR
        │
        ▼ Invoice {status, pr_number, cost, summary, …}
  [APPROVAL] SubagentStop hook → architect-intake.sh
    completed   → post summary + arm auto-merge (human approves to merge)
    failed      → fix-attempt ladder (fix-dispatch.sh, up to 3 attempts)
    needs-human → label + comment on the issue for the operator
```

All durable state lives in **GitHub** (issues, labels, PRs, comments).
Ruflo provides session memory and swarm coordination; GitHub is the
source of truth across runs.

---

## Usage

```bash
bash entrypoint.sh [OPTIONS]

  -b, --bootstrap         provision pipeline labels on PIPELINE_REPO first
  -r, --repo  owner/repo  target repo
  -l, --live              PIPELINE_DRY_RUN=0 — mutate GitHub (default: dry-run)
  -e, --engineer  BIN     Engineer binary (required in live mode)
  -f, --fixture   FILE    offline issue list JSON
  -h, --help
```

`entrypoint.sh` wires the full cycle — intake → engineer → approval — in one
command. (`scripts/pipeline.sh` is the same thing; each component script
remains independently runnable.)

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

---

## Engines

dispatch assembles existing engines; it builds nothing custom.

| Concern | Engine |
|---------|--------|
| Swarm coordination | ruflo V3 (hierarchical-mesh, up to 15 agents, `.claude-flow/config.yaml`) |
| Session memory | ruflo hybrid memory (HNSW + file, `.claude-flow/data/`) |
| Hook lifecycle | ruflo hooks in `.claude/settings.json` (SubagentStart/Stop, PostToolUse) |
| Issue intake | `scripts/gh-intake.sh` → `scripts/intake-to-ruflo.sh` bridge |
| Issue classification | Deterministic keyword classifier (`services/classifier/classify.py`) |
| Queue + state machine | GitHub Issues + labels (`scripts/bootstrap-labels.sh`) |
| Job execution | ruflo engineer agent (or any binary speaking Job Request / Invoice JSON) |
| CI gate | GitHub Actions (`.github/workflows/ci.yml`) |

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

## Layout

```
entrypoint.sh            ← start here (ruflo swarm bootstrap)
schemas/                 invoice.json · job-request.json
services/intake/         intake.py · pipeline.py · ranker.py
services/models/         models.py (LiteLLM / Anthropic / HF / CLI)
scripts/                 gh-intake.sh · intake-to-ruflo.sh (bridge)
                         pipeline.sh · dispatch.sh · fix-dispatch.sh · closure.sh
                         architect-intake.sh · mock-engineer.sh
                         bootstrap-labels.sh · deploy-remote.sh
                         smoke.sh · lib/common.sh · fixtures/
.claude/                 settings.json (hooks) · agents/ · skills/ · helpers/
.claude-flow/            config.yaml · data/ · logs/ · sessions/
.mcp.json                claude-flow MCP config
.github/workflows/       ci.yml
```

## Setup (first time)

1. `cp pipeline.env.example pipeline.env` — fill secrets (gitignored).
2. Set `ENGINEER_BIN` in `pipeline.env` to your engineer binary.
3. `bash scripts/bootstrap-labels.sh` (requires `gh` authed, `PIPELINE_DRY_RUN=0`).
4. Configure branch protection on `main`: require CI + one human review; no
   token may merge directly. This is what auto-merge waits on.
5. Verify with `bash scripts/smoke.sh`, then set `PIPELINE_DRY_RUN=0`.
