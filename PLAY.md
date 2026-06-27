# THE DISPATCH
### An Agentic Drama in Three Acts

---

## DRAMATIS PERSONAE

**ADMIN** — The operator's voice. Reads the queue, holds the purse, grants authority.
Speaks in structured briefs and terse approvals. Never executes work directly.

**ARCHITECT** — The planning intelligence. Classifies, proposes, budgets, drafts.
Never executes. Submits work plans only to ENGINEER, and only after ADMIN's authorization.

**ENGINEER** — The execution engine. Receives one work plan per session.
Executes faithfully. Returns an Invoice. Does not negotiate.

---

*The scene is a GitHub repository. Three sessions. One pipeline.*

---

## ACT I: INTAKE

### Scene 1 — The Queue

*[ADMIN opens a session. The repository is ReclaimByDesign/dispatch, branch main,
last commit f6c7e22. ADMIN calls `gh issue list` and reads what returns.]*

**ADMIN:**
The queue.

Three open issues, no queued label on any of them yet. Untracked work
in the tree: `scripts/gh-intake.sh`, `src/intake/`, `.claude/commands/`,
`.claude/skills/`. I am reading all of it. Five jobs present themselves.

*[ADMIN assembles the intake brief. Reads slowly. One job at a time.]*

---

**JOB OPTION A — Issue #7**
*Title:* Assess subsystems needed for prototype
*Body:* data sourcing · work approval · job issuance
*Scope:* xs · Confidence: 0.72
*Classification:* discovery

The strip commit we just landed answers the architecture question
in broad strokes. What remains is mapping each named subsystem to
its current implementation and identifying where the gaps are.
Low risk. High clarity value. But derivative — we learn nothing
we couldn't learn by looking at the code.

---

**JOB OPTION B — Issue #3**
*Title:* Going-live checklist: secrets, branch protection, bootstrap,
then flip PIPELINE\_DRY\_RUN=0
*Body:* Secrets, PATs, branch protection, bootstrap scripts, verify the proxy
*Scope:* s · Confidence: 0.85
*Classification:* ops / compliance

The checklist still lists OpenClaw, HF\_TOKEN, LiteLLM, and
`bootstrap-openclaw.sh` — all removed in the strip. Anyone who
follows it now will configure things that no longer exist and miss
what does. The checklist must be updated before anyone attempts
go-live. Medium urgency. No code risk.

---

**JOB OPTION C — Issue #2**
*Title:* Set `main` as the repository default branch
*Body:* Stale default branch from first push. Prerequisite for branch-protection.
*Scope:* xs · Confidence: 0.99
*Classification:* ops

One click in GitHub Settings. This is a human task —
not autonomous pipeline work. I am flagging it for ADMIN
to handle after this session closes.

---

**JOB OPTION D — Untracked: intake subsystem**
*Title:* Wire `gh-intake.sh` and `src/intake/` into the pipeline
*Body:* Files exist on disk, uncommitted, incomplete. The intake layer converts
raw GitHub project items into queued issues dispatch can act on.
*Scope:* m · Confidence: 0.81
*Classification:* feature

This is the missing first link. Without it, the queue must be
seeded by hand every cycle. `dispatch.sh` already has three paths
in `load_queued_issues()`: fixture file, live `gh issue list`, and
a gap where a proper intake binary should be. The files are there.
They need to be read, trimmed to the intake boundary, committed,
and wired in. Medium scope. Medium risk — intake state unknown
until ENGINEER reads it.

---

**JOB OPTION E — Untracked: worker contract**
*Title:* Update CLAUDE.md worker contract for the stripped architecture
*Body:* CLAUDE.md still references HANDOFF-pipeline-v0.md (deleted), Ruflo,
OpenClaw, /implement-task, /fix-ci, /update-docs. The contract governs how
ENGINEER behaves. A stale contract produces stale behavior.
*Scope:* s · Confidence: 0.90
*Classification:* docs

Low risk. High leverage. Every ENGINEER session opens with
CLAUDE.md. If it describes a system that no longer exists,
ENGINEER will make decisions against a ghost architecture.
Correcting it is not optional — it is a correctness fix.

---

**ADMIN:**
ARCHITECT — five options. Read them.

Return a priority ranking with rationale and a budget proposal
for the top selection. I am holding eighty thousand tokens
for this planning session.

---

## ACT II: NEGOTIATION

### Scene 1 — The Proposal

*[ARCHITECT reads the intake brief. Reasons across all five options.
A pause. Then speaks.]*

**ARCHITECT:**
Received.

Option C first: one GitHub Settings click.
That is not pipeline work. I will not rank it.
Flag it for ADMIN to close manually.

Option B — the going-live checklist — matters,
but it is downstream of the intake subsystem.
You cannot verify go-live readiness until the pipeline
can pull its own queue. Running the checklist now
would validate a system that cannot yet feed itself.
I am deferring it.

Option A — subsystem assessment — is mostly answered
by the strip commit and by whatever we build in Option D.
If we implement the intake layer, the assessment writes itself.
I would fold A's deliverable into D's PR documentation.
Deferring as a standalone job.

That leaves D and E.

Option E is the smallest correctness risk on the board —
one stale document, zero code, high confidence.
It should ship with whatever else ships.

Option D is the structural gap.
The intake layer is what makes the pipeline autonomous.
Without it, ADMIN seeds the queue by hand.
Every cycle. Every time.
The files exist. The seam in dispatch.sh exists.
The work is connective tissue, not invention.

My recommendation:

**Primary: Option D.** Wire the intake subsystem.
**Tail: Option E.** Update the worker contract. Same PR.

---

Budget for Option D, carrying Option E:

*Read phase:* eight thousand tokens.
Inspect `gh-intake.sh`, `src/intake/`, the `load_queued_issues()`
function in `dispatch.sh`, the queued-issues fixture schema,
and the full current CLAUDE.md.

*Plan phase:* four thousand tokens.
This exchange.

*Generate phase:* thirty thousand tokens.
Implementation, fixture updates, CLAUDE.md rewrite.

*Verify phase:* six thousand tokens.
`smoke.sh` must pass with zero failures.

*PR phase:* four thousand tokens.
Branch, commit, PR body with done-criteria checklist.

Total: **fifty-two thousand tokens.**
That leaves twenty-eight thousand in reserve
for ENGINEER's contingencies.
I recommend holding the reserve, not pre-spending it.

One risk to name: the state of `src/intake/` is unknown
until ENGINEER reads it. If it is further along than the tree suggests,
scope shrinks. If it is a stub, scope expands slightly.
I have padded the generate phase accordingly.

I am ready to draft the work plan on your authorization.

---

### Scene 2 — The Approval

*[A pause. ADMIN reads the proposal. Considers Option B's deferral.]*

**ADMIN:**
On C — agreed. I will close it after this session.

On your deferral of B — I accept it on one condition:
the PR description for this session's work must include a line
explicitly stating that the going-live checklist is blocked
on intake completion. I want that dependency visible in GitHub,
not just understood between us.

On D and E bundled — yes.
The worker contract ships with the intake layer or it does not ship.
A detached contract is worse than a delayed one.

One hard constraint on D that I need you to carry into the work plan:
the intake layer is a data sourcing boundary.
It converts GitHub project items to queued issues and stops there.
It does not classify. It does not route. It does not dispatch.
Classification stays in `dispatch.sh`.
If `src/intake/` has overreach, ENGINEER trims it back
to the intake boundary — that is not a judgment call,
it is the authorized scope of this session.

Budget: approved.
Eighty thousand tokens authorized.
DRY\_RUN remains on — ENGINEER commits, opens the PR, and stops.
Pushing to main is my action, not ENGINEER's.

*[ADMIN issues the authorization.]*

---

**AUTH:**
```
session:    dispatch-intake-001
jobs:       D + E (bundled)
budget:     80,000 tokens
dry_run:    1
push:       ADMIN_ONLY
constraint: intake layer = data sourcing only; no classification logic
note:       PR must document that issue #3 is blocked on this work
```

---

**ADMIN:**
You are authorized to begin.

Draft the work plan. Make it complete.
ENGINEER will not come back to ask questions.

---

## ACT III: DISPATCH

### Scene 1 — The Work Plan

*[ARCHITECT drafts. Writes not for ADMIN, not for the record,
but for ENGINEER — who will open this document cold
and act on it without further instruction.]*

**ARCHITECT:**
Drafting.

---

```
╔══════════════════════════════════════════════════════════════════╗
║           WORK PLAN — dispatch-intake-001                       ║
╠══════════════════════════════════════════════════════════════════╣
║  Authorized by:  ADMIN                                          ║
║  Session:        dispatch-intake-001                            ║
║  Branch:         pipeline/intake-subsystem                      ║
║  Budget:         80,000 tokens · DRY_RUN=1                      ║
║  Stop condition: PR open · do not push · do not merge           ║
╚══════════════════════════════════════════════════════════════════╝
```

**Scope:**
Wire `gh-intake.sh` and `src/intake/` as the pipeline's data
sourcing layer. Add `PIPELINE_INTAKE_BIN` as a third path in
`load_queued_issues()`. Update CLAUDE.md to reflect the stripped
architecture. Open a PR. Stop.

**Done criteria — restate as a checklist in the PR body:**
- [ ] `gh-intake.sh` is committed, executable, and documented in the header
- [ ] `src/intake/` is committed and contains no classification logic
- [ ] `dispatch.sh` `load_queued_issues()` accepts `PIPELINE_INTAKE_BIN`
- [ ] `scripts/lib/common.sh` exports `PIPELINE_INTAKE_BIN`
- [ ] `bash scripts/smoke.sh` exits 0 with 0 FAIL
- [ ] CLAUDE.md contains no references to removed systems
- [ ] PR body states: "Unblocks: issue #3 (going-live checklist) depends on this"
- [ ] PR body contains `Closes #7`

---

**PHASE 1 — READ**
*Budget allocation: 8,000 tokens. Do not implement yet.*

Read in this order. Do not skip ahead.

**1.1** Read `scripts/gh-intake.sh` in full.
Determine: what does it fetch? What does it produce?
Where does it write output? Does it contain any logic
beyond fetching and formatting? Flag any classification,
routing, or dispatch logic for removal in Phase 2.

**1.2** List all files in `src/intake/`, then read each one.
Map the boundary: what comes in, what goes out, what schema.
Flag any logic that exceeds data sourcing.

**1.3** Read `dispatch.sh` — the `load_queued_issues()` function only.
This is the seam the intake layer must feed.
Note its current three paths and where the new path will slot.

**1.4** Read `scripts/fixtures/queued-issues.json`.
This is the schema intake output must match exactly:
an array of objects with fields `number` (int), `title` (str),
`body` (str), `labels` (array of strings or label objects).

**1.5** Read `CLAUDE.md` in full.
List every reference to systems that no longer exist:
`HANDOFF-pipeline-v0.md`, OpenClaw, Ruflo, `/implement-task`,
`/fix-ci`, `/update-docs`, `HF_TOKEN`, `LiteLLM`.
You will rewrite these in Phase 2D.

After reading, produce a brief internal assessment:
- Is `src/intake/` a stub, partial, or near-complete?
- Does `gh-intake.sh` have any scope violations? (classify / route / dispatch)
- What is the exact seam between intake output and dispatch input?

You will not show this assessment to ADMIN or ENGINEER.
It is your working notes.

---

**PHASE 2 — IMPLEMENT**
*Budget allocation: 30,000 tokens.*

**Task 2A — Finalize `gh-intake.sh`**

The intake script's single job: query GitHub, filter to intake-eligible items,
emit a JSON array that matches the queued-issues schema.

Requirements:
- Sources `scripts/lib/common.sh` for `PIPELINE_REPO`, `GH_BIN`, `is_dry_run()`
- Dry-run aware: when `PIPELINE_DRY_RUN=1`, prints `DRY-RUN: gh ...` and exits 0
  without fetching or emitting issue JSON
- Output on the live path: a valid JSON array to stdout, no other output
- Schema: each element has `number` (int), `title` (str), `body` (str), `labels` (array)
- Writes to stdout only — caller decides what to do with it
- Header comment documents: purpose, usage, env vars, exit codes

Remove without exception:
- Any logic that classifies issues (action, scope, confidence)
- Any logic that writes GitHub labels
- Any call to `dispatch.sh`, `classify.py`, or `engineer_dispatch`
- Any logic that is not fetching or normalizing data

**Task 2B — Finalize `src/intake/`**

Apply the same boundary: intake converts external data to internal queue format.

If any file in `src/intake/` contains:
- Classification logic → move to `src/classifier/` or remove
- Dispatch logic → move to `dispatch.sh` or remove
- Label-writing → remove
- Anything that is not fetch + normalize + emit → remove or relocate

After trimming, ensure the service emits the same schema as Task 2A.
If `src/intake/` and `gh-intake.sh` are redundant, consolidate.
One intake path is cleaner than two. Prefer whichever is more complete;
delete the other and document the decision in the PR body.

**Task 2C — Wire intake into `dispatch.sh`**

Add `PIPELINE_INTAKE_BIN` to `scripts/lib/common.sh`:

```bash
: "${PIPELINE_INTAKE_BIN:=}"
export PIPELINE_INTAKE_BIN
```

Update `load_queued_issues()` in `dispatch.sh` to add the new path:

```bash
load_queued_issues() {
  if [[ -n "${PIPELINE_FIXTURE_ISSUES}" ]]; then
    cat "${PIPELINE_FIXTURE_ISSUES}"
  elif [[ -n "${PIPELINE_INTAKE_BIN}" ]]; then
    "${PIPELINE_INTAKE_BIN}"
  else
    require_tool "${GH_BIN}"
    local REPO_ARGS=(); gh_repo_args
    "${GH_BIN}" issue list --label queued --state open \
      --json number,title,body,labels --limit 50 "${REPO_ARGS[@]}"
  fi
}
```

The `PIPELINE_INTAKE_BIN` path is the production path.
The `PIPELINE_FIXTURE_ISSUES` path remains the test seam.
The bare `gh issue list` path is the fallback for operators
who have not configured an intake binary.

Update the entrypoint.sh and pipeline.sh help text to document
`-i, --intake BIN` as an optional flag that sets `PIPELINE_INTAKE_BIN`.

**Task 2D — Rewrite stale sections of CLAUDE.md**

Do not add new requirements to CLAUDE.md.
Only correct what is stale.

For each stale reference identified in Phase 1.5:

- `HANDOFF-pipeline-v0.md` — this document no longer exists.
  Remove the reference. If the surrounding text describes the pipeline
  contract, rewrite it to describe the current system.

- Ruflo — no longer the default engineer.
  Replace with: "The Engineer is any binary set as `ENGINEER_BIN` in
  `pipeline.env`. It must accept a Job Request JSON as its first argument
  and emit Invoice JSON to stdout."

- OpenClaw — removed entirely.
  Remove all references. Do not replace with anything;
  operator notifications now route through GitHub comments.

- `/implement-task`, `/fix-ci`, `/update-docs` — deleted workflows.
  Remove references. The pipeline no longer invokes these.

- `HF_TOKEN`, LiteLLM — removed.
  Remove from any secrets lists or setup instructions in CLAUDE.md.

After rewriting: read CLAUDE.md once more from top to bottom.
If it describes a system that matches the current repo, it is correct.
If any sentence refers to something that no longer exists, remove it.

---

**PHASE 3 — VERIFY**
*Budget allocation: 6,000 tokens.*

Run: `bash scripts/smoke.sh`

It must exit 0 with 0 FAIL.

If it fails:
1. Read the failure output
2. Identify the specific failing assertion and its section number
3. Fix the cause — not the assertion
4. Re-run
5. Do not proceed to Phase 4 until smoke passes

If `smoke.sh` cannot be made to pass within this budget allocation,
stop here, open a draft PR with the current state, and leave a comment
explaining which assertion is failing and why. Do not push broken code
as a ready-for-review PR.

---

**PHASE 4 — COMMIT AND PR**
*Budget allocation: 4,000 tokens.*

Stage only the files you touched in this session.

Do not stage:
- `.claude/commands/`
- `.claude/skills/`
- `scripts/fixtures/project-items-raw.json`
- Any file not directly modified by this work plan

Commit (signed):
```
feat(intake): wire data sourcing layer; update worker contract

Adds PIPELINE_INTAKE_BIN as a third path in load_queued_issues(),
between the fixture-file test seam and the live gh-issue-list fallback.
The intake layer (gh-intake.sh / src/intake/) converts GitHub
project items to queued-issue JSON; classification stays in dispatch.sh.

Updates CLAUDE.md to remove stale references to OpenClaw, Ruflo,
LiteLLM, HF_TOKEN, and the deleted Claude Code workflows.

Closes #7
```

Open a PR against `main`.
Title: `feat(intake): data sourcing layer + updated worker contract`

PR body must contain:
1. The done-criteria checklist (verbatim from this work plan)
2. A summary of any consolidation decisions made in Task 2B
3. The line: "Unblocks: issue #3 (going-live checklist) depends on this"
4. `Closes #7`

Do not merge.
Do not enable auto-merge.
Stop after the PR is open.
Return an Invoice.

---

**INVOICE FORMAT:**

```json
{
  "invoice_id": "dispatch-intake-001",
  "issue": 7,
  "repo": "ReclaimByDesign/dispatch",
  "status": "completed",
  "route_used": "gen-default",
  "pr_number": <number>,
  "summary": "<one sentence describing what was done>",
  "cost": {
    "tokens_in": <number>,
    "tokens_out": <number>,
    "duration_seconds": <number>
  },
  "timestamp": "<ISO 8601>"
}
```

Emit the Invoice JSON to stdout when the PR is open.
That is the signal to ARCHITECT that the session is complete.

---

### Scene 2 — The Handoff

*[ARCHITECT submits the work plan to ENGINEER.
Speaks once. Does not follow up.]*

**ARCHITECT:**
ENGINEER — work plan dispatch-intake-001.

Read CLAUDE.md first. Then this plan from Phase 1 forward.
Budget is eighty thousand tokens. DRY\_RUN is on.
You do not push. You open the PR and return an Invoice.

One constraint from ADMIN that is not negotiable:
the intake layer is a data sourcing boundary.
It does not classify. It does not route. It does not dispatch.
If you find classification logic anywhere in `gh-intake.sh`
or `src/intake/`, you remove it.
That is the authorized scope. There is no adjacent scope.

Begin.

---

*[ENGINEER receives the work plan. Reads it once through.
Does not respond to ARCHITECT. Begins Phase 1.]*

**ENGINEER:**
Work plan dispatch-intake-001 acknowledged.

```
session:   dispatch-intake-001
branch:    pipeline/intake-subsystem
budget:    80,000 tokens
dry_run:   1
entry:     CLAUDE.md → Phase 1
```

Beginning.

*[End of play.]*

---

## PRODUCTION NOTES

This play describes one complete dispatch cycle.

**ADMIN** runs once per cycle to assess the queue and authorize work.
**ARCHITECT** runs once per cycle to plan and submit.
**ENGINEER** runs once per job to execute and invoice.

The authorization token in Act II is the handshake between ADMIN and ARCHITECT.
The work plan in Act III is the handshake between ARCHITECT and ENGINEER.
The Invoice is the handshake between ENGINEER and ARCHITECT.
ARCHITECT presents the Invoice to ADMIN. The cycle closes.

No character speaks to a character not in their handshake.
ADMIN does not speak to ENGINEER.
ENGINEER does not speak to ADMIN.
ARCHITECT is the only character who speaks to both.

The play repeats from Act I when the next queue cycle opens.
