# ADR-003: Three-Layer Composition — Controller References, First-Class Needs, Service Facets, and Superseedable States

**Status:** Accepted
**Date:** 2026-07-01
**Deciders:** JC (architecture owner)
**Supersedes:** — (extends ADR-001, ADR-002)
**Related:** #188 (decouple GitHub-issue intake from core orchestration), #190 (superseedable HFSM states), ADR-001 construct mapping, `foundation/workflow`, `foundation/actions`

---

## Context

ADR-001 fixed the execution model (Harel statecharts, one RTC interpreter, first-class
transitions). ADR-002 fixed the specialization model (overlays as diffs over a base
workflow). Two structural problems remain, tracked by two epics:

1. **Workflows reference actions directly** (`phases: [github:generate_work_units, …]`).
   The workflow YAML is simultaneously the *contract* ("what parties cooperate to
   deliver this lifecycle") and the *plumbing* ("which concrete step runs third").
   That entanglement is why GitHub-issue intake is welded into the core lifecycle
   (#188): swapping an input source means editing the contract layer.

2. **States run to completion along the planned path** (#190). Nothing can preempt an
   active state when the scenario calls for something more pressing; the engine has no
   vocabulary for "this request routes to a more specific handler that takes over."

Underneath both: actions have no formal way to *declare what they require from their
environment* — a GitHub API surface, an interactive operator, a seeded input. Those
dependencies are implicit in the bodies, so nothing can statically decide whether a
workflow is runnable in a given hosting environment, and intake variants ("this request
came from a user, who may not be able to supply everything") cannot be reasoned about.

## Decision

Adopt a **three-layer reference discipline** with a **first-class dependency formalism**,
realized as four mechanisms in `foundation/` and proven by a new seed workflow:

### 1. Workflows reference controllers; controllers reference actions

- The **workflow** layer (YAML) is a *contract between controllers*: it declares which
  controllers are party to the lifecycle and in what phase order they engage. A phase
  step may be `- controller: <name>` (a `ControllerRefNode`); a workflow additionally
  carries a top-level `controllers:` list naming every party — including controllers
  that are only entered dynamically (supersede targets), so the contract surface is
  complete even where the phase graph is not.
- The **controller** layer (code) is the *coordination between actions*: a controller is
  registered in the `TokenRegistry` as a `ControllerSpec` — an ordered composition of
  action tokens (the same step grammar phases use, so `loop:`/`sequence:`/`parallel:`
  compose), plus any controller-level extra needs. Controllers are code-owned: end
  users compose workflows from controllers; extenders define controllers from actions.
- The **action** layer is the standard interface over implementation complexity: a
  manifest (interface rule) plus a registered body, exactly as today. Bodies reach
  implementation detail only through *services* (below), never by importing it.

Compilation preserves ADR-001's construct mapping: a controller reference compiles to a
named **OR-superstate** (a `Sequence` carrying the controller's name), so "Controller =
OR-superstate" now holds at the YAML surface too. Bare action tokens in phases remain
valid — the discipline is adopted incrementally: `baseworkflow`'s new `seed` phase is
the first controller reference; its `spec`/`work`/`build` phases still name actions
directly until their controllers are carved out (#188 leaf work).

One code path serves both static and dynamic composition:
`foundation.workflow.compile_controller(name, registry=…, manifests=…, factory=…)`
materializes a registered controller into a runnable `Control` — used by the
`CompileVisitor` for phase references *and* by run-time spawners (supersede targets).

### 2. Needs and Provisions — the dependency formalism

`foundation/needs.py` (stdlib-only leaf) defines the first-class objects:

- **`Need(kind, name, optional=False)`** — one declared dependency. Kinds:
  - `service:<domain>.<facet>` — a standard API service must be registered
    (e.g. `service:github.access`, `service:operator.interactive`).
  - `input:<key>` — a runtime input must be seeded onto the input shelf. Derived
    automatically from each manifest's `interface.in` refs that read the `input` shelf;
    declared explicitly only by `raw` actions (which self-manage I/O).
  - `config:<key>` — a key must be present in the workflow's seeded `config` mapping.
- **`Provision(kind, name, source)`** — one offered resource, same key space; `source`
  names the provider (a service class, `workflow.inputs`, `workflow.seed.config`).
- **`NeedSet`** — an immutable set with the composition algebra: union (`|`),
  `unmet(provisions)`, `satisfied_by(provisions)`. Optional needs never block
  satisfaction; they surface in reports.

The formalism composes upward:

- **an action's needs** = its manifest's declared `needs:` ∪ its derived input needs;
- **a controller's needs** = the union of its member actions' needs ∪ its spec's extras;
- **a workflow's needs** = the union over its declared controllers (and any bare
  action tokens still referenced in phases).

**Satisfied** is defined at each layer: a controller (or workflow) is satisfied by a
provision set iff every non-optional need is met. This yields **static validation**:
`python -m foundation.workflow <yml> --registry m:f --services m:f` checks structure +
data-flow + tokens + *needs satisfaction* without executing anything, and `--needs`
prints the per-controller needs report (each need annotated with its satisfying
provision or `UNMET`).

Two satisfaction questions, deliberately distinct:

- **Contract satisfaction** (static, strict): every declared controller satisfied —
  "this host can honor the whole contract." What the validator's PASS means.
- **Request satisfaction** (runtime, per-request): the needs of the *one* controller a
  request routes to, checked against the live provision set. A host with no interactive
  operator can still accept `job` requests; the seed's classify step computes exactly
  this.

### 3. Services split into access and interactive facets

`foundation/services/` (mechanism only) defines the standard API service layer actions
derive from:

- **`Service`** — identity (`domain`, `facet`, `id = domain.facet`) and the provisions
  it grants (`Provision("service", id)`).
- **`AccessService`** (facet `access`) — programmatic resource access: pure
  request/response against an external surface (an API, a queue, a filesystem). No
  human in the loop.
- **`InteractiveService`** (facet `interactive`) — a conversational surface that can
  *acquire* missing information at run time (`ask(...)`), which is why an interactive
  request "may not fulfill all requirements" up front and still proceed.
- **`ServiceRegistry`** — string-addressable (`domain.facet`) registration and lookup;
  its aggregate `provisions()` is the provision source for static validation. The
  registry is carried on the run `Context` (`ctx.services`); bodies resolve with
  `ctx.service("github.access")`, which raises a *needs-vocabulary* error when the
  service is absent — the runtime counterpart of static satisfaction.

Concrete services live with consumers (`seedwf/services.py`: `gh`-backed GitHub access,
TTY-backed operator interaction), mirroring how `TokenRegistry` bindings work; mock
services (`foundation/services/mock.py`: canned access responses, scripted interactive
answers) mirror `MockActionFactory` so the same controller code runs mock or real.
Splitting each domain's *interactive* surface from its *access* surface keeps the two
halves independently mockable, independently provisioned, and independently needed.

### 4. Superseedable states (the #190 contract)

The statechart gains **priority + preemption** semantics, wired through the external
event queue ADR-001 reserved:

- **`State.priority`** (default 0) — the preemption threshold a frame defends.
- **`SupersedeRequest(control, name, payload, policy, priority)`** — a first-class
  request to preempt. `control` is anything runnable (`run(payload, ctx)`); policies:
  - **`abandon`** — the superseding state takes over: the preempted superstate's
    remaining plan is abandoned and the superseder's `Result` becomes the superstate's
    result.
  - **`suspend`** — the superseder runs as an interruption; on its success the
    preempted plan *resumes* where it left off (history and shelf state untouched);
    its failure routes through the normal `error` edge.
- **`ctx.supersede(control, …)`** — the raising surface (sugar over
  `raise_event(EV_SUPERSEDE, …)`).
- **Preemption points are microstep boundaries.** RTC semantics are preserved: a step
  is never torn. After each child completes, the interpreter collects raised events and
  gives supersede requests first refusal — *before* named-event supervision and the
  ordinary `done`/`error` selection. A request preempts a frame only when
  `request.priority > frame.priority`; a refused request bubbles to the parent frame
  (the existing propagation), and one that reaches the root unconsumed is dropped with
  an audit entry. Every acceptance, rejection, and completion is recorded on the
  interpreter's event audit, and the superseding state is grafted into the chart's
  index, so `to_dict()`/`snapshot()` remain faithful (trace + shelf consistency).

### The redesigned base workflow — the seed controller

The base workflow itself is redesigned to **start with the seed controller**: a new
`seed` phase at the head of the lifecycle carries `- controller: seed.intake` (the
first controller reference in production YAML), and the workflow declares all four
seed controllers as parties. The seed is **not** a sibling engine — it is a controller
of `baseworkflow`, registered in `baseworkflow/bindings/seed.py`, entering the
lifecycle as a nested machine exactly like any controller reference. **Kernel code
initializes it**: the kernel builds the request the workflow boots on
(`DISPATCH_JOB_REQUEST=<json>` injects a non-interactive job request; a specified
issue becomes a GitHub-source request; engines synthesize a legacy job request for
existing job/triage callers) and registers the services
(`baseworkflow/services.py`). The request may originate from a **GitHub issue**, from
**user interaction**, or from a **non-interactive job request**.

The `seed.intake` controller (ingest → classify → route → fallback) normalizes the
request, computes *request satisfaction* (the matching variant's `NeedSet` against the
live provisions — a non-interactive job is **accepted or rejected** on exactly this
check), then **spawns a variation of itself** — `seed.handle.github` /
`seed.handle.interactive` / `seed.handle.job`, materialized from the same registered
specs via `compile_controller` — and **supersedes** its own processing with it
(`policy=abandon`): the variant takes over the request, and the generic fallback step is
abandoned. A rejected or unroutable request falls through to `fallback`, which records
the rejection (with the unmet needs) as a deliverable, and the workflow's
`terminal_when: seed_unrouted` completes the lifecycle there — spec/work/build never
run for a request that was never accepted. Every variant converges on
`seed:emit_work_item`, emitting the source-agnostic work item — the #188 seam: GitHub
issues become one intake among peers, and the engine's consumable is a generic envelope.

## Construct mapping (extension of ADR-001's table)

| Statechart construct | Glossary construct |
|---|---|
| OR-superstate named by a `controller:` reference | Controller (now referable from YAML) |
| Registered composition of action tokens | `ControllerSpec` (code-owned coordination) |
| Guard precondition, evaluated statically | Need / NeedSet satisfaction |
| What the environment offers a machine | Provision (services, inputs, config) |
| Higher-priority state preempting at a microstep boundary | Supersede (`abandon` / `suspend`) |
| Event carrying the preemption | `EV_SUPERSEDE` on the reserved external queue |
| Dynamic nested-machine entry | Grafted leaf running a spawned `Control` |

## Options considered

- **Controllers as YAML files** (a manifest per controller, like actions). Rejected for
  now: the operator direction is that coordination is *code* — controllers glue
  plumbing, and plumbing wants Python (predicates, spawning, service calls). The
  `ControllerSpec` step grammar is loader-compatible, so promoting specs to YAML later
  is additive.
- **Imperative controller builders** (`fn(factory) -> Control`). Rejected: opaque to
  static analysis — the needs union ("a controller's needs are the union of its
  actions'") must be computable without executing anything.
- **True asynchronous preemption** (interrupt a running leaf). Rejected: tears RTC
  semantics and every audit invariant; microstep-boundary preemption is the standard
  statechart answer and is exactly enough for routing/responder scenarios.
- **Supersede via chart-authored transitions only** (no dynamic spawn). Rejected: the
  seed must route to a *parameterized variation of itself* chosen at run time; a static
  transition table cannot name a state that does not exist until the request arrives.

## Consequences

- The workflow YAML becomes a readable contract: parties (`controllers:`) + engagement
  order (phases) + terms (budgets, inputs, seed). Plumbing moves to code where it can
  be composed, tested, and statically analyzed.
- Needs make hosting requirements explicit and machine-checkable; "can this
  environment run this workflow?" is now a validator question, not a runtime surprise.
  The same formalism gives intake accept/reject a principled definition.
- The service facet split gives every domain two independently-substitutable halves;
  actions stop caring whether data arrived via API or conversation.
- Supersede makes the engine reactive: per-workflow event responders (the sibling epic)
  now have their interruption mechanism. Cost: two policies to reason about; mitigated
  by the audit trail and priority gating.
- `baseworkflow` adopts the discipline first (the seed phase is the first controller
  reference; existing callers are unchanged — a legacy job/triage call synthesizes an
  accepted job-source request); `websitewf` inherits the seed phase through its
  overlay. Migrating the remaining spec/work/build action lists into controllers is
  future leaf work under #188.

## Action Items

1. [x] `foundation/needs.py` — `Need`, `Provision`, `NeedSet`, parse helpers (stdlib leaf).
2. [x] `foundation/services/` — `Service`/`AccessService`/`InteractiveService`,
   `ServiceRegistry`, mock family; `Context.services` + `ctx.service()` resolution.
3. [x] `TokenRegistry.register_controller` + `ControllerSpec`; loader `- controller:`
   step + top-level `controllers:`; `ControllerRefNode`; visitor expansion
   (validate/compile/render) + `compile_controller`.
4. [x] Needs derivation from manifests (`needs:` block + `interface.in`-derived input
   needs); `NeedsReport`; `--services` / `--needs` on `python -m foundation.workflow`.
5. [x] Supersede: `State.priority`, `SupersedeRequest`, `EV_SUPERSEDE`,
   `ctx.supersede`, interpreter preemption (priority gate, abandon/suspend, bubbling,
   audit) — `foundation/actions/test_supersede.py` covers accept/reject/policies.
6. [x] Redesigned base workflow: the `seed` phase (`- controller: seed.intake`) +
   declared parties + `terminal_when: seed_unrouted` in
   `app/workflows/baseworkflow.yml`; `app/config/actions/seed/*.yml`;
   `baseworkflow/bindings/seed.py` (controllers + bodies + predicate);
   `baseworkflow/services.py` (real + mock families); kernel request boot
   (`DISPATCH_JOB_REQUEST` / issue → github source) in `kernel/adhoc_session.py`;
   mock e2e over all three intake sources (`baseworkflow/test_seed.py`).
7. [x] Wire the new self-asserting tests + the seedwf validator into
   `app/scripts/smoke.py` (CI gate).
