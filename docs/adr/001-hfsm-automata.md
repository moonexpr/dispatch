# ADR-001: Adopt Harel Statecharts (HFSM) as the Dispatch Controller Automaton

**Status:** Accepted
**Date:** 2026-06-26
**Deciders:** JC (architecture owner)
**Supersedes:** —
**Related:** Controller/Action glossary spec; `engine/actions` Abstract Factory addendum; Claude Agent SDK orchestration addendum

---

## Context

The dispatch engine models work as **Controllers** — control structures whose body is Sequences and Loops over **Actions** (`Procedure` = deterministic leaf, `Inference` = agent leaf, `Program` = nested Controller, the sole recursion point). Each Controller carries three **Shelves** (`input`, `deliverables`, `shared`), and **Governors** (Decorators) enforce budget, permission, and iteration caps. `BaseController` exposes a fixed `spec → work → build` phase skeleton; `BaseWorkflow` fills it with the Architect/Admin/Engineering cycle. The Architect emits a serialized `Program` (an orchestration script of Claude Agent SDK `AgentDefinition`s) that the Admin deserializes and runs.

Two forces now exceed what informal "sequences and loops" can carry:

1. **A forward requirement.** Sets of Controllers must eventually run as peers, **communicate**, and **switch control between each other** to form richer automata. Nothing in the current model provides a channel for one Controller to observe or trigger another.
2. **Durability.** The broader pipeline already treats GitHub Issues as a durable state machine. A Controller whose Engineering phase dies mid-execution must **resume from its deepest active position**, not restart.

Additional constraints in play:

- **Heterogeneous step latency** — an `Inference` leaf runs for seconds to minutes; a `Procedure` leaf runs in milliseconds. Any concurrency model must tolerate wildly uneven step durations.
- **Inspectability / serialization** — the orchestration script must be data the Architect can emit and the Admin can deserialize, not host-language control flow.
- **SDK constraint** — a Claude Agent SDK subagent cannot obtain approval mid-task; approval gates must live in the parent, never inside a spawned subagent.
- **Deny-by-default** permission gating and **per-phase budget** enforcement must compose over every Action type uniformly.

The decision under record is the **formalism that governs Controller control flow and inter-Controller coordination**, and the execution model that realizes it.

---

## Decision

Adopt **Harel statecharts** (a hierarchical finite state machine: hierarchy + orthogonality + broadcast + history + guards, under run-to-completion semantics) as the automaton model for the dispatch engine, realized with:

- **Externalized, first-class transitions.** A transition is an object `(event, guard, source, target, action)`, not control flow buried inside a Controller's body.
- **A single run-to-completion (RTC) interpreter** that owns the agenda and drives a **serializable active configuration**. Controllers never invoke one another directly.
- **Addressable states.** Every Controller and Action has a stable ID so a transition elsewhere can target it.
- **A construct mapping** onto the existing glossary (below), so this is a reinterpretation of the current model, not a rewrite of its vocabulary.

The serialization target aligns with **SCXML / UML 2 behavioral state machines**, so the Architect's orchestration script becomes a serialized statechart fragment.

Crucially, v1 **builds the seams** for peer communication (externalized transitions, addressable IDs, single RTC interpreter, a reserved event queue) even though the broadcast bus and cross-region guards stay **stubbed** until multiple Controllers actually run concurrently. These seams are cheap to add now and effectively unbuildable-around later.

### Construct mapping (statechart ← glossary)

| Statechart construct | Glossary construct |
|---|---|
| OR-superstate (composite) | Controller |
| Depth / nested machine entry | `Program` action (sole recursion point) |
| Simple (leaf) state | `Procedure` / `Inference` action |
| Transition trigger | Action `Result` (`output` → success edge, `error` → failure edge) |
| Guarded self-transition | constrained `Loop` (constraint = guard) |
| Transition guard | `Governor` (permission/budget/iteration); deny-by-default = guard defaults false |
| Extended state (data model) | Shelf |
| Orthogonal AND-regions | `parallel: true` phases |
| Deep history (H\*) | Durability / resume seam |
| Event broadcast + `in(state)` guards | inter-Controller communication (forward requirement) |

---

## Options Considered

### Option A: Plain nested control flow (call stack) — status quo

Each Controller runs its own Sequence/Loop in host code and returns; `Program` recursion is an ordinary function call.

| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Serializability | None (state lives on the call stack) |
| Resumability | None |
| Concurrency | Ad hoc |
| Peer communication | Unreachable |
| Team familiarity | High |

**Pros:** Simplest possible; no interpreter to build; fastest to a first running cycle.
**Cons:** Control flow cannot be serialized, so the Architect's orchestration script can't be data. A pipeline that dies mid-execution restarts from zero. There is **no seam** for Controllers to observe or trigger one another — the forward requirement is structurally impossible without a rewrite.

### Option B: Flat finite-state machine

A single-level FSM; no nesting, no concurrency.

| Dimension | Assessment |
|---|---|
| Complexity | Low–Medium |
| Serializability | Good |
| Resumability | Good (single active state) |
| Concurrency | None (state explosion to fake it) |
| Peer communication | Possible but unstructured |
| Team familiarity | High |

**Pros:** Serializable and resumable; well-understood; decidable analysis.
**Cons:** No hierarchy means the Architect→Admin→Engineering nesting must be flattened, and every combination of phase × sub-state becomes an explicit state — the exact combinatorial blow-up Harel introduced statecharts to eliminate. No orthogonality for `parallel` phases.

### Option C: Behavior Tree

The dominant orchestration model in robotics/game AI and several agent frameworks: a tree of composite (sequence/selector/parallel) and leaf (action/condition) nodes, re-ticked each cycle.

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Serializability | Good |
| Resumability | Weak (trees model *doing*, not *being*; no native history) |
| Concurrency | Good (parallel nodes) |
| Peer communication | Weak (trees don't natively reference each other's state) |
| Team familiarity | Medium |

**Pros:** Excellent for reactive, composable agent behavior; naturally parallel; widely used for exactly this "orchestrate agents" use case; easy to author and serialize.
**Cons:** A behavior tree models **activity**, not **state** — it answers "what should I do next" rather than "where am I." It has no first-class notion of *being in a state*, so deep history / resume must be bolted on, and the forward requirement ("Controller A switches to Controller B based on B's current state") has no native expression: trees don't observe one another's configuration. Coordinating peers via a shared blackboard is possible but reinvents broadcast informally.

### Option D: Harel Statechart / HFSM — **chosen**

Externalized transitions + RTC interpreter over a serializable configuration, as specified above.

| Dimension | Assessment |
|---|---|
| Complexity | Medium–High (an interpreter must be built) |
| Serializability | Excellent (SCXML/UML-aligned) |
| Resumability | Excellent (deep history restores nested configuration) |
| Concurrency | Native (orthogonal regions) |
| Peer communication | Native (broadcast events + `in(state)` guards) |
| Team familiarity | Medium |

**Pros:** Every existing construct maps onto a named statechart primitive. Depth, orthogonality, history, and broadcast are exactly hierarchy, `parallel`, resume, and the forward requirement. The serialized machine doubles as the Architect's orchestration script. Discrete skeleton stays analyzable when kept separate from activities.
**Cons:** Requires building and maintaining an RTC interpreter — real upfront cost. Demands committing to event semantics (synchronous vs. asynchronous) before peer communication ships. With unbounded shelf data in guards, the machine is no longer a finite-state automaton and loses general decidability (see Consequences).

---

## Trade-off Analysis

The decision turns on two requirements the status quo cannot satisfy at any price short of rewrite: **resumability** and **peer communication**. Options A and C fail the second natively; A and B fail concurrency or serialization; B fails hierarchy. Only D provides a *named, canonical* primitive for each of the four forces (hierarchy, concurrency, broadcast, history) rather than an informal reinvention.

The closest real contender is **C (Behavior Tree)**, and the distinction is worth stating precisely because it is the crux: behavior trees are about *doing*, statecharts about *being*. The forward requirement is phrased entirely in the language of *being* — "Controllers communicate and **switch between** each other," which presupposes that a Controller **has an addressable, observable current state** that another can guard on. That is a statechart's native vocabulary and a behavior tree's blind spot. Likewise, "resume the dead pipeline where it left off" is deep history (H\*), a first-class statechart pseudostate with no behavior-tree equivalent.

The cost paid for D is the **interpreter** and the **event-semantics commitment**. The interpreter cost is the same reified-execution investment already implied by the durability requirement (you cannot serialize and resume a call stack), so it is not a *new* cost — it is the cost of resumability, which any non-A option pays. The event-semantics commitment is deferred but not avoided: building the seams now (externalized transitions, addressable IDs, single RTC loop, reserved event queue) keeps the broadcast model open without forcing the synchronous-vs-asynchronous choice until peers actually run.

---

## Consequences

**Becomes easier**

- The Architect's orchestration script and the runtime automaton become **the same serializable artifact** (an SCXML-aligned statechart fragment).
- **Resume-after-failure** is a first-class operation: deep history restores the nested active configuration, satisfying the Issues-as-durable-state-machine bias.
- The SDK's "no mid-task subagent approval" constraint is **enforced by construction**: the approval gate is a parent-level guarded transition, never reachable inside a subagent leaf.
- **Governors unify** as transition guards; deny-by-default is simply guards defaulting false.
- The future **peer-communication feature is additive** — it activates the reserved event queue and `in(state)` guards rather than restructuring Controllers.

**Becomes harder**

- An **RTC interpreter** must be built, tested, and owned — meaningful upfront work before the first cycle runs end-to-end.
- Two semantic decisions become load-bearing and must be pinned (see Action Items): **synchronous vs. asynchronous broadcast**, and the **shared-shelf concurrency contract**.
- Authoring control flow now means authoring **transitions and guards**, a higher-ceiling, less-familiar idiom than inline code.

**Must revisit**

- **Decidability.** With unbounded shelf data readable in guards, the model is an extended/communicating state machine — Turing-equivalent — so reachability and deadlock-freedom are undecidable in general. If model-checking over the Controller graph is ever wanted, restrict guards to a decidable fragment over finite shelf abstractions, and keep the **transition/guard layer formally separable from the activity layer** so the discrete skeleton stays analyzable even when activities do not.
- **Event semantics** (sync/async) should be re-evaluated once real multi-Controller workloads exist and their latency profiles are measured.

---

## Action Items

1. [ ] Define first-class `Transition`, `Guard`, `Event` types and the RTC `Interpreter.step()` contract over a serializable configuration.
2. [ ] Assign every Controller and Action a stable, addressable ID.
3. [ ] Route all `Result` outcomes, Governor decisions, and abort conditions through the transition/guard layer — no ad-hoc branches in Controller bodies.
4. [ ] Reserve an event-queue seam in the interpreter (single region active in v1; N regions later).
5. [ ] **Pin event semantics:** default to **asynchronous RTC** (queued events, one machine-step at a time) given heterogeneous Inference/Procedure latency; record the rationale.
6. [ ] **Pin the shared-shelf contract:** RTC-serialize all writes to the `shared` shelf specifically, so `in(state)`-style guards never observe a torn write (other shelves may remain last-write-wins).
7. [ ] Define the SCXML-aligned serialization schema for the Architect's emitted statechart (states, transitions, guards, history pseudostates).
8. [ ] Keep the broadcast bus and cross-region `in(state)` guards **stubbed** behind the reserved seam until a second concurrent Controller exists.
9. [ ] Add a regression test: kill a Controller mid-`work` phase and assert deep-history resume restores the nested active configuration.