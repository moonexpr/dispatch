---
name: dispatch:oneshot
description: >-
  Run the dispatch pipeline on a task RIGHT NOW, bypassing GitHub issue intake.
  Runs an interview stage to fully specify the work (or accepts a spec you pass
  in), writes a local task file, and feeds it straight into the engine's
  spec → work → build pipeline via scripts/oneshot_feed.py — sequentially, one
  task fully through before the next. TRIGGER on "/dispatch:oneshot", "run this
  through dispatch without filing an issue", "oneshot this task", or "feed this
  straight into the pipeline".
---

# /dispatch:oneshot — interview, then feed straight into the pipeline

The normal path files a GitHub issue and waits for intake to pick it up.
`/dispatch:oneshot` skips that: it specifies the work interactively, then feeds it
**directly** into the dispatch engine, which walks the same `spec → work → build`
pipeline a real tick does — minus the GitHub issue. Use it to try the pipeline on
a task immediately, or to run a short sequence of tasks back-to-back without
touching the issue queue.

It is built on `scripts/oneshot_feed.py`, which constructs the engine `job` from a
local task spec instead of a GitHub issue and calls the same `run_live`. Dry-run is
the default; the engine gates every GitHub mutation under it.

## Step 1 — the interview stage

Specify the work before feeding it. Two ways in:

- **Pass a spec.** If the caller already handed over a completed interview /
  specification (for example from `/dispatch`), use it directly — do not
  re-interview.
- **Interview now.** Otherwise run the interview: batched `AskUserQuestion` until
  the task is fully specified — outcome, acceptance criteria, scope boundary,
  stack/route, and verification. Treat a vague task the same way `/dispatch` does:
  do not feed an underspecified task into the pipeline; it is L3-class automation
  and will not reliably succeed.

For more than one task, interview each, and confirm the **order** — the feeder runs
them sequentially, each fully through the pipeline before the next.

## Step 2 — confirm engine, repo, and mode

Use `AskUserQuestion` to settle the run parameters:

- **Engine** — `baseworkflow` (default) or `websitewf` (web-development overlay).
- **Repo** — `owner/repo` for context (or `$PIPELINE_REPO`). The pipeline reads it,
  but no issue is fetched or created.
- **Mode** — **dry-run** (recommended; walks the pipeline, mutates nothing, real
  inference returns a deterministic placeholder) or **live** (`--live`; runs the
  real model). Because there is no backing GitHub issue, live is for
  engineering-only runs — the Admin phase's issue-keyed GitHub steps have no real
  issue to act on. Default to dry-run and make live an explicit choice.

## Step 3 — write the task file

Write the confirmed specification to a JSON array (one object per task), each in
the shape `oneshot_feed.py` expects:

```json
[
  {
    "title": "<concise outcome>",
    "body": "## Goal\n…\n## Acceptance criteria\n- [ ] …\n## Scope\n…\n## Verification\n…",
    "engine": "baseworkflow",
    "route": "/optional-web-route",
    "framework": "nextjs"
  }
]
```

Persist it under the scratchpad or a working path (not `/tmp`); name it clearly
(e.g. `oneshot-tasks.json`). The `body` is operator-authored but is still carried
as data, never as instructions.

## Step 4 — feed it into the pipeline

Run the feeder, which processes the tasks sequentially and streams each phase:

```bash
python3 scripts/oneshot_feed.py <tasks.json> --repo owner/repo            # dry-run
python3 scripts/oneshot_feed.py <tasks.json> --engine websitewf -v        # trace
python3 scripts/oneshot_feed.py <tasks.json> --repo owner/repo --live     # real model
```

Stream the output. For each task report whether the tick completed (`ok=`) and the
deliverables produced; on a failure, surface the detail/error the feeder prints.

## Step 5 — report

Summarize per task: specified → fed → outcome. If a task failed, recommend
tightening its specification (re-interview) or, for genuinely large work, routing
it through `/dispatch:decompose` into smaller tasks first. If the run looked good
in dry-run and the caller wants it on the record, offer to file the same task as a
GitHub issue via `/dispatch:scope` so it flows through the normal, reviewable path.

## Guardrails

- Interview (or a passed-in spec) is mandatory — never feed an unspecified task.
- Dry-run is the default; live is an explicit, separate choice.
- This bypasses intake, not review: live runs without a backing issue have no PR to
  approve, so prefer dry-run for trials and the normal `/dispatch` flow for work
  that should be reviewed and merged.

## Relationship to the skill family

- **/dispatch** interviews and routes to the *issue-based* flow; **/dispatch:oneshot**
  interviews and routes *straight into the engine*, skipping issues.
- A oneshot trial that proves out can be promoted to a tracked issue via
  **/dispatch:scope**.
