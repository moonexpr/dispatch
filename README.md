# dispatch

<p align="center">
  <img src="docs/assets/dispatch-logo.png" alt="dispatch logo — a control tower broadcasting signal" width="180">
</p>

**dispatch** is your engineering control tower. It watches a GitHub Issues queue,
picks up one ready job at a time, runs it through a workflow engine across a
`spec → work → build` cycle, opens a pull request, and either arms it for merge or
escalates back to you — all unattended, a few times a day. You write the issues
and approve the results; dispatch coordinates the work in between.

It does three things and nothing more:

1. **Intake** — fetch and classify queued issues, claim one.
2. **Issue the job** — the Architect phase composes a structured work order; the
   work phase implements it in an isolated worktree.
3. **Approve or escalate** — on success, arm auto-merge (awaiting your approval);
   on failure, advance the fix ladder; on ambiguity, comment and escalate.

> 📖 **Full guide: the [dispatch wiki](https://github.com/ReclaimByDesign/dispatch/wiki).**
> Start with [Getting started](https://github.com/ReclaimByDesign/dispatch/wiki/Getting-Started)
> and [Strengths and limitations](https://github.com/ReclaimByDesign/dispatch/wiki/Strengths-and-Limitations).

## Interact with dispatch via Claude skills (recommended)

dispatch is **L3-class automation**: it shines on small, clear, checkable units of
work and struggles with vague or sprawling ones. The standard, encouraged way to
drive it is the composable **`/dispatch` Claude Code skill family**, which
interviews you (`AskUserQuestion`) and shapes work toward what the pipeline does
well — *before* anything reaches the queue.

| Skill | What it does |
|-------|--------------|
| `/dispatch` | Front door: takes any high-level ask, interviews until it is specified, then routes it. |
| `/dispatch:scope` | One rough idea → one well-formed issue (goal, acceptance criteria, scope, verification). |
| `/dispatch:decompose` | A large ask → small, shippable leaf issues filed as a Draft epic. |
| `/dispatch:tick` | Preview a tick in dry-run, then a deliberate go-live. |
| `/dispatch:review` | Vet a pipeline pull request against its acceptance criteria before you approve. |
| `/dispatch:oneshot` | Interview a task, then feed it straight into the engine — bypassing GitHub issue intake. |

The skills ship under [`.claude/skills/`](./.claude/skills/) and work automatically
when this repo is your Claude Code workspace. To use them in **any** project, run
the cross-platform installer (Windows · macOS · Linux):

```bash
python install_claude_skills.py            # copy the skills into ~/.claude/skills/
python install_claude_skills.py --list     # preview what would be installed
python install_claude_skills.py --force     # overwrite an existing copy
```

Then just say what you want — *"have dispatch build a Vercel landing page"* — and
`/dispatch` takes it from there. Full guide:
[Interacting with dispatch](https://github.com/ReclaimByDesign/dispatch/wiki/Interacting-with-Dispatch).

## Installation

```bash
git clone git@github.com:ReclaimByDesign/dispatch.git
cd dispatch

# 1. Secrets and config (gitignored). Set CLAUDE_CODE_OAUTH_TOKEN, PIPELINE_REPO, tokens.
cp .env.example .env

# 2. Verify the workflow engine.
python3 scripts/smoke.py

# 3. Dry-run a tick — prints what it would do, mutates nothing.
./dispatch -r owner/repo

# 4. When ready, provision labels and go live.
bash scripts/bootstrap-labels.sh        # requires gh authed + a live run
./dispatch -r owner/repo --live
```

Dry-run is the default; `--live` is the only switch that lets dispatch mutate
GitHub. Configure branch protection on `main` (CI + one human review) before going
live — auto-merge waits on it, and no token may merge directly. See
[Configuration](https://github.com/ReclaimByDesign/dispatch/wiki/Configuration) and
[Unattended Linux server installation](https://github.com/ReclaimByDesign/dispatch/wiki/Unattended-Linux-Server-Installation)
(systemd) for the full setup.

## Use cases

dispatch earns its keep on work that is bounded and checkable:

- **Backlog burndown** — a queue of small, well-specified bugs and features, worked
  a few at a time on a schedule.
- **Repetitive, well-patterned changes** — a new route, a config key, a localized
  fix, scaffolding that resembles many prior examples.
- **Decomposed projects** — a larger build (a website, a service) broken via
  `/dispatch:decompose` into leaf issues the pipeline takes one at a time.
- **Test-backed work** — anything whose success a test or command can confirm, so
  mistakes are caught before they reach you.

## Guidelines

dispatch is a capable but fallible L3 collaborator — treat it like one:

- **Write small, clear, verifiable issues.** Use the
  [issue templates](./.github/ISSUE_TEMPLATE) (or `/dispatch:scope`); every task
  needs a goal, acceptance criteria, scope, and a way to verify "done".
- **Decompose big asks.** Hand the pipeline leaves, not epics — do the sprawling or
  novel thinking interactively with Claude first.
- **Stay in the loop.** Escalations arrive as GitHub comments and merges wait on
  your approval. It is scheduled, not fire-and-forget.
- **Review before you merge.** Every pull request is a draft to read — use
  `/dispatch:review`. Merging is never delegated.
- **Label by priority.** Carry one status rung — `Unscheduled` ▸ `Draft` ▸
  `Candidate` ▸ `Release` — plus the `Blocker` overlay to jump the queue; dispatch
  works the highest-priority ready issue first.

## Learn more

The wiki is the complete reference:

- [Getting started](https://github.com/ReclaimByDesign/dispatch/wiki/Getting-Started)
- [Interacting with dispatch (Claude skills)](https://github.com/ReclaimByDesign/dispatch/wiki/Interacting-with-Dispatch)
- [Configuration](https://github.com/ReclaimByDesign/dispatch/wiki/Configuration)
- [Strengths and limitations](https://github.com/ReclaimByDesign/dispatch/wiki/Strengths-and-Limitations)
- [Development and contribution](https://github.com/ReclaimByDesign/dispatch/wiki/Development-and-Contribution)
- [Architectural patterns](https://github.com/ReclaimByDesign/dispatch/wiki/Architectural-Patterns) · [Understanding HFSMs and statecharts](https://github.com/ReclaimByDesign/dispatch/wiki/Understanding-HFSMs-and-Statecharts)
- [Creating your own workflow](https://github.com/ReclaimByDesign/dispatch/wiki/Creating-Your-Own-Workflow)
- [Unattended Linux server installation](https://github.com/ReclaimByDesign/dispatch/wiki/Unattended-Linux-Server-Installation)

In-repo references: [`CLAUDE.md`](./CLAUDE.md) (worker contract) ·
[`docs/adr/`](./docs/adr) (architecture decisions) ·
[`schemas/`](./schemas) (Job Request / Invoice).
