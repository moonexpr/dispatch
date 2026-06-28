#!/usr/bin/env python3
"""Assemble the consolidated dispatch-1.0 3-day roadmap from the two design
workflow outputs + operator-added decision/chore issues, into 02-roadmap.json.

Inputs (workflow result files, read-only):
  ROADMAP_OUT  — 7-epic / 20-issue build plan (w9pof1zvq)
  HARNESS_OUT  — E8 demo-repo test-harness epic + E0-1 demo bootstrap (wlr5gplcx)

Output:
  doc/specs/v1-unattended-repo-monitoring/02-roadmap.json   (full, for creation)
  + a compact day-by-day tree printed to stdout for operator review.

No GitHub mutation. Deterministic.
"""
import json, os, sys

SPEC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROADMAP_OUT = "/private/tmp/claude-501/-Users-jc-Mendotree-dispatch/1b32613e-55ea-4bc9-b20f-6b35c05c2901/tasks/w9pof1zvq.output"
HARNESS_OUT = "/private/tmp/claude-501/-Users-jc-Mendotree-dispatch/1b32613e-55ea-4bc9-b20f-6b35c05c2901/tasks/wlr5gplcx.output"

build = json.load(open(ROADMAP_OUT))["result"]
harness = json.load(open(HARNESS_OUT))["result"]

# ---- labels: union of build.new_labels + a 'stretch' lane ----
labels = list(build["new_labels"])
labels.append({"name": "stretch", "color": "ededed",
               "description": "Post-1.0 stretch — deferred out of the 3-day critical path by the sprint critic."})
labels.append({"name": "decision", "color": "d876e3",
               "description": "An operator decision to commit before/early in the sprint (resolves a brainstorm clarification)."})

milestone = build["milestone"]

# ---- epics E1..E7 from build plan ----
epics = []
for e in build["epics"]:
    epics.append({
        "local_id": e["local_id"], "title": e["title"], "day_span": e["day_span"],
        "body_md": e["body_md"], "labels": e["labels"],
        "issues": [{
            "local_id": i["local_id"], "title": i["title"], "day": i["day"],
            "estimate_hours": i["estimate_hours"], "labels": i["labels"],
            "depends_on": i["depends_on"], "body_md": i["body_md"], "stretch": False,
        } for i in e["issues"]],
    })

# ---- apply critic cuts: mark stretch items ----
CRITIC = build["critic"]
STRETCH_IDS = {"E2-3"}   # replay → clear post-1.0 cut per critic
def mark_stretch(eid_issue):
    for ep in epics:
        for it in ep["issues"]:
            if it["local_id"] == eid_issue:
                it["stretch"] = True
                if "stretch" not in it["labels"]:
                    it["labels"].append("stretch")
for sid in STRETCH_IDS:
    mark_stretch(sid)

# ---- append verifier correctness-constraint notes to specific harness issues ----
VERIFIER = harness["verdict"]["blocking_problems"]
verifier_block = (
    "\n\n## Verifier notes (correctness constraints — confirmed by running the real classifier/schema)\n"
    "The sprint's adversarial verifier executed the actual code paths and found these; honor them or the smoke asserts will FAIL:\n"
    + "\n".join(f"- {p}" for p in VERIFIER)
)

# ---- epic E8 + children from harness plan ----
he = harness["synth"]["epic"]
e8 = {
    "local_id": he["local_id"], "title": he["title"], "day_span": he["day_span"],
    "body_md": he["body_md"], "labels": he["labels"], "issues": [],
}
REVISE_IDS = {x["local_id"] for x in harness["verdict"]["issue_verdicts"] if x["verdict"] == "revise"}
for i in harness["synth"]["issues"]:
    body = i["body_md"]
    if i["local_id"] in REVISE_IDS:
        body = body + verifier_block
    e8["issues"].append({
        "local_id": i["local_id"], "title": i["title"], "day": i["day"],
        "estimate_hours": i["estimate_hours"], "labels": i["labels"],
        "depends_on": i["depends_on"], "body_md": body, "stretch": False,
    })
epics.append(e8)

# ---- standalone issues: demo setup (E0-1) + operator decisions + chores/docs ----
ds = harness["synth"]["demo_setup_issue"]
ds_body = ds["body_md"] + verifier_block if ds["local_id"] in REVISE_IDS else ds["body_md"]
standalone = [{
    "local_id": ds["local_id"], "title": ds["title"], "day": ds["day"],
    "estimate_hours": ds["estimate_hours"], "labels": ds["labels"],
    "depends_on": ds["depends_on"], "body_md": ds_body, "stretch": False,
    "epic": "E8",
}]

def issue(local_id, title, day, hours, labels, depends_on, body, epic="", stretch=False):
    return {"local_id": local_id, "title": title, "day": day, "estimate_hours": hours,
            "labels": labels, "depends_on": depends_on, "body_md": body, "epic": epic, "stretch": stretch}

standalone.append(issue(
    "DEC-1", "decide(budget): commit operator plan tier + soft-cap fraction to tuning.json", "Day 1", 1.0,
    ["decision", "day-1", "pillar-3-budget"], [],
    "## Summary\nResolve brainstorm clarification Q3-residual: which Claude Code subscription window the budget guard (E5-3) reconciles against, and at what fraction it throttles. The Claude-Code-Usage-Monitor model (D2) needs a concrete plan tier — **Pro 44k / Max5 88k / Max20 220k tokens per 5h window**, or **Custom (P90 of last 192h)** — and a soft-cap fraction (e.g. 0.80) at which a tick auto-flips to dry-run.\n\n## Acceptance Criteria\n- [ ] `services/tuning.json` gains a `budget.window` block: `plan_tier` (one of pro|max5|max20|custom), `window_token_limit` (resolved int, or 'p90' marker), `soft_cap_fraction` (0<f<=1), and `window_hours` (default 5).\n- [ ] `services/tuning.py` exposes these with documented defaults; no code path hardcodes a tier.\n- [ ] The chosen values are recorded in a one-paragraph note in the issue thread (which plan the operator is actually on).\n- [ ] E5-3 (soft-cap guard) reads ONLY from this block — no second source of truth.\n\n## Test Procedure\nAdd a tiny offline assertion (smoke §7.x or a `tuning.py` self-test) that the `budget.window` block parses and `soft_cap_fraction` is in (0,1]. No network.\n\n## Context & References\n- `services/tuning.json`, `services/tuning.py`. Brainstorm §5 (Claude-Code-Usage-Monitor model), §6 Q3, decision D2.\n- Blocks: E5-3 (pre-flight soft-cap guard) and informs E5-1 (claude-monitor oracle plan flag).\n\n## Dependencies\nNone (a decision). Should land Day 1 so E5 can build against committed values.\n\n## Out of scope\nThe guard implementation itself (E5-3); usage-oracle integration (E5-1).",
    epic="E5"))

standalone.append(issue(
    "DEC-2", "decide(failure): reaper timeout + engineer-crash retry policy", "Day 1", 1.0,
    ["decision", "day-1", "pillar-1-cron"], [],
    "## Summary\nResolve brainstorm clarification Q6: when an engineer crashes mid-tick leaving an issue stuck in `claimed` with no Invoice, what does the reaper (E1-3) do, and do dispatch-layer engineer failures auto-retry? Today only CI failures retry (fix-ladder).\n\n## Acceptance Criteria\n- [ ] A committed policy: reaper timeout (e.g. claimed >N hours with no PR → re-queue), captured as a `tuning.json` value `recovery.reaper_timeout_hours`.\n- [ ] Decision recorded: on engineer non-zero exit / `failed` status mid-tick — auto-retry once, escalate to `needs-human`, or leave for the reaper? Written into the issue thread and reflected in E1-3's acceptance.\n- [ ] The re-queue action is ledger-noted (E4) and idempotent (re-queueing an already-queued issue is a no-op).\n\n## Test Procedure\nE1-3's §7.12 reaper assertion encodes the chosen timeout against a fixture issue with a synthetic 'claimed-since' marker; offline, dry-run.\n\n## Context & References\n- Brainstorm §4 Pillar 1 (c) crash recovery, §6 Q6. `services/tuning.json`, `scripts/lib/common.sh` label transitions.\n- Blocks: E1-3 (reaper). Cross-link existing #14 (ruflo SubagentStop recovery — different layer).\n\n## Dependencies\nNone (a decision). Land Day 1 so E1-3 builds against it.\n\n## Out of scope\nThe reaper implementation (E1-3); ruflo hook-layer recovery (#14).",
    epic="E1"))

standalone.append(issue(
    "CHORE-1", "chore(ci): extend shellcheck glob to cover scripts/demo + new sprint scripts", "Day 1", 1.0,
    ["enhancement", "day-1"], ["E8-1"],
    "## Summary\n`.github/workflows/ci.yml` runs shellcheck over a fixed glob (`scripts/*.sh scripts/lib/*.sh`). The sprint adds `scripts/demo/*.sh` (E8) and possibly other new scripts; without extending the glob, new shell is unlinted in CI.\n\n## Acceptance Criteria\n- [ ] The shellcheck step's glob in `ci.yml` covers every new script directory the sprint introduces (at minimum `scripts/demo/*.sh`).\n- [ ] `bash scripts/smoke.sh` and shellcheck both still pass locally.\n- [ ] No new scripts are silently excluded (a quick `git ls-files 'scripts/**/*.sh'` cross-check is noted in the PR).\n\n## Test Procedure\nRun shellcheck locally over the new glob; assert exit 0. CI is the gate.\n\n## Context & References\n- `.github/workflows/ci.yml` (shellcheck + smoke step). Brainstorm critic 'missing' item. OPEN-QUESTIONS A7 (CI == smoke.sh).\n\n## Dependencies\nSoft: lands once `scripts/demo/` exists (E8-1). Can be done anytime the new dirs are known.\n\n## Out of scope\nNew CI jobs beyond shellcheck/smoke; the ruflo-swarm CI path (#16).",
    epic="E7"))

standalone.append(issue(
    "CHORE-2", "test(smoke): renumber/contiguity audit of §7.x sections before filing pillar smokes", "Day 1", 1.0,
    ["enhancement", "day-1"], [],
    "## Summary\n`scripts/smoke.sh` ends at §7.10 today, but the sprint's issues reference §7.11–§7.22 across many epics filed in parallel. Without a single owner of section numbering, two issues will collide on the same §7.x number. This issue establishes a contiguous numbering map and a CI guard against duplicate section ids.\n\n## Acceptance Criteria\n- [ ] A committed numbering map (a comment block at the top of smoke.sh's §7 region, or a short doc) assigns each sprint pillar its §7.x range, with NO overlaps: harness §7.11–§7.15, cron §7.16–§7.18, architect §7.19–§7.20, budget/monitoring §7.21, integration §7.22 (adjust to fit; the point is one authority).\n- [ ] A guard (a tiny check in smoke.sh or CI) fails if two sections share a number.\n- [ ] Existing §7.1–§7.10 are untouched.\n\n## Test Procedure\nThe duplicate-section guard runs as part of `bash scripts/smoke.sh`; assert it passes with the assigned map and fails on an injected duplicate.\n\n## Context & References\n- `scripts/smoke.sh` §7 region. Brainstorm critic 'missing' item. Coordinates with #16 (ruflo smoke additions — must not collide).\n\n## Dependencies\nNone. Should land Day 1 so every later smoke-adding issue draws from the map.\n\n## Out of scope\nThe pillar assertions themselves (their own issues).",
    epic="E7"))

standalone.append(issue(
    "DOC-1", "docs(README): update quickstart + architecture for the five 1.0 capabilities + demo harness", "Day 3", 2.0,
    ["enhancement", "day-3"], ["E7-1"],
    "## Summary\nThe README's quickstart/layout/engines sections predate the sprint. Update them to document: the cron + flock scheduler path, DISPATCH_ARTIFACTS_DIR + --until breakpoints, the JSONL run-ledger + `dispatch report` digest, the budget soft-cap guard, the research-mode work order, and the `scripts/demo/` snapshot→mutate→replay harness against ReclaimByDesign/demo-repository.\n\n## Acceptance Criteria\n- [ ] README 'Usage', 'Layout', 'Engines', and 'Verify' sections reflect the new env vars (DISPATCH_LOCK_FILE, DISPATCH_ARTIFACTS_DIR, budget.window, recovery.*) and new scripts.\n- [ ] A new 'Reusable demo testing' subsection documents the snapshot/mutate/replay loop with a copy-paste example against a committed scenario.\n- [ ] Cross-references RUNBOOK.md (E7-2) rather than duplicating it.\n- [ ] No stale references (e.g. the absent HANDOFF-pipeline-v0.md is either restored-as-link or de-referenced).\n\n## Test Procedure\nDocs-only; the gate is `bash scripts/smoke.sh` still green + a manual read. No new asserts required.\n\n## Context & References\n- `README.md`. Brainstorm critic 'missing' item; pairs with E7-2 (RUNBOOK).\n\n## Dependencies\nE7-1 (so the README documents rails that actually landed). Document-as-you-go is acceptable; finalize after integration.\n\n## Out of scope\nThe operator RUNBOOK (E7-2); going-live ops (#3).",
    epic="E7"))

# ---- existing-issue disposition (all keep-open) ----
existing = build["existing_issue_disposition"]

plan = {
    "milestone": milestone,
    "new_labels": labels,
    "epics": epics,
    "standalone_issues": standalone,
    "existing_issue_disposition": existing,
    "sequencing_notes": build["sequencing_notes"],
    "critic": CRITIC,
    "harness_integration_notes": harness["synth"]["integration_notes"],
}

out_path = os.path.join(SPEC, "02-roadmap.json")
with open(out_path, "w") as f:
    json.dump(plan, f, indent=2, ensure_ascii=False)

# ---- compact review tree ----
def day_n(d):
    d = (d or "").strip()
    return d[-1] if d and d[-1] in "123" else "?"

all_issue_rows = []
for ep in epics:
    for it in ep["issues"]:
        all_issue_rows.append((ep["local_id"], it))
for it in standalone:
    all_issue_rows.append((it.get("epic", "-"), it))

total_hours = sum(it["estimate_hours"] for _, it in all_issue_rows)
day_hours = {}
for _, it in all_issue_rows:
    day_hours[day_n(it["day"])] = day_hours.get(day_n(it["day"]), 0) + it["estimate_hours"]

print(f"MILESTONE: {milestone['title']}")
print(f"NEW LABELS ({len(labels)}): " + ", ".join(l["name"] for l in labels))
print(f"\nEPICS: {len(epics)}   CHILD/STANDALONE ISSUES: {len(all_issue_rows)}   "
      f"TOTAL OBJECTS (epics+issues+milestone): {len(epics)+len(all_issue_rows)+1}")
print(f"TOTAL EST: {total_hours:.0f}h   Hours/day: " + str({k: round(v) for k, v in sorted(day_hours.items())}))

print("\n=== TREE ===")
for ep in epics:
    print(f"\n## {ep['local_id']}: {ep['title'].split(':',1)[-1].strip()[:62]}  [{ep['day_span']}]")
    for it in ep["issues"]:
        s = " *STRETCH*" if it["stretch"] else ""
        dep = ",".join(it["depends_on"]) or "-"
        print(f"   {it['local_id']:6} D{day_n(it['day'])} {it['estimate_hours']:>4}h dep:{dep:11} {it['title'][:70]}{s}")
print("\n## STANDALONE (decisions / chores / demo-setup)")
for it in standalone:
    dep = ",".join(it["depends_on"]) or "-"
    print(f"   {it['local_id']:7} D{day_n(it['day'])} {it['estimate_hours']:>4}h dep:{dep:7} ->{it.get('epic','-'):3} {it['title'][:66]}")

print("\n=== EXISTING ISSUES: all keep-open (not duplicated) ===")
print("   #" + ", #".join(str(x["number"]) for x in existing))
print(f"\nWrote {out_path}")
