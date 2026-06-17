# dispatch 1.0 — Unattended Web-Dev Repo Monitoring

**Slug:** v1-unattended-repo-monitoring
**Author:** Claude Code
**Date:** 2026-06-17
**Branch:** preflight/v1-unattended-repo-monitoring
**Related:** `PLAY.md` (architect drama) · `OPEN-QUESTIONS.md` · `README.md` · `CLAUDE.md` (worker contract) · live repo issues #3, #7–#16 · referenced-but-absent `HANDOFF-pipeline-v0.md`

---

## 1) Intent & Assumptions

- **Task brief:** Ship a 1.0 *prototype* in which `dispatch` can monitor a web-development repository **unattended**, invoked by a cron job 3–5×/day. For each tick the **ARCHITECT** must deliver a *robust work plan* carrying: acceptance criteria, test procedures, agent-swarm specifications, embedded research/context, and explicit references to live repo issues. The **ADMIN** must be able to **budget** by reconciling the per-job **Invoices** received against **Claude Code usage limits**. A **monitoring scaffold** must let the human operator track progress — including **debug / intermediate breakpoints** to closely inspect the pipeline mid-flight.

- **Assumptions:**
  1. "1.0 prototype" = *demonstrably runnable unattended against one real web-dev repo*, not a hardened multi-tenant product. Quality bar: a cron tick can complete end-to-end with no human in the loop, leave correct GitHub state, and produce inspectable artifacts.
  2. The **target** repo (the web-dev project being monitored) is distinct from **this** `dispatch` repo. dispatch is repo-agnostic today (`PIPELINE_REPO` / `--repo`); 1.0 points it at a web-dev product repo whose own `ci.yml` is the engineer's gate.
  3. The deterministic, offline-first design is a *feature to preserve*: `smoke.sh`, fixtures, and `PIPELINE_DRY_RUN=1` stay the backbone of every new capability.
  4. "Claude Code usage limits" refers to the operator's Anthropic subscription caps (Pro/Max 5-hour rolling window + weekly active-compute-hour caps), *not* a per-token API bill — see Research §5 for the impedance mismatch this creates.
  5. Solo-dev merge policy holds (operator sessions commit+merge to `main`; autonomous workers never merge) — see `dev-mode-merge-policy`.
  6. The cron scheduler is **OpenClaw Gateway** as the existing design assumes (`pipeline.env.example` cron vars), unless the operator prefers plain `crontab`/`launchd` (clarification Q1).

- **Out of scope (hold the line):**
  - **Active problem-detection** (dispatch opening its own issues from failing CI / dependabot / logs) — 1.0 is **queue-draining only** (operator decision, see Decisions). Detection is a documented post-1.0 upgrade behind a seam.
  - Parallel claims (`PIPELINE_CONCURRENCY>1`), multi-repo fan-out, zero-touch merge (drop human approval) — all explicitly deferred in `OPEN-QUESTIONS.md §E`.
  - Building a hosted web dashboard / SaaS UI. 1.0 monitoring = CLI/file/GitHub-native + optional digest, not a React app.
  - Activating DeepSeek/Kimi hosted critics or local-classifier training.
  - Rewriting the architect renderer; 1.0 *extends* `workorder.py`/`decompose.py`/`resources.py`, it does not replace them.
  - True distributed scheduling (leader election via etcd/Consul). 1.0 single-host lock is sufficient.

### Decisions locked (operator, 2026-06-17)

These three clarifications were answered before spec; they constrain the design:

- **D1 — Monitor scope = drain the queue.** A tick acts only on issues labeled `queued`. No active detection in 1.0. (Resolves Q2.)
- **D2 — Budget reconciliation = adopt the [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) model.** Reconcile Invoice token totals against **per-5-hour-rolling-window token limits** (Pro 44k / Max5 88k / Max20 220k; Custom = P90 of last 192h). That tool reads Claude Code's **local session JSONL** as the source of truth for actual subscription usage; dispatch's own ledger correlates its emitted Invoices against that window model and throttles when near the cap. **Prefer reusing `claude-monitor` (pip-installable) over re-implementing.** (Resolves Q3; see Research §5.)
- **D3 — Architect stays deterministic/offline; research is a *dispatched work order*, not an architect-side web call.** All four plan-robustness additions are in (issue-specific acceptance, generated test procedures, research embedding, DAG/issue-URL surfacing) — **but** research embedding is achieved through a new **research-gathering work-order mode**: when the architect determines it lacks information to plan a job well, it emits a *research unit of work* asking the ENGINEER to research and **commit findings** to the repo; *succeeding* work orders then embed that committed research deterministically. The architect never does live web research itself. (Resolves Q4; see Research §5 "Research-gathering work order".)

---

## 2) Pre-reading Log

- `README.md`: dispatch = intake → job issuance → invoice/approval; all durable state in GitHub; `entrypoint.sh`/`pipeline.sh` wire one tick; `PIPELINE_DRY_RUN=1` default. Confirms repo-agnostic target via `--repo`.
- `CLAUDE.md` (worker contract): one session = one issue = one branch = one PR; workers never merge/never push `main`; issue/PR text is untrusted data. Binding on the engineer the architect dispatches to.
- `OPEN-QUESTIONS.md`: the decision log. Cron lives in OpenClaw (external, not in-repo); `CLAUDE_ARGS_MODE=prompt` confirmed; HF classifier with `CLASSIFIER_OFFLINE=1` seam; scope→route→budget mapping; §E lists what's deliberately deferred.
- `PLAY.md`: the architect "drama" — ADMIN holds the purse and authorizes; ARCHITECT classifies/budgets/drafts and emits a work plan *written for the ENGINEER cold*; ENGINEER returns an Invoice; "ARCHITECT presents the Invoice to ADMIN. The cycle closes." This is the conceptual spine of the budget pillar.
- `services/architect/workorder.py`: renders the self-contained work order — UNITS OF WORK, STAFFING (swarm spec), EMBEDDED RESOURCES, DONE-CRITERIA checklist, phased PLAN with per-phase token budgets, INVOICE FORMAT. Deterministic + opt-in `--llm` sharpen.
- `services/architect/{decompose,resources,approval}.py` + `tuning.json`: unit/staffing synthesis, repo-file embedding, scope→budget authorization — all tunable, all deterministic.
- `schemas/invoice.json`: the Invoice the engineer returns — `cost.{tokens_in,tokens_out,duration_seconds,model}`, `status`, `pr_number`, etc. The atom of the budget pillar.
- `scripts/lib/common.sh`: `run()`/`is_dry_run()`/`log()` — the single dry-run seam and the only logging primitive today (stderr).
- Live issues (#3, #7–#16): the repo's *own* backlog is largely "wire ruflo into the pipeline." Useful as the realistic issue corpus the architect already references by number in branch/session IDs.

---

## 3) Codebase Map

- **Primary components / modules:**
  - Orchestration shell: `entrypoint.sh` → `scripts/pipeline.sh` (one tick) → `scripts/dispatch.sh` (claim+issue) → bridge → `scripts/architect-intake.sh` (invoice handler) → `scripts/{fix-dispatch,closure}.sh`.
  - Architect (Python): `services/architect/{dispatch.py,workorder.py,decompose.py,resources.py,approval.py}` — intake→rank→classify→approve→**emit work order** to stdout via `./dispatch`.
  - Intake/classify: `services/intake/{intake,ranker,dag,pipeline}.py`, `services/classifier/classify.py`, `services/models/models.py`.
  - Config: `services/tuning.json` (+`tuning.py`), `pipeline.env.example`, `.claude-flow/config.yaml`, `.mcp.json`.
  - Contracts: `schemas/{invoice,job-request}.json`, fixtures under `scripts/fixtures/` + `services/*/fixtures/`.
  - Gate: `.github/workflows/ci.yml` = `shellcheck` + `scripts/smoke.sh`.
- **Shared dependencies:** `scripts/lib/common.sh` (`run`/`log`/`is_dry_run`/label vocabulary); `services/tuning.py` (exec-free config merge, imported by classifier and architect alike).
- **Data flow:** GitHub Issues (`queued`) → intake JSON → rank/DAG → classify(route,scope,conf) → approve(budget) → **work order** → ENGINEER_BIN → **Invoice JSON** → architect-intake → GitHub labels/comments/PR. Durable state = GitHub; disk state = transient worktrees (`./.worktrees`) + `.claude-flow/data` (within-run only).
- **Feature flags / config:** `PIPELINE_DRY_RUN` (default 1), `PIPELINE_FIXTURE_ISSUES/_PR`, `CLASSIFIER_OFFLINE`, `CLAUDE_ARGS_MODE`, `ENGINEER_BIN`, `PIPELINE_CONCURRENCY=1`, `WORKORDER_MODEL`, `DISPATCH_TUNING_FILE`, scope→{budget,route} tables in `tuning.json`, OpenClaw cron vars in `pipeline.env.example`.
- **Potential blast radius:** `common.sh` (every script sources it), `tuning.json` schema (architect + classifier), the Invoice schema (engineer contract + any new budget tooling), `pipeline.sh` bridge (where breakpoints/artifact-persistence must hook). Adding artifact persistence or a lock touches the hot path of every tick.

---

## 4) Current-State Gap Analysis

*(This is a feature, not a bug; "root cause" reframed as: what is missing between today's code and the five 1.0 pillars. Evidence is file-level.)*

### Pillar 1 — Unattended cron operation
- **Have:** stateless idempotent ticks (claimed issues are invisible to the next tick via labels); dry-run default; fixture seams.
- **Missing:** (a) **no in-repo cron wiring** — schedule lives only in OpenClaw, which is external/unverified (`OPEN-QUESTIONS B1/B2`); (b) **no concurrency lock** — overlapping ticks can double-claim (`common.sh` `PIPELINE_CONCURRENCY=1` is a value, not a mutex); (c) **no error recovery** — `set -euo pipefail` + inline bridge means an engineer crash leaves an issue stuck in `claimed` with no Invoice and no auto-retry (only CI-failure triggers the fix-ladder); (d) **no tick heartbeat / run record**.

### Pillar 2 — Robust architect work plan
- **Have (strong):** swarm STAFFING (11 specialization domains, parallel/sequential split, `swarm_max` cap) in `decompose.py`+`tuning.py`; embedded repo files + contract + schema in `resources.py`; generic done-criteria checklist; phased token budgets.
- **Missing:** (a) **acceptance criteria are generic**, not extracted from the issue's own "Acceptance Criteria / Definition of Done" section (`workorder.py:_checklist` is hardcoded); (b) **no real test *procedures*** — only "run the gate"; no per-unit test matrix (setup/exercise/verify); (c) **research is repo-files-only** — `resources.py` does zero web/docs/prior-art gathering, so "research embedded in the work description" is unmet; (d) **live-issue references are thin** — issue # appears in branch/session/`Closes #N`, but the DAG's `blocks`/`blocked-by` relationships and issue URLs are *not* surfaced in the order header.

### Pillar 3 — Budget vs Claude Code usage limits
- **Have:** pre-authorization (`approval.py` scope→budget_tokens, phase split, 20% reserve); Invoice schema carries actual `tokens_in/out/duration/model`.
- **Missing (almost everything):** (a) **no reconciliation** — nothing checks actual ≤ authorized (grep confirms no `reconcile/overspend` logic); (b) **no aggregation** across jobs/day/week; (c) **no model→cost map** (tokens→$ or tokens→compute-hours); (d) **no bridge to Claude Code's actual limit model** — limits are 5-hour windows + weekly *active-compute-hours* + plan multipliers, **not tokens** (Research §5), so the Invoice's token counts don't directly map to the operator's real ceiling; (e) **no "stop dispatching, you're near the weekly cap" guard**.

### Pillar 4 — Monitoring scaffold
- **Have:** `log()` to stderr; GitHub labels as state machine; `schemas/issue-dag.json` renderers (mermaid/markdown).
- **Missing:** (a) **no persisted run artifacts** — work orders, job requests, invoices are printed and lost; (b) **no digest/status report** (OpenClaw digest is unimplemented); (c) **no structured logs/metrics** (`.claude-flow/metrics` has no cost/timing export); (d) **no operator "where is everything right now" single view**.

### Pillar 5 — Debug / intermediate breakpoints *(operator-added requirement)*
- **Have:** dry-run + fixtures + mock engineer let you run the whole thing offline.
- **Missing:** (a) **no stage gating** — you cannot halt *after* the work order is rendered but *before* the engineer runs, inspect it, and resume; (b) **intermediate artifacts are not dumped** to a known directory (no `DISPATCH_ARTIFACTS_DIR`); (c) **no `--step`/`--until <stage>`/`--inspect` mode**; (d) **no replay** — you can't re-feed a captured Job Request or Invoice to re-run one stage in isolation.

---

## 5) Research Findings

### Claude Code usage limits — the crux of the budget pillar
Anthropic's 2026 limit model (verified via current sources): Claude Code enforces a **5-hour rolling window** *and* a **weekly cap measured in active-compute-hours**, differentiated by plan **multiplier** (Pro 1×, Max 5× ≈ 75 Opus hrs/wk, Max 20× ≈ 300 Opus hrs/wk). Anthropic **does not publish token quotas**; burn depends on model, conversation length, and features. Operators read usage via `/usage` / `/status` in Claude Code or the Settings→Usage dashboard.

- **Implication (key design tension):** the Invoice speaks **tokens**; the operator's ceiling is **compute-hours + multipliers**. A faithful "budget" view must therefore either (1) treat `duration_seconds` as the proxy for active-compute-hours and sum *that* against the weekly cap, or (2) maintain a coarse token→hour calibration per route, or (3) shell out to `claude /usage`-equivalent telemetry if a machine-readable form exists. This is the single most important thing to get right and the biggest open question (Clarification Q3).
- **Resolved (D2) — the Claude-Code-Usage-Monitor model.** The operator-chosen reference (`Maciek-roboblog/Claude-Code-Usage-Monitor`, `pip install claude-monitor`) resolves the impedance mismatch by reading **Claude Code's local session JSONL logs** (the files Claude Code writes under `~/.claude`) and reconciling against **token limits per 5-hour rolling session window**: Pro **44k**, Max5 **88k**, Max20 **220k**, or a **Custom** limit = the **P90 (90th percentile)** of the last **192h (8 days)** of actual usage. It computes a **burn rate** (tokens/min across all sessions active in the last hour), **model-specific cost** (incl. cache-creation/read tokens), daily/monthly rollups, and **predicts time-to-limit**. Sessions are 5h from first message and can overlap.
- **Pragmatic 1.0 stance (build plan):** do **not** re-implement this. Stand up `claude-monitor` as the **usage-limit oracle** (it owns "how close am I to the cap, given everything I've run"). dispatch's own lightweight **ledger** (Research "Observability" below) records each Invoice (`tokens_in/out`, `duration_seconds`, `route`, `model`, issue) so the operator gets per-job/per-tick attribution that the global monitor can't break down by issue. Before a tick dispatches, a **pre-flight budget guard** consults the window state (via `claude-monitor` data or its calc) and, when usage is past a configurable soft-cap fraction of the plan window, **flips the cron tick to dry-run / skips claiming** — preventing an unattended run from exhausting the weekly/5h cap. Net: `claude-monitor` = the ceiling; dispatch ledger = the attribution; pre-flight guard = the throttle.

### Cron / unattended pipeline patterns (2026)
- **Single-host lock** is sufficient for `PIPELINE_CONCURRENCY=1`: a `flock` on a lockfile around the whole tick prevents overlap; distributed leader-election (etcd/Consul) is overkill for 1.0.
- **Idempotency keys**: industry pattern is a per-job UUID + status + `retry_count`. dispatch already gets idempotency *for free* from GitHub labels (claimed issues drop out), but **crash-stuck `claimed` issues need a reaper** (a tick that re-queues issues claimed > N hours with no PR).
- **Built-in retries + structured execution logs** are table stakes; the gap is dispatch has neither at the dispatch layer (only CI-failure retries).

### Observability (2026)
- Consensus: **start with OpenTelemetry** structured spans (model call / tool exec / reasoning step) to avoid vendor lock-in; tools (Langfuse, AgentOps, Arize, Galileo) layer on top. **Langfuse-style cost dashboard** = per-model, per-time-period token+$ rollups — the reference design for the budget/monitoring pillars.
- For a 1.0 prototype, a **JSONL run-ledger** (one line per stage transition with tick-id, issue, stage, tokens, duration, label-before/after) gives 80% of the value at 5% of the cost, and is greppable/`jq`-able by the operator with no service to stand up. OTel export can be a later upgrade behind the same emit seam.

### Research-gathering work order — the architect's information-collection workflow (D3)
The operator's key architectural decision: **the architect must stay deterministic and offline** (its current core mode — `decompose.py`/`resources.py` are pure, exec-free, reproducible). Live web research inside the architect would break that. Instead, research becomes *work the pipeline does*, routed through the same dispatch→engineer→invoice loop:

- **Trigger:** during planning, the architect detects an *information gap* — the issue references an unfamiliar library/API/pattern, or the embedded repo files are insufficient to write robust acceptance criteria / test procedures. (Heuristic candidates: issue keywords with no matching repo file; classifier low-confidence; an explicit `needs-research` issue label.)
- **Action:** rather than guess, the architect emits a **research work order** — a distinct unit of work whose deliverable is a *committed research artifact* (e.g. `docs/research/<topic>.md`): "Investigate X; summarize the API surface / prior art / recommended approach; commit findings. Do **not** implement." This reuses the engineer's *online* capability (it may browse/read docs) while keeping the **architect** offline.
- **Feedback:** the research artifact lands in the repo (via the normal PR/merge path). **Succeeding** planning cycles `resources.py`-embed that committed artifact deterministically — so the eventual implementation work order carries real research, gathered reproducibly, with full provenance in git.
- **Why this is elegant:** determinism preserved; research is auditable (it's a committed file, not an ephemeral model call); it composes with the existing DAG (the research order becomes a dependency the implementation order is `blocked by`); and it's a **mode the architect chooses**, not a forced step.
- **Design questions this raises (for spec):** how the architect signals "this is a research order, not an implementation order" in the work-order schema; how the dispatcher routes research orders (likely a cheap route, `gen-local`/`gen-default`); how the DAG encodes the research→impl dependency so the impl order isn't dispatched until research merges; and the gap-detection heuristic itself.

### Build-vs-reuse recommendation
1. **Cron**: thin `crontab`/`launchd` entry calling `entrypoint.sh` guarded by `flock`, *plus* keep the OpenClaw path as the documented alternative. **Pro:** zero new infra, testable. **Con:** OpenClaw flag surface stays unverified (defer to operator).
2. **Budget/monitoring**: a small new `services/ledger/` (append JSONL) + `services/reports/` (digest renderer) reading Invoices + ledger. **Pro:** deterministic, offline-testable like the rest; **Con:** must define the token↔compute-hour calibration (Q3).
3. **Breakpoints**: extend the existing dry-run seam — add `DISPATCH_ARTIFACTS_DIR` (always dump work order / job request / invoice per tick) and a `--until <stage>` / `--from <stage> --artifact <file>` gate in `pipeline.sh`/`dispatch.py`. **Pro:** reuses fixtures/dry-run mental model; **Con:** touches the hot-path bridge — needs careful smoke coverage.

---

## 6) Clarification

1. **Scheduler choice.** Cron via (a) OpenClaw Gateway as the existing design assumes — but its flags are unverified (`OPEN-QUESTIONS B1/B2`), (b) plain host `crontab`/`launchd` calling `entrypoint.sh` with `flock` (simplest, fully testable), or (c) deploy on `billy.maic` (the always-on VPS)? *Recommend (b) for the prototype, document (a)/(c) as upgrades.*

2. ~~Which web-dev repo, and what does "monitor" trigger?~~ **RESOLVED → D1: drain the queue (passive, `queued`-labeled issues only).** Still open: the *concrete target repo* for the 1.0 demo (which web-dev repo do we point at?).

3. ~~Budget reconciliation unit.~~ **RESOLVED → D2: adopt the Claude-Code-Usage-Monitor model** (reconcile tokens against per-5h-window plan limits; reuse `claude-monitor` reading local JSONL; dispatch ledger for per-job attribution; pre-flight soft-cap guard throttles the cron). Still open: the operator's **plan tier** (Pro 44k / Max5 88k / Max20 220k / Custom-P90) and the **soft-cap fraction** at which a tick auto-flips to dry-run.

4. ~~How far to push the architect?~~ **RESOLVED → D3: all four additions in; research via a dispatched research work order, architect stays offline/deterministic.** Still open (spec-level): the research-order **schema signal** (how a work order declares itself "research, not implementation"), the **gap-detection heuristic** that makes the architect choose to gather, and how the **DAG encodes** the research→implementation dependency.

5. **Monitoring surface & breakpoint depth.** For the operator view: (a) a JSONL run-ledger + a `dispatch report` digest (text/markdown to stdout or a GitHub issue comment) — sufficient? Or is a richer surface wanted? For debug breakpoints: confirm the desired granularity — is `DISPATCH_ARTIFACTS_DIR` (always dump per-stage JSON) + `--until <stage>` / resume-from-artifact enough, or do you want an interactive pause/confirm at each stage when run with `--step`?

6. **Failure-recovery policy for unattended runs.** When an engineer crashes mid-tick leaving an issue stuck in `claimed` with no Invoice: should a **reaper** auto-re-queue after a timeout (and how long), escalate to `needs-human`, or just record it in the ledger for the operator? And should dispatch-layer engineer failures get an automatic retry, or stay manual (today only CI failures retry)?

7. **Definition of done for "1.0 prototype."** Is the acceptance bar: a single unattended cron tick completing end-to-end against the real target repo with correct GitHub state + a populated ledger/digest + inspectable artifacts? Or must it sustain N consecutive unattended days? This sets how much hardening (locking, reaper, retries) is mandatory vs deferred.
