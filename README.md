# dispatch — Unattended Engineering Pipeline (v0)

An unattended pipeline that intakes engineering jobs (GitHub Issues), does
**one unit of work per session** (issue → branch → PR), verifies via CI +
adversarial review, iterates on failures with fresh stateless sessions, updates
docs, and closes the job — notifying the operator on chat. **All state lives in
GitHub** (issues, labels, PRs, comments); sessions are stateless workers.

> Full design: [`HANDOFF-pipeline-v0.md`](./HANDOFF-pipeline-v0.md).
> Build decisions / what a human must supply:
> [`OPEN-QUESTIONS.md`](./OPEN-QUESTIONS.md).
> Worker contract: [`CLAUDE.md`](./CLAUDE.md).

## Principle: assemble, don't invent

Every stage rides an existing engine — nothing here is a custom daemon, queue,
or notifier.

| Concern | Engine |
|---|---|
| Schedule / webhooks / sessions | **OpenClaw Gateway** (`scripts/bootstrap-openclaw.sh`) |
| Queue + state machine | **GitHub Issues + labels** (`scripts/bootstrap-labels.sh`) |
| Per-task orchestration | **Claude Code dynamic workflows** (`.claude/workflows/*.js`) |
| Deterministic gate | **GitHub Actions CI** (`.github/workflows/ci.yml`) |
| Adversarial review | **claude-code-action@v1** (`.github/workflows/review.yml`) |
| Model routing | **LiteLLM proxy** (`config/litellm.pipeline.yaml`) |
| Issue triage (v0) | **HF Inference API** (`services/classifier/classify.py`) |
| Operator I/O | **OpenClaw channels** |

## Flow

```
chat/issue ─► [DISPATCH] dispatch.sh (cron */10m, command job)
                 classify ─► label + route comment ─► claim 1 issue
              ─► [IMPLEMENT] /implement-task in a worktree ─► opens PR
              ─► [TEST LOOP] CI gate + advisory review
                   fail ─► webhook ─► fix-dispatch.sh ─► /fix-ci @ ladder tier
              ─► [CLOSURE] CI green + approved ─► closure.sh
                   /update-docs ─► arm auto-merge ─► (human tap) ─► done
```

## Label state machine

`queued → claimed → pr-open → in-review → docs-pending → done-pending-merge → done`
plus `needs-human`, `wont-do`, `duplicate`, `fix-attempt-1..3`. Each transition
has exactly one owner, documented in the relevant script header.

## Safety first (dry-run defaults ON)

`PIPELINE_DRY_RUN=1` is the default (`scripts/lib/common.sh`). Every script
prints the `gh`/`git`/`claude`/`openclaw` calls it *would* make and mutates
nothing. Flip to `0` only after the one-time setup in OPEN-QUESTIONS.md §D.

```bash
# See exactly what dispatch would do, against fixtures, mutating nothing:
PIPELINE_FIXTURE_ISSUES=scripts/fixtures/queued-issues.json scripts/dispatch.sh
```

## Verify the prototype

```bash
bash scripts/smoke.sh     # offline + dry-run acceptance runner; exit 0 = pass
```

`smoke.sh` asserts all of HANDOFF §7 (1–9) plus checkable §8 security
guardrails. It runs with no network, no GitHub, no Gateway, and no model calls;
checks that need an absent engine (`openclaw`/`litellm`/`gh`) degrade to a
structural/dry-run proxy and report a SKIP for the live portion. This is also
what `.github/workflows/ci.yml` runs as the blocking gate.

## Layout

```
HANDOFF-pipeline-v0.md   OPEN-QUESTIONS.md   CLAUDE.md   pipeline.env.example
config/litellm.pipeline.yaml
services/classifier/     classify.py · classify_local_stub.py · fixtures/
scripts/                 bootstrap-labels.sh · bootstrap-openclaw.sh ·
                         dispatch.sh · fix-dispatch.sh · closure.sh ·
                         smoke.sh · lib/common.sh · fixtures/
.claude/workflows/       implement-task.js · fix-ci.js · update-docs.js
.github/workflows/       ci.yml · review.yml · notify-openclaw.yml
```

## Setup (summary; details in OPEN-QUESTIONS.md §C/§D)

1. `cp pipeline.env.example pipeline.env` and fill secrets (never committed).
2. `bash scripts/bootstrap-labels.sh` (gh authed).
3. Configure branch protection on `main` (require CI + 1 review; no token may
   merge) — this is what auto-merge waits on.
4. `bash scripts/bootstrap-openclaw.sh` on the Gateway host.
5. Point the LiteLLM proxy at `config/litellm.pipeline.yaml`.
6. Keep dry-run on until verified, then set `PIPELINE_DRY_RUN=0`.
