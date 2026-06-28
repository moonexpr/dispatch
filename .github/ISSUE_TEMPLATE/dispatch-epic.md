---
name: Dispatch epic (decomposable)
about: A larger goal that must be broken into small leaf tasks before the pipeline works it.
title: "epic: <the larger goal>"
labels: ["Draft", "epic"]
---

<!--
A large, sprawling, or cross-cutting goal is NOT a unit of work — dispatch's reliability
falls off on big changes. Use this epic to capture the goal and decompose it into small,
independently shippable child tasks (each a "Dispatch task" issue). dispatch may CLOSE an
epic once every child is closed, but never implements an epic directly.

Tip: the /dispatch:decompose Claude skill drafts this epic and its children for you.

LABELS — the epic itself carries a status rung (usually Draft while children are open,
promoted toward Candidate/Release as they close). The status ladder and its selection
precedence apply to the CHILD tasks, where dispatch actually picks work:

  Blocker ▸ Release ▸ Candidate ▸ Draft ▸ Unscheduled  (highest priority first)

  • Unscheduled — child is backlog, not yet ready to action.
  • Draft       — child is the active, ready unit of work (the common case).
  • Candidate / Release — child has matured through review toward release.
  • Blocker      — overlay on a child that must be picked before anything else.

dispatch never implements an epic directly; it works the children and may CLOSE the epic
once every child is closed.
-->

## Goal

<!-- The larger outcome this epic delivers. -->

## Why / context

<!-- Background, motivation, constraints that apply across the children. -->

## Children

<!-- Each child is a small, independently shippable Dispatch task. The pipeline tracks
     completion off this checklist; add the issue numbers as the children are filed. -->

- [ ] #
- [ ] #
- [ ] #

## Out of scope

<!-- What this epic deliberately does not cover. -->

-

## Definition of done

<!-- The epic is done when every child above is closed and this is true: -->
