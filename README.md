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
> Invoice schema: [`schemas/invoice.json`](./schemas/invoice.json) ·
> Job Request schema: [`schemas/job-request.json`](./schemas/job-request.json)

## Roles

| Role | What it does | Backed by |
|------|-------------|-----------|
| **Architect** (this repo) | Issue selection → Job Request → Invoice intake | dispatch scripts + Claude |
| **Engineer** | Executes the job, returns an Invoice | [Ruflo](https://github.com/ruvnet/ruflo) |

The two roles are fully decoupled. Set `ENGINEER_BIN` to swap the Engineer
without touching any Architect code.

## Flow

```
GitHub Issues (queued)
        │
        ▼
  [SELECTION] dispatch.sh
    classify → route → claim one issue
        │
        ▼ Job Request {issue, route, scope, confidence}
  [ENGINEER] Ruflo  (or ENGINEER_BIN stub)
    implement → test → open PR
        │
        ▼ Invoice {status, pr_number, cost, summary, …}
  [INTAKE] architect-intake.sh
    completed  → arm auto-merge → done
    failed     → fix-dispatch ladder (up to 3 attempts)
    needs-human → escalate to operator
```

All state lives in **GitHub** (issues, labels, PRs, comments). Sessions are
stateless — there is no shared memory between runs.

---

## Testing on another repo

The fastest path to a working pipeline run on a target repo:

### 1. Point dispatch at the target repo

```bash
export PIPELINE_REPO=owner/target-repo
```

Or set it in `pipeline.env` (copy `pipeline.env.example` and fill in secrets).

### 2. Provision labels

```bash
PIPELINE_DRY_RUN=0 bash scripts/bootstrap-labels.sh
```

Idempotent — safe to re-run. Creates the full label vocabulary on `PIPELINE_REPO`.

### 3. Label issues you want the pipeline to pick up

On the target repo, add the `queued` label to any issue you want dispatched.
The pipeline only touches issues that carry this label.

### 4. Dry-run selection (no mutations)

```bash
PIPELINE_REPO=owner/target-repo bash scripts/dispatch.sh
```

Prints what dispatch *would* claim and route. Nothing is written to GitHub.

### 5. Run one issue end-to-end (manual Engineer)

Select an issue number and run the scripts individually:

```bash
# Step 1: dispatch claims the issue and emits a Job Request
PIPELINE_DRY_RUN=0 PIPELINE_REPO=owner/target-repo bash scripts/dispatch.sh

# Step 2: Engineer works the job, returns an Invoice JSON.
# If using the mock stub for testing:
ENGINEER_BIN=scripts/mock-engineer.sh bash scripts/dispatch.sh

# Step 3: feed the Invoice to architect-intake
PIPELINE_DRY_RUN=0 bash scripts/architect-intake.sh "$(cat invoice.json)"
```

### 6. Full mocked roundtrip (no Engineer, no GitHub mutations)

```bash
bash scripts/roundtrip.sh
```

Classifies all fixtures, routes each issue to the mock Engineer, processes
every Invoice through architect-intake, and prints the full audit trail.
No network calls. No GitHub mutations.

To run against live fixture issues from a real repo:

```bash
PIPELINE_REPO=owner/target-repo \
PIPELINE_FIXTURE_ISSUES=scripts/fixtures/queued-issues.json \
bash scripts/roundtrip.sh
```

---

## Engines

dispatch assembles existing engines; it builds nothing custom.

| Concern | Engine |
|---------|--------|
| Queue + state machine | GitHub Issues + labels (`scripts/bootstrap-labels.sh`) |
| Issue classification | HF Inference API (`services/classifier/classify.py`) |
| Job execution | Ruflo / Engineer (`ENGINEER_BIN`) |
| Per-task orchestration | Claude Code workflows (`.claude/workflows/*.js`) |
| CI gate | GitHub Actions (`.github/workflows/ci.yml`) |
| Adversarial review | claude-code-action@v1 (`.github/workflows/review.yml`) |
| Model routing | LiteLLM proxy (`config/litellm.pipeline.yaml`) |

## Label state machine

`queued → claimed → pr-open → in-review → docs-pending → done-pending-merge → done`

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
no GitHub, no Gateway, no model calls. This is also what CI runs.

## Layout

```
schemas/                 invoice.json · job-request.json
config/                  litellm.pipeline.yaml
services/classifier/     classify.py · fixtures/
scripts/                 dispatch.sh · fix-dispatch.sh · closure.sh
                         architect-intake.sh · roundtrip.sh · mock-engineer.sh
                         bootstrap-labels.sh
                         smoke.sh · lib/common.sh · fixtures/
.claude/workflows/       implement-task.js · fix-ci.js · update-docs.js
.github/workflows/       ci.yml · review.yml · notify-openclaw.yml
```

## Setup (first time)

1. `cp pipeline.env.example pipeline.env` — fill secrets (gitignored).
2. `bash scripts/bootstrap-labels.sh` (requires `gh` authed, `PIPELINE_DRY_RUN=0`).
3. Configure branch protection on `main`: require CI + one human review; no
   token may merge directly. This is what auto-merge waits on.
4. Point the LiteLLM proxy at `config/litellm.pipeline.yaml`.
5. Verify with `bash scripts/smoke.sh`, then set `PIPELINE_DRY_RUN=0`.

See `OPEN-QUESTIONS.md §D` for the full pre-flight checklist.
