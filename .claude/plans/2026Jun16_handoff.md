# HANDOFF — dispatch self-contained work-order enhancement (2026-06-16)

> **To resume after `/clear`:** read this file + `.claude/plans/2026Jun16_dispatch-prototype.md`,
> then `git -C /Users/jc/Mendotree/dispatch status`. The working tree holds **uncommitted,
> verified-working** code (see §3). Repo: `/Users/jc/Mendotree/dispatch`, branch `main`,
> remote `ReclaimByDesign/dispatch`.

## 1. What this project is
`dispatch` is a prototype program that turns a GitHub issue queue into a finished,
ENGINEER-ready **work order** on stdout. Pipeline:
`intake → rank (independence/blocking) → classify → approve → issue`.
PLAY.md is the canonical skeleton (3-act drama ADMIN→ARCHITECT→ENGINEER); prompt-master
(issue #8, github.com/nidhinjs/prompt-master) Template H/M is the prompt method.
Run it: `./dispatch --fixture services/architect/fixtures/play-queue.json` (offline, deterministic).
Test gate: `bash scripts/smoke.sh` (must be PASS=… FAIL=0; CI runs this + shellcheck).

## 2. What is DONE and already on `main` (pushed, commit `7b38394`)
- The base prototype: `services/architect/{approval,workorder,dispatch}.py`, root `./dispatch`
  wrapper, `RANKER_OFFLINE` offline dependency-ranking in `services/intake/ranker.py`,
  `services/architect/fixtures/play-queue.json`, smoke `§7.9` (16 asserts).
- A fix to `scripts/lib/common.sh`: `engineer_dispatch` is now dry-run-aware (fixed smoke `§7.3`).
- Merge history gotcha: remote `main` had advanced via **PR #17** (`94c467b`, a squash of the
  same WIP intake work + a 6-line "epic rule" in `ranker.py`'s SYSTEM_PROMPT). We integrated by
  **cherry-picking** our two commits onto it (NOT force-push); `ranker.py` auto-merged and now
  has BOTH the epic rule and the offline ranking. smoke was PASS=56 FAIL=0 at merge.

## 3. What is IN PROGRESS — uncommitted, VERIFIED WORKING, but not yet committed
The operator's new requirement: **the work order must be self-contained AND a staffing plan** —
embed every resource (no further reading/research), define the unit of work, state how many
agents and each agent's specialization. Implemented as:

| File | State | Purpose |
|------|-------|---------|
| `services/architect/resources.py` | **new (untracked)** | discover referenced files in issue text + embed them, the worker contract (CLAUDE.md), the Invoice schema, conventions. Deterministic, offline, path-traversal-guarded, caps logged. |
| `services/architect/decompose.py` | **new (untracked)** | split job into UNITS OF WORK (one cohesive deliverable per unit) + STAFFING. Specialization = `<domain> <function>` (e.g. "developer tooling (shell) engineer", "QA / test automation engineer"). Agent count = #units capped at 15. |
| `services/architect/workorder.py` | **modified** | rewritten to render 3 new sections (UNITS OF WORK, STAFFING, EMBEDDED RESOURCES); PHASE 1 now says "it's all embedded above", footer no longer says "read CLAUDE.md". `render()` gained `resources=`/`plan=` kwargs. |
| `services/architect/dispatch.py` | **modified** | imports `resources`+`decompose`, computes `repo_root`, calls `gather()`+`plan()`, passes to `render()`; `--json` envelope now also carries `units`+`staffing`. |

**Verified just now:** `./dispatch --fixture …` exits 0, no traceback; renders all 3 new sections;
staffs 3 agents for issue #2 (Unit A `scripts/dispatch.sh`, Unit B `scripts/gh-intake.sh` → both
"developer tooling (shell) engineer"; Unit C verify → "QA / test automation engineer"); embeds the
real `load_queued_issues` source + CLAUDE.md contract + invoice schema; work order is ~25KB (was
~4.3KB). `bash scripts/smoke.sh` still PASS=56 FAIL=0 (the new sections are additive; old §7.9
asserts still pass).

## 4. Spec that drove the enhancement (operator decisions, locked)
1. Embed all resources — engineering needs zero further reading/research.
2. Define what a clear unit of work comprises.
3. State how many agents to employ.
4. State each agent's specialization.
- **Specialization taxonomy:** generic **function** (engineer, writer, designer, analyst…)
  qualified by a **domain** (e.g. "developer tooling", "data-pipeline", "QA / test automation").
  NOT the biblical agent roster, NOT bare generic.
- **Unit of work:** one cohesive, independently-verifiable deliverable owned end-to-end by one
  specialist. **Agent count = number of units, capped at the swarm max (15).**

## 5. NEXT STEPS (do these, in order)
1. **Update smoke `§7.9`** (`scripts/smoke.sh`) to assert the NEW capabilities — it currently only
   checks the old markers. Add asserts that the work order contains: `UNITS OF WORK`, `STAFFING`,
   `Employ 3 agent`, a `<domain> <function>` specialist label, embedded source (`load_queued_issues`),
   `EMBEDDED RESOURCES`, the worker contract — and that PHASE 1 no longer tells the engineer to go
   read files. Keep it offline + deterministic (two runs byte-identical).
2. **Re-run** `bash scripts/smoke.sh` → expect FAIL=0. Also `python3 -c "import ast; ..."` syntax-check
   the two new modules if not already covered.
3. **Commit** (signed — see §6) the enhancement: `services/architect/resources.py`,
   `decompose.py`, `workorder.py`, `dispatch.py`, `scripts/smoke.sh`. One commit, e.g.
   `feat(architect): self-contained work orders — embedded resources + units + staffing`.
   **Stage ONLY those files** (see §6 exclusions).
4. **Merge to `main`** the same way as before if the operator confirms (operator said "just merge"
   earlier; confirm it still applies for this commit). `main` should fast-forward cleanly now;
   `git push origin main`. Watch for remote advancing again (re-fetch; cherry-pick, never force).
5. Decide on `test_results.txt` (untracked, repo root) — it's a STALE copy of the OLD (pre-embed)
   work order the operator pasted. Delete it or regenerate; don't commit it.

## 6. Conventions & gotchas (load-bearing)
- **Commit signing:** bare `git commit` HANGS (gpg pinentry, no TTY). Use
  `git -c gpg.program=gpg-loopback commit -m "..."`. A pre-tool hook enforces this.
- **Do NOT stage** (pre-existing / not ours): `CLAUDE.md`, `README.md` (uncommitted M from before
  the session), `.claude/settings.json`, and the untracked `.claude/` + `.claude-flow/` tooling dirs.
  Stage explicit paths, never `git add -A`.
- **Determinism is a hard requirement** (smoke asserts byte-identical across 2 runs): no wall-clock
  in work-order output. Session id is `dispatch-issue-<n>` (no timestamp) on purpose.
- **Security:** issue text is untrusted DATA (embedded, never executed); classifier is a quarantine
  reader (no exec — smoke §8 asserts this); `resources.py` only reads regular files inside repo_root.
- **Codebase pattern:** deterministic + offline by default, LLM is an opt-in upgrade
  (`--llm` on workorder, `--rank-llm`, live `--repo`/`--project`). Keep new work the same shape.
- **Scope:** this is a PROTOTYPE — keep additions minimal; don't over-build.

## 7. Open threads (pre-existing, not blockers)
- README/CLAUDE.md on `main` still reference removed systems (HANDOFF-pipeline-v0.md, OpenClaw,
  Ruflo) — PLAY.md's "Option E", not yet done.
- shellcheck SC2034 warning in `scripts/architect-intake.sh` (`route_used` unused) — pre-existing.
- `--llm` live path is wired but only exercised via offline fallback; never run against a real model.
- Live cross-repo embedding: `resources.py` reads from `repo_root` (= cwd = the dispatch repo). For
  a live `--repo` target whose files aren't checked out locally, discovery finds nothing — fine for
  the offline demo; a real cross-repo run needs a local checkout.

## 8. Pointers
- Design plan: `.claude/plans/2026Jun16_dispatch-prototype.md`
- Session journal: `/Users/jc/Garden/admin/wiki/journals/2026Jun16_pipeline-pickup-1-5.md`
- Project memory: `~/.claude/projects/-Users-jc-Mendotree-dispatch/memory/dispatch-program-architecture.md`
- Skeleton/spec: `PLAY.md`; subsystem checklist: issue #7; prompt method: issue #8.
