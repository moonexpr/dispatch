# Plan — dispatch full prototype (work-order emission)

**Goal:** `./dispatch` invocation emits a **finished work order** (the ARCHITECT's
WORK PLAN, PLAY.md Act III) to stdout. Completes issue #7's two unchecked
subsystems: **work approval** (Act II) + **job issuance** (Act III). Prompt
crafted per **prompt-master** (issue #8) Template H (ReAct + Stop Conditions) /
Template M (Opus Task Brief).

## Decisions (locked with PROMPTER 2026-06-16)
- **Emission engine:** deterministic template engine (default, offline,
  reproducible) + optional `--llm` upgrade via `services/models/models.py`.
  Mirrors the classifier/ranker pattern (deterministic core + LLM upgrade).
- **Demo input:** PLAY.md-style fixture queue is the offline default; live
  `--repo` / `--project` supported. Acceptance bar = a **structurally complete**
  work order for the selected primary job (not a byte-match of PLAY.md prose).

## Pipeline (reuse + add)
```
intake.py (✅ data sourcing)            reuse
  → ranker.rank() + RANKER_OFFLINE      EXTEND (add deterministic dep-parse)
  → classify.py (✅ triage)             reuse (subprocess, quarantine reader)
  → architect/approval.py               NEW  (work approval: budget/route/authorize)
  → architect/workorder.py              NEW  (job issuance: Template H/M work order)
  → architect/dispatch.py (orchestrator+CLI)  NEW
  → ./dispatch (root wrapper)           NEW
```

## New modules
- `services/architect/approval.py` — deterministic `approve(job, triage) ->
  Authorization{budget_tokens, route, dry_run, branch, session_id, constraint,
  stop_condition, phase_budgets}`. Scope→budget: xs 20k / s 40k / m 80k / l 160k.
- `services/architect/workorder.py` — `render(job, triage, auth, *, llm=False)
  -> str`. Sections: boxed header · OBJECTIVE · STARTING STATE · DONE-CRITERIA
  checklist · ALLOWED · FORBIDDEN (ADMIN constraint) · STOP CONDITIONS · phased
  PLAN (budgeted) · INVOICE FORMAT (schemas/invoice.json instance). `--llm`
  builds a prompt-master-derived meta-prompt → `models.complete()` → sharpened
  text; falls back to deterministic on any failure. Issue text is data only.
- `services/architect/dispatch.py` — orchestrator + CLI: intake → rank →
  classify → approve → workorder. Flags: `--repo`/`--project`/`--fixture`,
  `--all`, `--llm`, `--json`, `--confidence`. stdout = work order; stderr =
  ADMIN/ARCHITECT reasoning summary (ranked queue + selection + budget).
- `services/architect/fixtures/play-queue.json` — small queue modeled on
  PLAY.md (foundational intake job + docs tail + dependents) that
  deterministically selects the intake job as primary (scope m, gen-default, 80k).
- root `dispatch` — `exec python3 services/architect/dispatch.py "$@"`.

## Edits
- `services/intake/ranker.py` — add `RANKER_OFFLINE=1` deterministic ranking:
  regex-parse "blocked by #N / depends on #N / requires #N / parent epic: #N /
  follow-up to #N", topological order (foundational→dependent), tie-break lower
  number first. Same public `rank()` signature; LLM path unchanged.
- `scripts/smoke.sh` — new section "§7.x dispatch work-order emission":
  `./dispatch --fixture …` exits 0, stdout contains WORK PLAN / OBJECTIVE /
  STOP CONDITIONS / `Closes #<primary>` / route / `DRY_RUN=1` / Invoice; two
  runs byte-identical (deterministic).

## Acceptance
1. `./dispatch --fixture services/architect/fixtures/play-queue.json` exits 0,
   emits a complete work order to stdout for the foundational primary job.
2. Output deterministic across two runs (offline).
3. `bash scripts/smoke.sh` → 0 FAIL (existing + new section).
4. `--json` wraps `{…job fields…, "work_order": "<text>"}`; `--all` emits the
   ranked eligible set; `--llm` seam present with graceful fallback.

## Out of scope (note, do not build)
- Bundling logic (PLAY.md's D+E same-PR pairing) — selection is mechanical
  (rank + classify + threshold), primary = top-ranked eligible.
- Actually invoking ENGINEER / opening PRs from this program — it stops at the
  work order (that is the deliverable).
- Vendoring the full prompt-master skill — `--llm` uses an inlined,
  prompt-master-derived system prompt; full vendoring deferred.
