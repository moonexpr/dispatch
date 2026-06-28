# HANDOFF — WebsiteWF overlay engine + live-tick wiring

**Date:** 2026-06-27 · **Branch:** `websitewf-impl` (continuously ff-pushed to `main`) ·
**Worktree:** `.worktrees/websitewf-impl` (off `origin/main`).
**Last commit on main:** `e0d49a0` (kind fix + TODO). `main` is **fast-moving** (many
parallel sessions) — always `git fetch` + rebase onto `origin/main` and ff-push; **no one
force-pushes** main, so a plain ff is durable. Inference uses the **Claude Code
subscription** (`MODELS_BACKEND=cli` + `CLAUDE_CODE_OAUTH_TOKEN`), not API billing.

---

## What is DONE and on main

1. **ADR-002 overlay engine (fully implemented, validated).** `docs/adr/002-websitewf-overlay-layer.md`.
   - `engine/workflow/overlay.py` — merges `extends:` + `overlays:` with four verbs:
     `replace` / `extend` (before|after) / `add` / `proxy`. Wired into `engine/workflow/loader.py`.
   - **`proxy` action kind** — `Proxy` in `engine/actions/action.py` + `factory.proxy()` +
     CompileVisitor `_build_proxy` + `proxy_manifest()` in `manifest.py`. Reroutes shelf I/O
     around a target (works for `raw` targets — shelf-level copy before/after).
   - **WebsiteWF engine** — `src/websitewf/` is a sibling of `src/baseworkflow/`:
     `websitewf.py` (subclasses `BaseWorkflow` via the new `_load_doc`/`_build_registry`/
     `_total_budget` hooks), `bindings.py` (reuses base `build_registry` + `web:*` bodies),
     `actions/*.yml` (web manifests). Overlay doc: `app/workflows/websitewf.yml`.
   - **Selection:** `DISPATCH_ENGINE=websitewf` (base = `baseworkflow`; `visitor` disabled).
   - **Tests:** `src/websitewf/test_websitewf.py` (23/23) proves all four verbs + proxy +
     bridge selection. `python3 -m engine.workflow app/workflows/{baseworkflow,websitewf,engineer}.yml`
     all `[PASS]`.

2. **dispatch.py is now the router (visitor tick retired).** It fetches the issue, builds
   `job`+`triage`, selects the engine, and runs `run_live` in-process. The engine's Admin
   build phase owns the gh lifecycle (gated by `PIPELINE_DRY_RUN`; `-l/--live` mutates).
   - Defaults `MODELS_BACKEND=cli`, loads `pipeline.env` (GITIGNORED; holds
     `CLAUDE_CODE_OAUTH_TOKEN` + `MODELS_BACKEND=cli`), line-buffers stdout/stderr.
   - **Debug scaffolding:** `-v`/`--verbose` (or `DISPATCH_DEBUG=1`) →
     `_debug_dump` (decomposition + trace + first-failure) AND a **streaming per-action trace**
     in `engine/actions/action.py` `Action.run` (`→ start` / `← [ok|FAIL Ns]`, depth-indented).
     This is how the hang below was found.

3. **Architect/admin/web "inference" actions reclassified to `procedure`** (commit `e0d49a0`).
   Their bodies are deterministic (decompose/classify/bucket/author/draft/adversarial/
   work-units/scaffold) — as `kind:inference` the live `RealActionFactory` IGNORED the body and
   fired an empty-prompt `claude -p` per action, rate-limiting the subscription and hanging at
   `architect:select_bucket`. Only `engineer:run` stays `inference`. e2e 57/58, websitewf 23/23.

## How to run a tick
```
cd .worktrees/websitewf-impl            # pipeline.env (token) lives here, gitignored
DISPATCH_ENGINE=websitewf python3 dispatch.py -r ReclaimByDesign/dispatch-testrepo-c 1 -v        # dry-run, verbose
DISPATCH_ENGINE=websitewf python3 dispatch.py -r ReclaimByDesign/dispatch-testrepo-c 1 --live -v # LIVE (mutates gh)
```
Dry-run is fully green end-to-end (all 4 overlay verbs, 2-agent decomposition, full lifecycle).

---

## OPEN ISSUES / NEXT STEPS (priority order)

1. **✅ RESOLVED (2026-06-27) — Engineering phase now does real work under live; full live
   tick engineers a change + opens (and auto-merges) a PR.** Fix: `build_execute_orchestration`
   in `src/baseworkflow/bindings/engineer.py` is now gated on `ctx.dry_run`:
   - **live** (`dispatch --live`, `ctx.dry_run` False) → drives the real `engineer.yml`
     lifecycle via the module's `run_live(job)` (clone → cut `pipeline/issue-<n>` → run the
     engineer agent on the subscription → judge/commit/contamination-scan → PUSH), then writes
     the Invoice as `engineering_result = {ok, value: invoice, meta:{status, branch, summary}}`
     so `admin:consolidate_pr` opens ONE PR from the pushed branch and `admin:intake_invoice`
     arms `--squash` auto-merge. The work phase enriches `job` with the architect's
     `deliverables.plan` units before calling `run_live`.
   - **dry-run / mock** (`run_mock`, `run_live(dry_run=True)`, `ctx.dry_run` True) → unchanged:
     deserialize the architect's `orchestration_script` and run it in-process (the depth
     operator; the path the e2e/mock suites pin — no real model call, depth>0).
   - **(a) the websitewf proxy** rewires `orchestration_script ← web_route_spec`; the live
     path **no longer reads `orchestration_script`**, so that broken in-rewire is now harmless
     (the out-rewire still mirrors `engineering_result → web_engineering_result`). The proxy
     remains a mechanism demo; no rework needed for the live path to work.
   - **VERIFIED LIVE** on `ReclaimByDesign/dispatch-testrepo-c`:
     - `baseworkflow` engine, issue #14 → **PR #16 MERGED**, issue #14 CLOSED (index.html
       metadata + favicon.svg + og.png).
     - `websitewf` engine, issue #13 → all 4 overlay verbs ran → **PR #17 MERGED**, issue #13
       CLOSED (index.html responsiveness). e2e 57/58, websitewf 23/23 (no regression).
   - Single engineer agent per tick (no runaway fan-out). NOTE: the test repo has no branch
     protection, so intake's `--squash --auto` merges immediately.

2. **Admin "request project info" early gate (DESIGNED, NOT BUILT).** Operator-approved:
   *base admin* action, *spec-phase early gate*. When the target repo is **sparse / new /
   missing a clear goal**, open a GitHub issue requesting project information and short-circuit
   the tick (instead of sailing to a vacuous result). dispatch-testrepo-c is exactly this case
   (empty repo). Detection: `gh api repos/{repo}` (description, size, created_at) + contents
   file count + README presence. Gate it before `work`/`build` via the workflow's
   `terminal_when:` early-exit.

3. **`SerializedShelf` regresses 1 base e2e check** (commit `b28c441`, operator's ADR-001 #6,
   not part of ADR-002). Base e2e is 58/58 WITHOUT the `SerializedShelf` wrap in
   `engine/actions/factory.py` `shelves()`, 57/58 WITH it. The failing check has no `[FAIL]`
   marker (one check silently doesn't run). Needs a fix-up pass.

4. **Bridge doesn't fold web deliverables.** `src/visitor/orchestration/baseworkflow_bridge.py`
   `author_via_workflow` folds only `orchestration_script`/`work_plan`/`plan` into the Job
   Request, not `web_route_spec`/`scaffold_result`. (Minor; the router runs run_live directly so
   it sees all deliverables — this only matters if the bridge path is used.)

5. **WebsiteWF epic #165 + children #166–#170** (on `ReclaimByDesign/dispatch`, milestone
   "Babcock Release: Web Development Suite", all labeled `Release`) — the 5 deferred web use
   cases. ADR-002 action items #1–#5 are `[x]`; #6–#7 remain.

6. **Stale `src.orchestration` / dead visitor lineage.** `src/visitor/__init__.py` says the
   `src.<pkg>`/`src.orchestration` import paths were "intentionally left, expected to need
   rewiring." The classifier moved to `src/visitor/classifier/`. dispatch.py no longer touches
   the visitor tick, but other files still import `src.orchestration` (dead). Clean up when
   convenient — operator: "no visitor orchestration, that's dead code; router code goes in
   dispatch.py directly."

7. **(BLOCKER for #6) The baseworkflow ACTION BINDINGS still depend on visitor-lineage code —
   refactor needed.** The workflow engine's binding modules are thin wrappers; the real
   subsystem logic lives in `src/visitor/` (the dead lineage), so the engine is NOT
   self-contained and the visitor lineage can't be retired until this is moved out. Operator
   flagged `src/baseworkflow/bindings/github.py` as an example. Full list (flat imports resolved
   via `bindings/__init__` sys.path manipulation — comments say `src/architect/*` &
   `src/orchestration/*` but those now live under `src/visitor/`):
   - `bindings/github.py` → `import purpose`  (`src/visitor/architect/purpose.py`)
   - `bindings/architect.py` → `import decompose, resources, strategy`  (`src/visitor/architect/*`)
   - `bindings/budget.py` → `import approval`  (`src/visitor/architect/approval.py`)
   - `bindings/admin.py` → `import prep, rescaffold, common`  (`src/visitor/architect/{prep,rescaffold}.py`,
     `src/visitor/orchestration/common.py` — the dry-run-aware **gh wrapper** + run-ledger)
   - `bindings/engineer.py` → `from seam import SEAM_SCHEMA_VERSION`  (`src/visitor/orchestration/seam.py`)
   **Refactor:** lift these subsystems (decompose / strategy / purpose / approval / resources /
   prep / rescaffold, and `common`'s gh/dry-run/ledger helpers + `seam`) into the engine or
   `src/baseworkflow/` so the workflow engine owns its logic and `src/visitor/` can be deleted.
   The deterministic-body→`procedure` flip (commit `e0d49a0`) only retargeted the *kinds*; the
   bodies still call into `src/visitor/`.

## Gotchas
- **Shell is `fish`**, not bash — `for f in $files` does NOT word-split a string var. Pass
  multiple files to one command, or use `bash -c`.
- **Runaway fan-out:** a prior live run spawned ~35 `claude` procs (the max bucket = wrong; a
  2-unit decomposition should be ~2 agents). Watch `pgrep -fc claude` during live runs; kill via
  `TaskStop <id>` (scoped) — do NOT `pkill -f claude` (hits the operator's other sessions; the
  permission classifier blocks it).
- **pipeline.env is gitignored** — never commit it. The token may live in conversation (operator
  said so) but not in tracked files.
- The architect bodies are deterministic resolvers TODAY; if the architect should become
  model-driven later, give those actions real prompts and flip back to `inference`. See the new
  `TODO(John)` in `engine/actions/action.py` about adding an action **'lifetime'** concept.
