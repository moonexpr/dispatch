# dispatch

**dispatch** selects engineering jobs from a GitHub Issues queue, issues them
to an Engineer, and processes the Invoice the Engineer returns — closing the
loop or escalating based on outcome.

Three responsibilities, nothing more:

1. **Issue selection** — classify queued issues, route each to the right model
   tier, claim one at a time.
2. **Job issuance** — hand a structured Job Request to the Engineer (currently
   [Ruflo](https://github.com/ruvnet/ruflo)) with the issue, route, and scope.
3. **Invoice processing** — receive the Engineer's Invoice; on success arm
   auto-merge; on failure escalate through the fix ladder; on ambiguity return
   the issue to the operator.

> Full design: [`HANDOFF-pipeline-v0.md`](./HANDOFF-pipeline-v0.md) ·
> Build decisions: [`OPEN-QUESTIONS.md`](./OPEN-QUESTIONS.md) ·
> Worker contract: [`CLAUDE.md`](./CLAUDE.md) ·
> Invoice schema: [`schemas/invoice.json`](./schemas/invoice.json)

## Roles

| Role | What it does | Backed by |
|------|-------------|-----------|
| **Architect** (this repo) | Issue selection → Job Request → Invoice intake | dispatch scripts + Claude |
| **Engineer** | Executes the job, returns an Invoice | [Ruflo](https://github.com/ruvnet/ruflo) |

## Flow

```
GitHub Issues (queued)
        │
        ▼
  [SELECTION] dispatch.sh
    classify → route → claim one issue
        │
        ▼ Job Request {issue, route, scope}
  [ENGINEER] Ruflo
    implement → test → open PR
        │
        ▼ Invoice {status, pr_number, cost, summary, …}
  [INTAKE] architect-intake
    completed  → arm auto-merge → done
    failed     → fix-dispatch ladder (up to 3 attempts)
    needs-human → escalate to operator
```

All state lives in **GitHub** (issues, labels, PRs, comments). Sessions are
stateless — there is no shared memory between runs.

## Engines

dispatch assembles existing engines; it builds nothing custom.

| Concern | Engine |
|---------|--------|
| Schedule / webhooks | OpenClaw Gateway (`scripts/bootstrap-openclaw.sh`) |
| Queue + state machine | GitHub Issues + labels (`scripts/bootstrap-labels.sh`) |
| Issue classification | HF Inference API (`services/classifier/classify.py`) |
| Job execution | Ruflo / Engineer |
| Per-task orchestration | Claude Code workflows (`.claude/workflows/*.js`) |
| CI gate | GitHub Actions (`.github/workflows/ci.yml`) |
| Adversarial review | claude-code-action@v1 (`.github/workflows/review.yml`) |
| Model routing | LiteLLM proxy (`config/litellm.pipeline.yaml`) |
| Operator I/O | OpenClaw channels |

## Label state machine

`queued → claimed → pr-open → in-review → docs-pending → done-pending-merge → done`

Escalation flags: `needs-human`, `wont-do`, `duplicate`, `fix-attempt-1..3`.
Each transition has exactly one owner, documented in the relevant script header.

## Safety

`PIPELINE_DRY_RUN=1` is the default. Every script prints what it *would* do
and mutates nothing. Flip to `0` only after completing setup (§D of
OPEN-QUESTIONS.md).

```bash
# See exactly what dispatch would do against fixtures, mutating nothing:
PIPELINE_FIXTURE_ISSUES=scripts/fixtures/queued-issues.json scripts/dispatch.sh
```

## Verify

```bash
bash scripts/smoke.sh
```

Offline + dry-run acceptance runner; exit 0 = all assertions pass. No network,
no GitHub, no Gateway, no model calls. This is also what CI runs.

## Layout

```
schemas/                 invoice.json
config/                  litellm.pipeline.yaml
services/classifier/     classify.py · fixtures/
scripts/                 dispatch.sh · fix-dispatch.sh · closure.sh
                         bootstrap-labels.sh · bootstrap-openclaw.sh
                         smoke.sh · lib/common.sh · fixtures/
.claude/workflows/       implement-task.js · fix-ci.js · update-docs.js
.github/workflows/       ci.yml · review.yml · notify-openclaw.yml
```

## Setup

1. `cp pipeline.env.example pipeline.env` — fill secrets (gitignored).
2. `bash scripts/bootstrap-labels.sh` (requires `gh` authed).
3. Configure branch protection on `main`: require CI + one human review; no
   token may merge directly. This is what auto-merge waits on.
4. `bash scripts/bootstrap-openclaw.sh` on the Gateway host.
5. Point the LiteLLM proxy at `config/litellm.pipeline.yaml`.
6. Verify, then set `PIPELINE_DRY_RUN=0`.
