# METHODS.md

Project-specific conventions established during spec phase. Fill in each section by asking the PROMPTER. Leave fields blank if not yet decided.

---

## Development Status

**`Development Status: 1.0`** (released — **production mode**).

The repo has left solo dev, so the dev-mode push pre-authorization has **lapsed**:
operator and pipeline sessions no longer push to `main`. The PR / branch-protection /
review flow is in force. Branch discipline (see **Branch strategy** below):

- **All work lands via PR into `beta`** — the integration branch.
- **`main` receives releases only.** Only a release merge moves `beta` → `main`; no
  feature, fix, or pipeline branch targets `main`. Direct pushes to `main` are blocked
  by branch protection.

This line stays the authoritative signal: were `Development Status` ever set back to
`development`, the [`CLAUDE.md`](./CLAUDE.md) dev-mode push-to-`main` pre-authorization
would re-arm.

---

## Running a live engine tick test

The pipeline is pure Python; the unit-of-work lifecycle is the YAML **BaseWorkflow**
(`app/workflows/baseworkflow.yml`, engine in `foundation/workflow/`), driven directly
by `./dispatch`. Entry point:

- `./dispatch [FLAGS]` — canonical entry; runs the BaseWorkflow engine directly,
  with the Engineer running in-process via the Claude Agent SDK.

Tick stages: `intake → workorder → prep → engineer → intake-invoice → closure`. The
architect selects which issues to work during `intake` — epics are flagged
`wontdo_parent_issue` (with child links), leaves are ranked and claimed up to
`PIPELINE_CONCURRENCY`.

### Safe-to-live progression

Each rung is more "live" than the last. Use a **throwaway target repo** (e.g.
`ReclaimByDesign/dispatch-testrepo-a`) for anything past step 1.

1. **Validate the workflow YAML** — offline, instant:
   ```bash
   python3 -m foundation.workflow app/workflows/baseworkflow.yml \
     --registry baseworkflow.bindings:build_registry
   ```
   Expect `[PASS] … (structure + data-flow + tokens; 3 phases)`.

2. **Deterministic e2e ("roundabout")** — the full spec→work→build lifecycle over real
   issue data with a MockActionFactory: real subsystem logic, **no model, no network
   mutations, no PRs**:
   ```bash
   python3 scripts/demo/roundabout-baseworkflow.py OWNER/REPO ISSUE [ISSUE...]
   ```
   Prints a per-issue statechart trace, budget meters, and deliverables; ends with
   `roundabout summary: N/N green`.

3. **Dry-run tick** — the real orchestration tick with the architect selecting issues,
   but **no GitHub mutations** (the default; `gh` writes print as `DRY-RUN: …`). Halt
   early with `--until` to inspect a stage's artifact:
   ```bash
   DISPATCH_ARTIFACTS_DIR=.dispatch/test-artifacts \
     ./dispatch -r OWNER/REPO --until workorder
   ```
   Artifacts (`workorder.txt`, `job-request.json`, `invoice.json`) land under
   `${DISPATCH_ARTIFACTS_DIR}/<tick-id>/`.

4. **Live tick** — opts in to mutate GitHub (claim labels, open the PR) and run the real
   Engineer:
   ```bash
   ./dispatch -r OWNER/REPO --live              # one ready issue
   ./dispatch -b -r OWNER/REPO --live           # provision pipeline labels first
   PIPELINE_CONCURRENCY=2 ./dispatch -r OWNER/REPO --live
   ```

### Flags & env

| Flag / env | Effect |
|------------|--------|
| `-r/--repo OWNER/REPO` | target repo (sets `PIPELINE_REPO`) |
| `-l/--live` | `PIPELINE_DRY_RUN=0` — mutate GitHub (default: dry-run) |
| `-b/--bootstrap` | provision pipeline labels on the repo first |
| `-e/--engineer BIN` | Engineer binary (default: SDK engineer; `scripts/mock-engineer.sh` for offline) |
| `-f/--fixture FILE` | issue-list JSON for fully offline intake |
| `-u/--until STAGE` | halt after STAGE (dumps artifacts) |
| `--from STAGE -a/--artifact FILE` | replay from a captured artifact |
| `PIPELINE_CONCURRENCY=N` | issues claimed per tick (default 1) |
| `DISPATCH_ARTIFACTS_DIR=DIR` | per-stage artifact capture |

> **Python 3.14 caveat:** the default SDK Engineer can hang under 3.14; for a live
> engineer run use the `claude -p` CLI backend or a Python < 3.14. Validation, the
> deterministic roundabout, and dry-run ticks are unaffected (verified on 3.14).

---

## Goal

> What is the goal of this session?

---

## Testing

> What is the testing philosophy for this project?
> Are tests required for all new code, or only when explicitly requested?

- **Philosophy**: <!-- unit-first / integration-first / interface-stable-first -->
- **Required for all new code**: <!-- yes / no / ask -->

---

## Git & PRs

> What commit message convention should be used?
> Are PRs preferred over direct pushes to main?

- **Commit convention**: conventional commits (signed)
- **Branch strategy**: production mode — all work lands via PR into `beta` (the integration branch); `main` receives **releases only** (`beta` → `main` on release). Direct pushes to `main` are blocked by branch protection. (Dev-mode direct-push-to-`main` applies only while `Development Status: development`; see **Development Status** above.)

---

## Stack

> What language and framework does this project use?
> Are there platform-native solutions to prefer?

- **Language**:
- **Framework**:
- **Platform-native preferences**:

---

## Evaluation

> How should the AGENT verify that work is correct?

- **Command(s)**: <!-- e.g. pytest, npm test, python demo.py -->

---

## Documentation System

> Where do out-of-scope / deferred requests get logged? (See `CLAUDE.md` → Scope Discipline.)
> If no formal system, the AGENT files a GitHub issue via `/document` (private repo)
> or falls back to a local `docs/requests/<slug>.md` file (public repo / no GitHub).

- **Destination**: <!-- e.g. GitHub issues (via /document), Linear project, Notion page, docs/requests/<slug>.md -->
- **Promotion path**: <!-- how do deferred entries become tracked work? -->

---

## API Documentation (for developers and agents)

The live packages — `foundation`, `baseworkflow`, `websitewf`, `agents` — carry an
**auto-generated API reference** built by the [`.github/api_docs/`](./.github/api_docs/)
package (see [`.github/api_docs/README.md`](./.github/api_docs/README.md)). It is derived **statically from
source docstrings with `ast`** — it never imports the documented code, so it is
deterministic, version-independent, and immune to the Python 3.14 SDK-import hang.
`visitor/` (dead graveyard) and the empty `engine/`/`src/` husks are excluded.

Two surfaces, one core:

- **Humans** — a static HTML site, published to **GitHub Pages** by
  `.github/workflows/docs.yml` on every push to `beta` (the integration branch,
  so the live site can be refined pre-release) and `main` (republished on
  release). Live at <https://reclaimbydesign.github.io/dispatch/>. *Visibility
  note:* on the Team plan this Pages site is **public** even though the repo is
  private (private/access-controlled Pages is Enterprise-only) — it exposes the
  internal API surface to anyone with the URL.
- **Agents** — an MCP server `dispatch-apidocs` (registered in `.mcp.json`):
  `list_index`, `lookup` (pydoc for a symbol), `find_usage`, `get_source`. It
  needs the `mcp` package: `python3 -m pip install -r requirements-docs.txt`.

### How an AGENT looks up API information (use context mode)

Prefer these over reading source files into context — they return only the
answer, not the file. Two equivalent paths:

**A. The MCP tools** (when the `dispatch-apidocs` server is connected). Call them
directly — each returns a compact, token-light result:

- `list_index("foundation.workflow")` — browse the index, scoped to a prefix.
- `lookup("baseworkflow.BaseWorkflow")` or `lookup("chat")` — signature + full
  docstring + location (a bare name that is ambiguous returns a candidate list).
- `find_usage("make_backend")` — every reference across the source, def sites flagged.
- `get_source("foundation.models.chat")` — the exact source of one definition.

**B. The CLI through context-mode** (always available, no server needed). Run the
`api_docs` CLI inside `ctx_execute` so its output is **indexed in the sandbox** and
only the summary enters your context — the Think-in-Code path. The package lives
under `.github/`, so prefix `PYTHONPATH=.github`:

```
ctx_execute(language: "shell", code: "cd <repo> && PYTHONPATH=.github python3 -m api_docs index foundation")
ctx_execute(language: "shell", code: "cd <repo> && PYTHONPATH=.github python3 -m api_docs lookup BaseWorkflow")
ctx_execute(language: "shell", code: "cd <repo> && PYTHONPATH=.github python3 -m api_docs usage make_backend")
```

For repeated questions about the same area, index the generated site or the
source once and then query it cheaply:

```
ctx_execute(language: "shell", code: "cd <repo> && PYTHONPATH=.github python3 -m api_docs build --out site")
ctx_index(path: "<repo>/site")          # or ctx_index the package dirs directly
ctx_search(queries: ["how does BaseWorkflow seed the input shelf",
                     "where is make_backend called"])
```

The rule: **ask the index, don't grep the tree.** `lookup`/`find_usage` (or a
`ctx_search` over the indexed site) answer "what is this / where is it used" for
O(answer) tokens instead of O(files-read).

---

## Notes

> Anything else the PROMPTER wants the AGENT to know.
