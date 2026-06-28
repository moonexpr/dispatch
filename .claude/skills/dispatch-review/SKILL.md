---
name: dispatch:review
description: >-
  Review a pull request the dispatch pipeline produced against its issue's
  acceptance criteria, run the checks, and capture the human approval decision —
  keeping a person as the approver of every merge. TRIGGER when the user wants to
  review or approve a dispatch PR, says "review the dispatch PR", "is #N ready to
  merge", "check what dispatch did on issue N", or points at an open pipeline
  pull request. Never merges directly — branch protection plus human approval is
  the only merge path.
---

# /dispatch:review — human-in-the-loop review of pipeline output

dispatch produces a *draft* pull request from a capable but fallible
contributor; the human stays the approver (the wiki's *Strengths and
limitations* is explicit that merging is never delegated). This skill makes that
review fast and thorough: it checks the PR against the criteria the issue
declared, runs the gate, and routes the decision back through `AskUserQuestion`
so the person — not the model — approves.

## Step 1 — locate the PR and its contract

Identify the pull request (`gh pr list`, or the number the user gave). Fetch the
linked issue (the PR body should carry `Closes #<n>`) and extract its **acceptance
criteria** checklist — that list is the contract the work is judged against.

## Step 2 — check the diff against each criterion

Read the diff (`gh pr diff`). For each acceptance criterion, judge whether the
change actually satisfies it, citing the specific files and lines. Be a skeptic:
default to "not yet met" unless the diff clearly demonstrates otherwise. Watch
for the known L3 failure modes — a plausible-but-wrong implementation, a skipped
step, scope that widened beyond the issue, or criteria silently unmet.

## Step 3 — run the gate

Run the project's checks locally where possible — the smoke gate
(`python3 scripts/smoke.py`) and any tests the PR touches — and read the
continuous-integration status (`gh pr checks`). Report pass/fail plainly,
including the actual output on failure; never report green you did not observe.

## Step 4 — summarize, then capture the decision (`AskUserQuestion`)

Present a compact verdict: each criterion marked met / not-met / unclear, the
gate result, and any concerns. Then use `AskUserQuestion` to capture the human
decision — this is the approval gate, and the answer must come from the person:

- **Approve & arm auto-merge** — the criteria are met and the gate is green;
  approve the PR and let branch protection carry it to merge.
- **Request changes** — post the specific, actionable gaps as a PR review so the
  next fix tick (or an interactive session) can address them.
- **Escalate / discuss** — something needs a human conversation before any
  decision; capture the question.
- **Hold** — take no action yet.

## Step 5 — act, but never merge

Execute the chosen action with `gh` (approve, request changes, comment).
**Never merge directly and never push to the protected branch** — auto-merge,
gated by CI plus the human approval just given, is the only merge path. Touch
only the labels your review owns. Report what was done and the PR URL.

## Composition

- Reviews the PR that a `/dispatch:tick` run (or a scheduled tick) produced.
- "Request changes" pairs naturally with a follow-up interactive session or a
  fix tick.
