# Demo harness — offline scenario catalog

The `scripts/demo/` harness lets a tester exercise the dispatch pipeline against
a **representative web-dev backlog** entirely offline — no GitHub, no model, no
mutations. The flow is **snapshot → mutate → replay**:

1. **`snapshot.sh`** (E8-1) captures the live demo queue (read-only) into a
   committed fixture, `snapshots/demo-current.json` (the dual-consumer superset:
   `number,title,body,labels,assignees,url,state`, labels kept as `{name}`
   objects). A hand-built sample ships so the harness works before the live
   `ReclaimByDesign/demo-repository` is seeded (E0-1 / #47).
2. **`mutate.py`** (E8-2) resolves a base snapshot **+ an overlay scenario** into
   a fully-resolved fixture (deterministic, offline, never mutates the base).
3. **`replay.sh`** (E8-3) runs one offline dry-run tick through `./dispatch
   --fixture` and dumps the round's artifacts (`fixture.json`, `work-order.txt`,
   `job-request.json`, `invoice.json`) under the gitignored `rounds/` tree.

```sh
# Replay a scenario (writes scripts/demo/rounds/<scenario>/<n>/):
scripts/demo/replay.sh feature-with-acceptance
# Reset a scenario's rounds (transient output only; committed files untouched):
scripts/demo/replay.sh --reset feature-with-acceptance
```

## Scenario catalog (E8-4)

Each scenario is a small overlay on `demo-current.json`. The **expected
action/route is a property of the real `classify.py`** (verified by running the
classifier on the resolved fixture), not a hardcoded label — so the catalog
stays honest if classifier tuning changes. Routes come from `route_for_scope()`
in `scripts/lib/common.sh`.

| Scenario file | Target issue | Intent | Expected action / route |
|---|---|---|---|
| `bug-fix.json` | #101 | A clear, narrow bug (typo / minor tweak) | `implement` · `gen-local` (xs) |
| `feature-with-acceptance.json` | #102 | A well-specified feature with acceptance criteria | `implement` · `gen-default` (m) |
| `large-refactor.json` | #104 | An oversized migration / refactor | `implement` · `gen-frontier` (l) |
| `vague-needs-human.json` | #103 | A body so thin the classifier defers | `implement` **below `PIPELINE_CONFIDENCE_THRESHOLD` (0.55)** → operator defer |
| `out-of-scope-wont-do.json` | #105 | A decline-shaped ask ("by design / not planned") | `wont-do` |

> **Route hint tokens (real-classifier behaviour).** `gen-local` (xs/s) is only
> reached via small-change tokens (`typo`, `rename`, `lint`, `small`, `minor`,
> `tweak`); `wont-do` via decline tokens (`wontfix`, `won't do`, `by design`,
> `not planned`). The scenario bodies embed these deliberately — without them the
> same issues classify as `gen-default`/`implement`. The "vague" case surfaces as
> a **low-confidence defer**: `./dispatch` prints `skip (conf … < 0.55)` and
> exits 3 — the literal `needs-human` label is applied on the `dispatch.sh` path,
> not by `./dispatch`.

### Mechanic scenarios (E8-2 examples)

These back the mutator's own smoke assertions rather than a classifier route:

| Scenario file | Op exercised |
|---|---|
| `blank-vague-body.json` | `set-body` (empties #103) |
| `drop-issue.json` | `remove-issue` (#105) |
| `add-bug.json` | `add-issue` (#901) |

## Notes

- Snapshots, scenarios, and the sample seed are **committed**; only the
  per-round `rounds/` output is gitignored (transient, regenerable).
- The committed `demo-current.json` is a drop-in for the `./dispatch --fixture`
  and `intake.py` paths (object-shaped labels). The bash `dispatch.sh` path uses
  the string-label `scripts/fixtures/queued-issues.json` instead.
