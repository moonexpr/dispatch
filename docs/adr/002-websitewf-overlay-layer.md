# ADR-002: WebsiteWF — an Overlay Layer that Specializes BaseWorkflow for Web Development

**Status:** Proposed
**Date:** 2026-06-27
**Deciders:** JC (architecture owner)
**Supersedes:** —
**Related:** [`ADR-001`](./001-hfsm-automata.md) (HFSM engine); `app/workflows/baseworkflow.yml`; `engine/workflow/loader.py`; `engine/actions/action.py`; the `WORKFLOW` runtime selector

---

## Context

Dispatch already has three architectural layers of instruction: the **three phases**
(`spec → work → build`), the **three agents** (Architect / Engineer / Admin), and the
**BaseWorkflow** that wires them into a YAML-driven Harel statechart (ADR-001). The
engine (`engine/`) is deliberately domain-agnostic mechanism; everything web-, app-,
or task-specific is *configuration* (`app/workflows/baseworkflow.yml` + the per-action
interface manifests under `app/config/actions/`) and *bindings*
(`src/baseworkflow/bindings/`).

We now want a **fourth layer on top of BaseWorkflow — `WebsiteWF`** — that caters the
generic lifecycle to **web development**. Concrete target use cases:

1. Create new GitHub issues from UX loops and website use cases.
2. Create the foundations of a website on a reliable framework (Next.js or Laravel).
3. Add a new page or route to an existing site.
4. Turn user feedback into a new technical ticket.
5. Build new web technology that requires backend support.
6. Adopt existing technology — ours or third-party (e.g. the Stripe payment
   processor) — into an application.

Crucially, WebsiteWF must **reuse BaseWorkflow's architecture, not fork it**. It must be
able to do four things against the base lifecycle:

- **Extend** an existing agentic task (augment its behavior, base still runs).
- **Add** a new task onto the workflow.
- **Reroute / rewire** an existing action's I/O **via a proxy action** (a new primitive).
- **Replace** an existing action with a substitute.

The decision under record is **how WebsiteWF is expressed against BaseWorkflow**, **what
the four operations compile to in the engine**, and **how a workflow is selected at
runtime** — *not* the implementation of any single web use case (that follows, vertical
by vertical).

A second force shapes the choice: `main` is in active development and BaseWorkflow is
still evolving. A *fork* of `baseworkflow.yml` would drift the moment the base changes;
a *diff* stays correct by construction because the base remains its single source.

---

## Decision

Express **WebsiteWF as an overlay (a declarative diff) over BaseWorkflow**, applied at
load time, plus the minimal engine support the four operations require.

### 1. Overlay document (`extends:` + `overlays:`)

A workflow YAML may declare `extends: <base>` and a list of ordered `overlays:` ops. The
loader (`engine/workflow/loader.py`) loads the base `WorkflowNode`, then applies the ops
in order to produce a merged tree. New/replacement action manifests come from the overlay
doc's own `uses:` globs. The base document is never edited.

Four overlay verbs map one-to-one onto the four required operations:

| Required operation | Overlay verb | Compiles to |
|---|---|---|
| Extend an agentic task | `extend: <tok> with: <tok> mode: before\|after\|wrap` | binding composition — `with` runs before/after, or wraps and calls, the base body |
| Add a new task | `add: <tok> phase: <p> [after\|before: <tok> \| at: start\|end]` | splice a new `ActionRefNode` into phase `p`'s Sequence/Loop at the named position |
| Reroute / rewire I/O | `proxy: <tok> rewire: {in: {...}, out: {...}}` | install a **proxy action** (below) around the target that remaps its `interface.in/out` shelf wiring |
| Replace with a substitute | `replace: <tok> with: <tok>` | swap the action node's `bind`/`kind`/manifest at the same interface position |

### 2. The `proxy` action kind (the one new primitive)

The four verbs need exactly one new engine construct — the **proxy action** the
requirement names explicitly. It is a fourth Action kind alongside `Procedure`,
`Inference`, and `Program` (`engine/actions/action.py`):

- A `proxy` manifest declares `kind: proxy`, a `target:` token, and a `rewire:` map.
- At compile time (`engine/workflow/visitor.py`), the proxy wraps the target's body: it
  reads the **remapped** inputs from the shelves, delegates to the target action, then
  writes the target's outputs to the **remapped** output shelf paths. The target's own
  binding is untouched — the proxy only rewires the wiring around it.
- This keeps proxying a *load-time I/O reroute*, not a behavioral rewrite: it is the
  statechart analogue of inserting an adapter on a transition's data edges.

This maps onto ADR-001's construct table as a new row: **proxy action ← data-edge adapter
on an Action's interface** (it touches the Shelf wiring, never the transition graph).

### 3. Runtime selection — the `WORKFLOW` env var

Which workflow runs is selected by a new **`WORKFLOW`** environment variable
(`WORKFLOW=baseworkflow` default | `WORKFLOW=websitewf`), kept **orthogonal** to
`DISPATCH_ENGINE` (which selects the *authoring engine* and whose only supported value is
now `baseworkflow` after ADR-era cleanup). The authoring bridge
(`src/orchestration/baseworkflow_bridge.py`) becomes workflow-agnostic: it reads
`WORKFLOW`, imports that workflow module, and calls its `run_live`. Two orthogonal axes —
*which workflow* (`WORKFLOW`) and *which authoring engine* (`DISPATCH_ENGINE`) — stay
separable.

### 4. WebsiteWF as a thin Python + config layer

- `app/workflows/websitewf.yml` — the overlay document (`extends: baseworkflow`).
- `app/config/actions/web/*.yml` — the new `web:*` action interface manifests
  (including the `kind: proxy` ones).
- `src/websitewf/` — a `WebsiteWF` controller mirroring `BaseWorkflow` (loads
  `workflows/websitewf.yml`) and a `bindings/` package whose `build_registry()` **reuses the
  base bindings** and registers the `web:*` bodies on top. BaseWorkflow gains only a small
  optional `_bindings_module` hook so it stays reusable; no base behavior changes.

### 5. Scope: mechanism + one proof vertical

The first increment lands the **mechanism** (overlay loader + proxy kind + `WORKFLOW`
selector + WebsiteWF skeleton) proven end-to-end on **one** use case — **"add a new page
or route"** (#3) — because it exercises all four overlay verbs at once:

- **replace** `github:generate_work_units` → `web:generate_route_work_units`
- **extend** `architect:classify_strategy` (mode `after`) → `web:classify_route_addendum`
  (tags target framework + route path)
- **add** `web:scaffold_route` into the `work` phase
- **proxy** `engineer:execute_orchestration` I/O to inject the route spec

The other five use cases (#1, #2, #4, #5, #6) are deferred to tracked issues under a
WebsiteWF epic.

---

## Options Considered

### Option A: Fork `baseworkflow.yml` → `websitewf.yml`

Copy the base document and edit it inline.

| Dimension | Assessment |
|---|---|
| Engine change | None |
| Drift from base | High — two docs to keep in sync by hand |
| Auditability of the diff | Poor — the "diff" is implicit in two full files |
| Expresses extend/add/proxy/replace | Only as manual copy-edits |

**Pros:** zero engine work; simplest possible to a first run.
**Cons:** WebsiteWF silently diverges every time BaseWorkflow changes (and it is changing
daily). The four required operations have no first-class expression — they become
copy-paste, exactly the coupling the layer is meant to avoid.

### Option B: Python subclass only (`class WebsiteWF(BaseWorkflow)`)

Subclass the controller, override `WORKFLOW_PATH` and the bindings, but still point at a
hand-written full YAML.

| Dimension | Assessment |
|---|---|
| Engine change | Minimal (`_bindings_module` hook) |
| Reuse of base config | Bindings only, not the workflow shape |
| Expresses the four ops | No declarative grammar; ops live in Python |

**Pros:** small; reuses the bindings cleanly.
**Cons:** the *shape* of the workflow (phases, action order, I/O wiring) still has to be
restated in a forked YAML — it solves binding reuse but not document reuse, so Option A's
drift problem remains for the structure.

### Option C: Overlay / diff document — **chosen**

`extends: baseworkflow` + ordered `overlays:`, merged at load; one new `proxy` action kind.

| Dimension | Assessment |
|---|---|
| Engine change | Small + additive (loader merge + one Action kind) |
| Drift from base | None — base stays the single source; overlay is a diff |
| Auditability of the diff | Excellent — the overlay file *is* the diff |
| Expresses the four ops | Native — one verb each |

**Pros:** WebsiteWF is a small, reviewable diff that stays correct as the base evolves.
The four operations are first-class. The engine stays domain-agnostic — it gains a generic
overlay/merge capability and a generic proxy adapter, both reusable by any future layer
(AppWF, MobileWF, …), not web-specific.
**Cons:** real (if small) engine work: a merge pass in the loader and a fourth Action
kind with its compile-time I/O-rewire wrapper. Overlay-order semantics must be pinned
(ops apply top-to-bottom; a later op sees earlier ops' effect).

### Runtime-selection sub-decision

- **`WORKFLOW` env var (chosen)** — a dedicated axis; keeps "which workflow" separate from
  "which authoring engine" (`DISPATCH_ENGINE`).
- *Reuse `DISPATCH_ENGINE=websitewf`* — rejected: conflates two distinct concepts on one
  flag.
- *Per-repo / per-issue config* — deferred as an additive convenience on top of `WORKFLOW`
  (e.g. a `website` label resolves to `WORKFLOW=websitewf`) once the env path works.

---

## Trade-off Analysis

The decision turns on one property the status quo cannot give cheaply: **a layer that
stays correct while its base changes daily.** Only a diff (Option C) has that property;
both fork-shaped options (A, B) decay the moment BaseWorkflow moves. The price is a small,
*generic* engine addition — an overlay-merge pass and a proxy adapter — neither of which is
web-specific, so the cost is amortized across every future workflow layer rather than spent
on WebsiteWF alone. That generality is the tell that the abstraction is at the right
altitude: WebsiteWF should be *the first consumer* of a layering capability, not a bespoke
fork.

The proxy action is the subtle part. It deliberately rewires **data edges only** (Shelf
wiring on an Action's interface), never the **transition graph** — preserving ADR-001's
separation of the discrete skeleton from the activity/data layer, so the machine stays as
analyzable as before. A proxy that could re-target transitions would reintroduce exactly
the coupling ADR-001 externalized; scoping it to I/O keeps it safe.

---

## Consequences

**Becomes easier**

- A web-(or any-domain-)specialized lifecycle is a small auditable diff, not a fork.
- The four operations (extend / add / proxy / replace) become declarative one-liners with
  uniform semantics across every layer.
- The engine gains two *generic* capabilities — workflow overlay/merge and a data-edge
  proxy adapter — reusable by future layers (AppWF, MobileWF) with no new engine work.
- `WORKFLOW` cleanly separates "which lifecycle" from "which authoring engine."

**Becomes harder**

- The loader grows a merge pass; overlay-order and conflict semantics must be pinned and
  tested (two overlays touching the same token; an `add` after a `replace`).
- A fourth Action kind (`proxy`) must be built, validated, and threaded through the
  CompileVisitor's I/O wrapper and the workflow validator.
- Two workflow documents now exist; `validate()` must run against the **merged** tree, and
  CI must validate `websitewf.yml`'s merged form, not just its surface.

**Must revisit**

- **Overlay conflict policy.** Define what happens when two ops target the same token
  (last-wins vs. error). Default proposal: **error on conflicting `replace`/`proxy` of the
  same token; allow stacked `extend`** (composition is associative).
- **Deep nesting of layers.** If a third layer ever `extends: websitewf`, the merge must be
  transitive and depth-bounded (mirror `Program.MAX_DEPTH`).
- **Per-repo selection.** Revisit promoting `WORKFLOW` from env to repo/issue config once
  more than one repo runs WebsiteWF.

---

## Action Items

> Tracked as the **WebsiteWF epic** + child issues. The mechanism items gate the verticals.

1. [ ] **Overlay loader:** `extends:` + `overlays:` merge in `engine/workflow/loader.py`
   (verbs: `replace`, `extend`, `add`, `proxy`); pin top-to-bottom order; error on
   conflicting `replace`/`proxy`.
2. [ ] **Proxy action kind:** add `Proxy(Action)` (`kind: proxy`) + the CompileVisitor
   I/O-rewire wrapper + manifest schema (`target:`, `rewire:`); validator support.
3. [ ] **`WORKFLOW` selector:** make the authoring bridge workflow-agnostic; default
   `baseworkflow`; document in PROJECT.md's flag table.
4. [ ] **WebsiteWF skeleton:** `app/workflows/websitewf.yml` (this spec), `app/config/actions/web/`,
   `src/websitewf/` (controller + bindings reusing base via `build_registry`); add the
   `_bindings_module` hook to `BaseWorkflow`.
5. [ ] **Proof vertical "add a page/route":** the four `web:*` bindings + manifests; a
   `run_mock` e2e (roundabout green) + smoke section; validate the merged `websitewf.yml`.
6. [ ] **Deferred use cases (epic children):** #1 UX-loop→issues, #2 scaffold-foundation,
   #4 feedback→ticket, #5 new-backend-tech, #6 adopt-third-party (Stripe).
7. [ ] **Overlay conflict + transitivity tests:** two ops on one token; `add` after
   `replace`; (future) `extends: websitewf` depth bound.
