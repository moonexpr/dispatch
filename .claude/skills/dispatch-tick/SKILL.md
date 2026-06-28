---
name: dispatch:tick
description: >-
  Preview a dispatch tick in dry-run, summarize exactly what it would do, and
  capture a deliberate decision before any live GitHub mutation. TRIGGER when the
  user wants to run, preview, or trigger the pipeline — "run dispatch", "do a
  tick", "preview what dispatch would do on issue N", "take this issue live" — or
  to go from a filed issue to a real run. Enforces dry-run-first; live is always
  an explicit opt-in.
---

# /dispatch:tick — preview a tick, then decide to go live

dispatch is scheduled, not fire-and-forget, and dry-run is its default for a
reason (the wiki's *Strengths and limitations* and *Getting started*). This
skill runs a tick the safe way: preview first, read what it intends, and only
then — with a deliberate `AskUserQuestion` — let it mutate GitHub.

## Step 1 — confirm the target

Establish the repository (`-r/--repo` or `$PIPELINE_REPO`) and the issue. If no
issue is named, the pipeline takes the first open one; surface which that is
before running so the user is not surprised. Confirm the engine if it matters
(`DISPATCH_ENGINE=baseworkflow` by default, `websitewf` for the web overlay).

## Step 2 — run the dry-run tick

Run the tick in its default dry-run mode (mutating nothing):

```bash
python3 dispatch.py --repo <owner/repo> [<issue>]
```

Dry-run prints the actions the pipeline *would* take. Capture that output. If the
run errors or the classifier routes the issue to needs-human, report that
plainly — a low-confidence or under-specified issue is a signal to send it back
through `/dispatch:scope` rather than to force it live.

## Step 3 — summarize the intended actions

Tell the user, in a few lines, what the tick would do: which issue it claims, the
work plan the Architect composed (purpose, strategy, the acceptance criteria it
will build against), and the GitHub mutations the Admin phase would make (claim,
branch, pull request, label transitions). Flag anything that looks off against
the issue's stated intent.

## Step 4 — decide to go live (`AskUserQuestion`)

Going live is the one consequential step, so make it explicit. Use
`AskUserQuestion`:

- **Stay in dry-run** (recommended default) — done; nothing was mutated.
- **Run live now** — re-run with `--live` (`PIPELINE_DRY_RUN=0`) so the Admin
  phase performs real `gh` mutations. Confirm the repo and issue in the question
  text so the target is unambiguous.
- **Pick a different issue** — loop back to Step 1.
- **Re-scope first** — the preview revealed the issue is not ready; hand off to
  `/dispatch:scope`.

## Step 5 — run live only if chosen

On *Run live now*:

```bash
python3 dispatch.py --repo <owner/repo> <issue> --live
```

Report what the live tick did and the resulting pull-request URL. Remind the user
that the PR still needs human approval — route it to `/dispatch:review`. Do not
approve or merge from this skill.

## Composition

- Consumes an issue authored by `/dispatch:scope` or `/dispatch:decompose`.
- Produces a pull request reviewed by `/dispatch:review`.
- For unattended scheduling instead of a manual tick, see the wiki's *Unattended
  Linux server installation*.
