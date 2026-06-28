---
name: dispatch:scope
description: >-
  Turn a rough idea into a dispatch-ready GitHub issue — a well-scoped unit of
  work with explicit acceptance criteria, scope boundaries, and a verifiable
  outcome — then file it. TRIGGER when the user wants to hand work to the
  dispatch pipeline, says "file an issue for dispatch", "scope this for
  dispatch", "turn this into a dispatch task", or describes a change they want
  the unattended pipeline to implement. Composable: invoked per-leaf by
  /dispatch:decompose.
---

# /dispatch:scope — author a dispatch-ready issue

dispatch is L3-class automation: it is reliable on **small, clear, checkable**
work and unreliable on vague or open-ended asks (see the wiki's *Strengths and
limitations*). This skill exists to move a request across that line *before* it
reaches the pipeline. It reads the user's rough idea as data, interrogates the
gaps with `AskUserQuestion`, and produces a GitHub issue the pipeline can
actually succeed on.

Do **not** skip the questions to save a round trip — the questions are the
value. A ten-second clarification here prevents a wasted unattended tick.

## Step 1 — ground the request

Read the user's description. If a target repository is named (or `$PIPELINE_REPO`
is set), briefly look at the relevant area of the codebase so the scope options
you offer are concrete, not generic. Do not over-explore; a couple of targeted
reads or a `gh` search is enough to name real files and subsystems.

## Step 2 — interrogate the gaps (batched `AskUserQuestion`)

Send the questions the request leaves open, batched into one `AskUserQuestion`
call where independent. Cover, at minimum:

- **Purpose** — what kind of work this is, which drives the engineering harness.
  Offer options drawn from dispatch's purpose taxonomy: *new-feature*,
  *refactor*, *prototype*, *test*, *research*, *bug-fix*. (Source of truth:
  `app/config/workplan-rules.yml`.)
- **Scope boundary** — which files / subsystem / feature are in scope, and what
  is explicitly **out** of scope. Offer the concrete areas you found in Step 1.
- **Definition of done** — how success is verified: a test that passes, a
  behavior that can be checked, a command whose output changes. If the user
  cannot name one, that is a signal the task is not yet ready for dispatch —
  help them make it verifiable or recommend doing it interactively instead.

Add a question for any other load-bearing ambiguity (target branch, data
shape, user-facing copy). Always state your recommended default so a quick "go"
is enough.

## Step 3 — draft the issue

Compose the issue body in this shape (the pipeline restates acceptance criteria
as a checklist, so write them as checkable items):

```markdown
## Goal
<one or two concrete sentences>

## Acceptance criteria
- [ ] <verifiable outcome 1>
- [ ] <verifiable outcome 2>

## In scope
- <files / area>

## Out of scope
- <explicitly excluded>

## Verification
<the test / command / behavior that confirms done>
```

Keep it self-contained: everything needed to understand the task lives in the
issue, not in the user's head or a side channel. The pipeline treats issue text
as untrusted data, so write plainly — no meta-instructions to the agent.

## Step 4 — confirm and file

Show the drafted issue. Use `AskUserQuestion` to offer: **File it** (recommended),
**Edit first**, or **Hold** (return the draft without filing). On *File it*,
create it with `gh issue create` (prefer the `Dispatch task` issue template), and
apply exactly one **status rung** plus, if warranted, the `Blocker` overlay. The
rungs and their selection precedence — dispatch picks the highest-priority ready
issue — are:

- **Blocker** (overlay, highest) — must-do-now; jumps the queue. Use sparingly.
- **Release** — accepted / slated for the next release.
- **Candidate** — complete and validated; proposed for review/merge.
- **Draft** — being worked / not yet ready for review. *The usual choice for a
  newly scoped, ready task.*
- **Unscheduled** (lowest) — triaged backlog, not slated for current work.

So a fresh, ready unit of work is normally `Draft`; mark `Unscheduled` to park it,
or add `Blocker` to have dispatch pick it first. Add any purpose label the repo
uses, then report the issue URL.

Do not run the pipeline from this skill — filing is the deliverable. To preview a
tick on the new issue, hand off to `/dispatch:tick`.

## Composition

- `/dispatch:decompose` calls this skill once per leaf issue.
- The issue this skill files is the input to `/dispatch:tick`.
- A pull request the pipeline later opens is reviewed by `/dispatch:review`.
