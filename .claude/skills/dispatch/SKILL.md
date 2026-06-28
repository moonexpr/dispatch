---
name: dispatch
description: >-
  The front door to the dispatch pipeline. Takes any high-level ask — even one
  as broad as "make a Vercel website" — interviews the user with AskUserQuestion
  until the work is fully specified, then routes it: small asks to /dispatch:scope,
  large ones to /dispatch:decompose, and offers to preview a tick. TRIGGER when
  the user wants dispatch to build or do something but has not yet scoped it —
  "have dispatch make X", "get the pipeline to build Y", "/dispatch <idea>", or
  any vague-but-real request aimed at the unattended pipeline.
---

# /dispatch — interview, then route to the pipeline

dispatch is L3-class automation: it succeeds on small, clear, checkable work and
fails on vague or open-ended asks (see the wiki's *Strengths and limitations*).
The single most valuable thing this skill does is **refuse to pass a vague ask
straight to the pipeline**. It stands between the user's first sentence and any
GitHub mutation, and it uses that position to interview the user until the work
is genuinely specified — covering ground the user would not have thought to
cover on their own. Only then does it route the work onward.

The point is not to be a menu. The point is to *force meaningful engagement*:
the user does the thinking with Claude, interactively, and the pipeline receives
the small, well-formed pieces that result.

## Step 1 — understand the ask and its size

Read the request. Form a quick judgment of its *size*:

- **One unit of work** — a single, bounded change (a fix, one endpoint, one
  page). Route to `/dispatch:scope`.
- **Many units** — a feature, a project, a "build me an X" (a website, a
  service, a migration). Route to `/dispatch:decompose`, which will call
  `/dispatch:scope` per leaf.

When unsure, treat it as large — decomposition with one leaf is cheap;
under-scoping a big ask is expensive.

## Step 2 — interview until specified (the core)

Do not start decomposing or filing until the work is specified. Interview with
batched `AskUserQuestion` calls, adapting the questions to the domain. Keep
asking — across two or three rounds if needed — until you could hand the result
to a competent contractor who has never spoken to the user. State a recommended
default on every question so a fast "go with your defaults" is possible.

Cover, adapted to the domain:

- **Outcome** — what exists when this is done, concretely, and how success is
  recognized (a working deploy, a passing test, a visible behavior).
- **Shape and scope** — the major pieces, and the boundary: what is explicitly
  *not* included in this round.
- **Stack and constraints** — frameworks, languages, hosting, existing repo or
  greenfield, and any hard constraints (budget, performance, accessibility,
  deadline).
- **Inputs and unknowns** — content, data, credentials, designs, or decisions
  the work depends on; surface what is missing now rather than letting the
  pipeline guess later.
- **Target repository** — the `owner/repo` the work lands in. Generate a sensible
  default slug and confirm it (see *Naming the target repository* below); never
  route without a settled target.

### Worked example: "make a Vercel website"

That sentence is not yet a unit of work — it is a project. Before any
decomposition, interview:

- **Purpose and content** — marketing site, web app, blog, storefront? What
  pages or features? Where does the content come from?
- **Stack** — Next.js (the Vercel-native default) or another framework?
  TypeScript? A component library or design system?
- **Data and integrations** — static, a CMS, a database, auth, payments,
  third-party APIs?
- **Design** — an existing design or brand, a reference site, or
  design-from-scratch within constraints?
- **Repository and deploy** — new repo or existing? The Vercel project and
  domain — already set up, or part of the work? What are the deploy/preview
  expectations?
- **Out of scope** — what is deliberately deferred (analytics, i18n, a CMS) so
  the first increment stays shippable.

Only once these are answered is "make a Vercel website" specified enough to
break down responsibly.

### Naming the target repository

Every routed unit lands in a GitHub repository, so the skill must settle one
`owner/repo` target before routing — generate a name, then let the user keep or
change it. Do **not** make the user invent a slug from nothing.

1. **Generate a default slug from the agreed outcome.** Kebab-case, lowercase,
   hyphen-separated, concise and descriptive — e.g. a personal accounting app →
   `accounting-app`, a CTTB events site → `cttb-events`. Strip filler ("the",
   "app", "website") only when the remainder still reads clearly. Avoid dates and
   redundant suffixes.
2. **Resolve the owner.** Default to the configured pipeline owner (`PIPELINE_REPO`'s
   org, else the operator's default GitHub org); only ask when none is known.
3. **Ask once with `AskUserQuestion`**, offering the generated `owner/repo` as the
   recommended default, plus: **Use an existing repo** (the user names it), **Edit
   the name**, and visibility (private default — it matches what the pipeline
   creates). If the user gave a repo in their original ask, skip generation and
   confirm that one.

The target may be a **new** repo: the pipeline's `admin:prep` stage creates it
(`gh repo create … --private --add-readme`) on the first live tick if it is
missing, so a generated name that does not yet exist is fine — it does not have to
be created by hand first. Record the settled `owner/repo` so Step 3 can play it
back and Step 4 can hand it onward.

## Step 3 — confirm the specification

Play back a short, structured summary of what was agreed — outcome, scope,
stack, constraints, explicit non-goals, and the **target `owner/repo`**. Use one
`AskUserQuestion` to
confirm: **Looks right — proceed**, **Adjust** (reopen the interview), or
**Stop**. Do not proceed on assumptions the user has not confirmed.

## Step 4 — route

With a confirmed specification (including the settled `owner/repo`):

- **Large / multi-unit** → invoke `/dispatch:decompose`, handing it the
  confirmed specification *and the target `owner/repo`* so it can produce small
  leaf issues (each via `/dispatch:scope`) under a Draft epic in that repo.
- **Single unit** → invoke `/dispatch:scope` directly to file one well-formed
  issue against the target repo.

Always pass the target `owner/repo` downstream — never let scope/decompose
re-ask or default it silently.

Then offer, with `AskUserQuestion`, to preview the first issue with
`/dispatch:tick` (dry-run) or to stop and let the user review the filed work.
Never take the work live as part of this skill — going live stays an explicit,
separate decision in `/dispatch:tick`, and merges stay with `/dispatch:review`
and branch protection.

## Guardrails

- Interviewing is mandatory for a vague ask; skipping it defeats the skill.
- This skill files and routes; it does not run the pipeline live and does not
  merge.
- If the user resists specifying and the ask stays vague, say plainly that the
  pipeline will not reliably succeed on it yet, and offer to do the work
  interactively instead.

## The skill family

- **/dispatch** — this front door: interview, then route.
- **/dispatch:scope** — one rough idea → one well-formed issue.
- **/dispatch:decompose** — a large ask → many small leaf issues (Draft epic).
- **/dispatch:tick** — preview a tick in dry-run, then a deliberate go-live.
- **/dispatch:review** — vet a pipeline pull request before a human approves.
