# OPEN-QUESTIONS.md

`ultracode` writes; a human answers. Each item records an ambiguity in
`HANDOFF-pipeline-v0.md`, the **decision taken so the prototype is runnable**,
and what (if anything) a human must confirm or supply. Nothing here guesses a
secret, URL, or account-specific value — those are left blank in
`pipeline.env.example` and listed in §C below.

Status legend: **[decided]** built this way, reversible · **[needs-human]**
blocks going live · **[verify]** depends on a moving external surface.

---

## A. Architecture / interpretation decisions

### A1. `pipeline/` in §4 == the repository root  **[decided]**
The §4 tree is rooted at `pipeline/`, but `.github/workflows/` only runs when
it sits at the repo root, `.claude/workflows/*.js` only register from the repo
root (or `~/.claude`), and `CLAUDE.md` only auto-loads from the repo root.
Nesting them under a literal `pipeline/` subdirectory would make CI, the
slash-commands, and project memory all non-functional.
**Decision:** treat the empty `Mendo-AI-Consulting/dispatch` repo root as the
`pipeline/` of the diagram. Everything is scaffolded at the repo root.
**If you disagree** (e.g. you want this vendored under a `pipeline/`
subdirectory of a larger monorepo), you must add a sync/symlink step so the
two framework dirs reach the repo root.

### A2. `claude -p ... --args '<json>'` — the `--args` flag does not exist  **[decided]**
The handoff (§3, §5.1, §5.8) invokes workflows as
`claude -p "/implement-task" --args '{...}'`. Current Claude Code (verified
against `code.claude.com/docs`) has **no `--args` flag**; the documented
headless form embeds arguments in the prompt and the workflow reads a global
named `args`. §10 already flags these surfaces as moving.
**Decision:** `scripts/lib/common.sh:claude_invoke` is mode-switched by
`CLAUDE_ARGS_MODE` (default `prompt`): it runs
`claude -p "Run /<wf> with args <json>"`. Set `CLAUDE_ARGS_MODE=flag` to use
`--args` if/when that flag ships. The workflow `.js` files read the `args`
global accordingly.
**Confirmed 2026-06-13:** operator confirmed `CLAUDE_ARGS_MODE=prompt` is
correct for the deployed version. No further verification needed.

### A3. Workflow `.js` runtime API (subagent spawning) is undocumented  **[decided]**
The public docs confirm `.claude/workflows/*.js` auto-registers, args arrive
via the `args` global, and scripts must be deterministic — but they do **not**
specify the primitive used to spawn subagents from inside a workflow.
**Decision:** each workflow feature-detects `globalThis.spawnSubagent` /
`globalThis.agent`; absent either, it logs the planned subagent call (so the
file is still inspectable/validatable). All three pass `node --check`.
**Confirmed 2026-06-13:** operator confirmed `globalThis.agent` is the correct
runtime primitive. The feature-detect order (`spawnSubagent` → `agent`) already
tries `agent` second; swap to prefer `agent` first when updating the workflows.

### A4. Fix-ladder attempt counter semantics  **[decided]**
§5.5 says "read `fix-attempt-N` label … increment label" and §7.6 says a
"fix-attempt-2 fixture selects `gen-default`". To keep the label vocabulary
(only `fix-attempt-1..3`) consistent **and** reach the `>3` cap, the attempt
*about to run* = `(max fix-attempt-N label) + 1`, and `fix-dispatch.sh` also
honors an explicit `.attempt` from the webhook payload (the two agree in the
fixtures). `tier_for_attempt`: 1→`gen-local`, 2→`gen-default`,
3→`gen-frontier`, >3→`needs-human` (escalate, no `/fix-ci`).

### A5. `closure.sh` posts the summary comment in the success phase  **[decided]**
§5.8 puts the issue summary comment on the *merged* event, but §7.8 expects
the success-payload dry-run to print "the summary comment". **Decision:** the
success phase posts a **closure summary on the PR** (and arms auto-merge); the
separate *merged* event posts the final **issue** summary and flips to `done`.
Both behaviours exist; the acceptance check sees the former.

### A6. Classifier offline seam for deterministic, network-free testing  **[decided]**
§7.2 requires deterministic classification across two runs with no network,
but §5.3's v0 is the HF Inference API. **Decision:** `classify.py` runs the HF
zero-shot path by default and, per §5.3, exits non-zero on API failure (no
silent fallback). An **explicit** `CLASSIFIER_OFFLINE=1` selects a
deterministic, rule-based classifier used by `smoke.sh` and available as an
opt-in degraded mode. It never masks a real API failure.

### A7. CI for this repo == `smoke.sh`  **[decided]**
This repo's "product" is the pipeline itself, so the deterministic gate
(`.github/workflows/ci.yml`) runs `shellcheck` + `scripts/smoke.sh` (offline,
dry-run, no secrets). When you point the pipeline at a *product* repo, that
repo supplies its own `ci.yml` (tests/typecheck/build).

### A8. `claude-code-action@v1` review is gated off by default  **[decided]**
`review.yml` runs only when repo **variable** `ENABLE_CLAUDE_REVIEW=true`, so
PRs stay green before the key/secret exists. Exact action inputs are a moving
surface — verify against the action README before enabling.
**Confirmed 2026-06-13:** operator confirmed `ENABLE_CLAUDE_REVIEW=true` should
be set now. Action: set the repo variable and wire `ANTHROPIC_API_KEY` as a
GitHub Actions secret. Verify action inputs against the action README before
the first PR review fires.

### A9. Cross-family critic in v0 = local/open-weight  **[decided]**
§5.2 wants the `critic` "cross-family vs the generator". `update-docs.js`
writes docs on `gen-local` (open-weight) and verifies on the opposite family
via `criticRoute()` → `gen-default` (Anthropic). For an *Anthropic* generator
the opposite family is open-weight: in v0 that resolves to `gen-local` (MLX),
since the hosted open-weight critics (`gen-deepseek`/`gen-kimi`) are stubs.
**Upgrade:** activate a hosted stub (§9) for a stronger hosted cross-family
critic. The `critic` LiteLLM group remains declared for direct use.

---

## B. Things a human must verify against moving external surfaces  **[verify]**

- **B1. OpenClaw CLI flags.** `bootstrap-openclaw.sh` uses plausible
  `openclaw cron add` / `openclaw webhook add` / `openclaw announce` flags.
  Verify names against your Gateway version (command vs isolated job type,
  webhook auth flag, per-job model flag, announce channel flag).
- **B2. Webhook routing.** The handoff routes one `/webhook/github` to an
  isolated session that runs `fix-dispatch.sh` (failure) **or** `closure.sh`
  (success). `bootstrap-openclaw.sh` expresses this as
  `--handler … --on-success-handler …`; confirm the Gateway's actual
  success/failure routing mechanism (it may instead branch inside one handler
  on the payload `.conclusion`).
- **B3. HF zero-shot model.** Default `HF_INFERENCE_MODEL=facebook/bart-large-mnli`
  on the serverless Inference API. Confirm it is still served (or pick a
  current zero-shot NLI model) and that your `HF_TOKEN` has access.
- **B4. LiteLLM model ids.** `gen-frontier` reads `ANTHROPIC_FRONTIER_MODEL`
  (an Opus-tier id you must fill); `gen-default`/`haiku-tier` ship with
  current Sonnet/Haiku ids — confirm they match what you intend to run.
  **Confirmed 2026-06-13:** `ANTHROPIC_FRONTIER_MODEL=anthropic/claude-opus-4-8`
  set in `pipeline.env`. Sonnet/Haiku ids already current.
- **B5. `litellm --health`/`/v1/models`.** smoke does a structural YAML check
  offline; run the real proxy once and confirm `/v1/models` lists every
  non-stub group.

---

## C. Account-specific values to supply (blank in `pipeline.env.example`)  **[needs-human]**

Copy `pipeline.env.example` → `pipeline.env` (gitignored) and fill:

- **GitHub fine-grained PATs (§8, least privilege, none may merge):**
  `TRIAGE_GITHUB_TOKEN` (issues:read), `WORKER_GITHUB_TOKEN`
  (contents+PR write, non-protected branches), `CLOSURE_GITHUB_TOKEN`
  (PR write / enable auto-merge), optional `GITHUB_TOKEN` fallback.
- **Model keys:** `ANTHROPIC_API_KEY`, `MLX_QWEN_*` (local endpoint),
  `LITELLM_MASTER_KEY`; stubs `DEEPSEEK_API_KEY` / `MOONSHOT_API_KEY` stay
  blank in v0.
- **Classifier:** `HF_TOKEN`.
- **OpenClaw Gateway:** `OPENCLAW_OPERATOR_CHANNEL` (paired channel id),
  `OPENCLAW_WEBHOOK_URL`, `OPENCLAW_WEBHOOK_BEARER_TOKEN`,
  `OPENCLAW_BIND_ADDR` (keep loopback/tailnet).
- **GitHub Actions secrets** (mirror, for `review.yml` / `notify-openclaw.yml`):
  `ANTHROPIC_API_KEY`, `OPENCLAW_WEBHOOK_URL`, `OPENCLAW_WEBHOOK_BEARER_TOKEN`;
  variable `ENABLE_CLAUDE_REVIEW=true` to turn on the advisory review.

---

## D. Operator setup steps (one-time, before flipping dry-run off)  **[needs-human]**

1. `bash scripts/bootstrap-labels.sh` (with `gh` authed; `PIPELINE_DRY_RUN=0`).
2. Configure **branch protection** on `main`: require the `CI` check **and**
   one approving review; disallow direct pushes and merges by the worker/
   closure tokens. This is what makes "no actor can merge" true and what
   `--auto` merge waits on.
3. `bash scripts/bootstrap-openclaw.sh` on the Gateway host; verify
   `openclaw cron list` shows `pipeline-dispatch` + `pipeline-digest`.
4. Confirm Gateway bind is loopback/tailnet, channel pairing is on, and the
   pipeline agent's exec allowlist is `gh,git,python,claude` only (§8).
5. Start/point the LiteLLM proxy at `config/litellm.pipeline.yaml`; confirm
   `/v1/models`.
6. Leave `PIPELINE_DRY_RUN=1` until 1–5 are verified; then flip to `0`.

---

## E.0 pipeline.env bugs caught during live run (fixed 2026-06-13)

- `ANTHROPIC_FRONTIER_MODEL=anthropic/<opus-tier-model-id>` — unquoted angle
  brackets caused a bash parse error on `source`. Fixed: real model id filled.
- `OPENCLAW_DISPATCH_SCHEDULE=*/10 * * * *` and `OPENCLAW_DIGEST_SCHEDULE=0 8 * * *`
  — unquoted cron expressions were glob-expanded by `set -a; source`. Fixed:
  values now quoted in `pipeline.env` (update `pipeline.env.example` to match).
- `scripts/lib/common.sh` sourcing with `set -a` silently clobbered
  caller-exported env vars (e.g. `PIPELINE_DRY_RUN=0` on the command line).
  Fixed: caller values for `PIPELINE_DRY_RUN`, `PIPELINE_CONCURRENCY`, and
  `PIPELINE_REPO` are saved before the source and restored after.

## E. Deliberately deferred (per §9, not open questions)

Parallel claims (`PIPELINE_CONCURRENCY>1`), local classifier training
(`classify_local_stub.py`), DeepSeek/Kimi activation (uncomment in litellm +
key), zero-touch merge (drop the human-approval requirement), multi-repo, and
cost dashboards. All are config flips or upgrades inside existing seams.
