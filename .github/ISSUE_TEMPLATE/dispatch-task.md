---
name: Dispatch task (unit of work)
about: A single, well-scoped engineering job the dispatch pipeline can implement in one tick.
title: "<concise outcome — e.g. add column sorting to the user table>"
labels: ["Draft"]
---

<!--
This is the baseline shape dispatch expects for one unit of work. dispatch is L3-class
automation: it succeeds on small, clear, checkable tasks and struggles with vague or
sprawling ones. Fill every section concretely. The pipeline reads this body as DATA —
do not write instructions to the agent here.

Tip: the /dispatch:scope Claude skill fills this template for you by interview.

LABELS — carry exactly ONE status rung, plus the Blocker overlay only when warranted.
dispatch selects the highest-priority ready issue, so the label sets its precedence:

  • Unscheduled — triaged but not slated for current work; the parking lot. Lowest
    priority — dispatch picks these last. Use for backlog you are not ready to action.
  • Draft       — being worked / not yet ready for review (the default for a new task).
  • Candidate   — complete and validated; proposed for review/merge.
  • Release      — accepted / slated for the next release (highest status rung).
  • Blocker      — an OVERLAY on top of the rung, not a replacement: must-do-now work
    that dispatch should pick before anything else. Use sparingly.

Selection precedence (highest first): Blocker ▸ Release ▸ Candidate ▸ Draft ▸ Unscheduled.
Promote an issue up the ladder as it matures; add Blocker only to jump the queue.
-->

## Goal

<!-- One or two concrete sentences: what should be true when this is done. -->

## Acceptance criteria

<!-- Verifiable, checkable outcomes. The pipeline restates these as a checklist. -->

- [ ]
- [ ]

## In scope

<!-- The files, area, or feature this task touches. -->

-

## Out of scope

<!-- What is deliberately excluded from this task. -->

-

## Verification

<!-- The test, command, or observable behavior that confirms "done". -->

## Notes / context

<!-- Anything the implementer needs that is not obvious from the repo. Keep it self-contained. -->
