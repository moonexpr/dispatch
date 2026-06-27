# docs/research/

Committed findings from **research-mode work orders** (Pillar 2, E6-1/E6-2).

When the architect's gap-detection heuristic
(`src/architect/research.py::detect_gap`) decides a job lacks grounded
information to plan well, it emits a *research* work order instead of an
implementation order (decision **D3**: the architect itself never does live
research — it only chooses the mode). The research worker investigates the gap
and commits a single deliverable here:

```
docs/research/<topic>.md
```

where `<topic>` is the deterministic slug `issue-<n>-<slugified-title>` (e.g.
`docs/research/issue-302-integrate-the-quux-analytics-client.md`).

A research file should summarize the **API surface / prior art / recommended
approach** for the gap — enough that a follow-up implementation order can
proceed. The research worker opens one PR against `main` (never merged by the
worker) and does **not** implement the feature.

Dispatch-path actuation is gated by `generation.research.dispatch_enabled` in
`app/config/tuning.yml` (default off until repo-file discovery is target-repo
aware); the gap heuristic itself is governed by `generation.research.enabled`.
