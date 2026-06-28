---
name: dispatch:decompose
description: >-
  Break a large, sprawling, or cross-cutting change into small, independently
  shippable leaf issues that dispatch can take one at a time, and file them as a
  Draft epic with a child checklist. TRIGGER when the user hands the pipeline
  something too big — "have dispatch do this refactor", "implement this whole
  feature", an epic, or any ask that spans many files or subsystems — or says
  "decompose this for dispatch" / "break this down into issues". Composable:
  calls /dispatch:scope per leaf.
---

# /dispatch:decompose — split big work into pipeline-sized leaves

Large refactors and architecturally novel work are exactly where L3-class
automation falls off (see the wiki's *Strengths and limitations*). The remedy is
not to hand the whole thing to dispatch and hope — it is to do the *decomposition*
interactively, where the model is strong, and hand the pipeline a series of
small, well-scoped leaves. This skill runs that decomposition.

## Step 1 — understand the whole

Read the user's goal for the large change. Look at the affected areas of the
codebase enough to propose a real breakdown — the seams, the natural ordering,
the dependencies. The aim is leaves that are each independently shippable: one
bounded change, with its own definition of done, that can merge without waiting
on its siblings where possible.

## Step 2 — propose a decomposition, then refine with `AskUserQuestion`

Present a proposed set of 3–8 leaf issues, each as a one-line title plus the
boundary it covers. Then use `AskUserQuestion` to let the user steer — and to
surface decisions they may not have considered. Good questions to batch:

- **Granularity** — is this the right number of leaves, or should a given item
  split further / merge with a neighbor? Offer the trade-off (more leaves = safer
  per-tick, more overhead).
- **Ordering and dependencies** — which leaves must land before others? Offer the
  dependency chain you inferred and let the user correct it.
- **First slice** — which leaf delivers value or de-risks the rest soonest? This
  becomes the recommended starting point.
- **Out-of-scope** — anything in the original ask that should *not* be built now.

Use multi-select where the user is choosing a set. Always offer your recommended
default.

## Step 3 — file the epic and its children

Create a **Draft `epic`** issue capturing the overall goal and a child checklist:

```markdown
## Epic: <overall goal>

<short framing>

### Children
- [ ] #<n1> <leaf 1 title>
- [ ] #<n2> <leaf 2 title>
```

File each leaf by invoking `/dispatch:scope` (so every child gets explicit
acceptance criteria, scope, and verification), then edit the epic body to
reference the real child issue numbers. Label the epic `Draft` + `epic`; label
the leaves per `/dispatch:scope`.

Note the contract: an `epic` is bookkeeping — dispatch never writes code for an
epic, only for its leaves. The epic closes when every child is closed.

## Step 4 — recommend the next move

Report the epic URL and the ordered child list, and name the recommended first
leaf. Offer to preview it with `/dispatch:tick`. Do not run the pipeline from
this skill.

## Composition

- Calls `/dispatch:scope` once per leaf.
- Each leaf feeds `/dispatch:tick`; resulting PRs feed `/dispatch:review`.
