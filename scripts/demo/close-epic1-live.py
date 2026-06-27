#!/usr/bin/env python3
"""close-epic1-live.py — drive the dispatch engineer LIVE to close epic #1.

A thin Python driver (no shell) that closes epic #1 of the disposable test target
by running the dispatch Engineer (``src/orchestration/engineer_sdk.py``, Claude
SUBSCRIPTION auth) on its two children in dependency order:

  for #4 (Init Next.js+Tailwind) then #5 (site shell):
    1. build a Job Request (schemas/job-request.json) from the live issue;
    2. run the Engineer LIVE (PIPELINE_DRY_RUN=0) → it clones the target, runs the
       claude-code agent on the subscription in a worktree, opens one PR (Closes #N);
    3. merge that PR (squash, delete branch) → #N auto-closes — BEFORE the next
       child, so #5's clone already contains #4's Next.js base.
  then, only if both children closed, close epic #1 (authorized epic-closure) and
  post a digest.

Focused on epic #1 (not the whole backlog) and honest by construction: epic #1 is
closed only if both children actually closed; otherwise a blocker digest is posted
and #1 is left open. Run output is the traceable pipeline log.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = "ReclaimByDesign/dispatch-testrepo-a"
EPIC = 1
CHILDREN = [4, 5]
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (scripts/demo/..)
ENGINEER = os.path.join(ROOT, "src", "orchestration", "engineer_sdk.py")


def sh(*args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def gh_state(num: int) -> str:
    r = sh("gh", "issue", "view", str(num), "-R", REPO, "--json", "state", "--jq", ".state")
    return (r.stdout or "").strip().upper()


def log(msg: str):
    print(f"[close-epic1] {msg}", flush=True)


def run_child(num: int) -> dict:
    """Run the Engineer live on child #num, merge its PR. Returns the Invoice (+merge)."""
    view = sh("gh", "issue", "view", str(num), "-R", REPO, "--json", "title,body")
    meta = json.loads(view.stdout or "{}")
    job = {
        "job_id": f"issue-{num}-live",
        "issue": num,
        "repo": REPO,
        "title": meta.get("title", ""),
        "body": meta.get("body", "") or "",
        "route": "gen-default",
        "scope": "m",
        "confidence": 0.85,
    }
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)               # subscription auth
    env["PIPELINE_DRY_RUN"] = "0"                     # LIVE
    env.setdefault("ENGINEER_BACKEND", "cli")        # SDK 0.2.x hangs under py3.14; CLI works
    env.setdefault("ENGINEER_TIMEOUT_SECONDS", "1800")
    env["PIPELINE_REPO"] = REPO

    log(f"#{num}: running Engineer LIVE ({meta.get('title','')!r})")
    proc = subprocess.run(
        [sys.executable, ENGINEER], input=json.dumps(job),
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    inv = {}
    try:
        inv = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception:
        log(f"#{num}: engineer produced no parseable Invoice; stderr tail: {(proc.stderr or '')[-400:]}")
        return {"status": "failed", "issue": num}
    log(f"#{num}: invoice status={inv.get('status')} pr={inv.get('pr_number')} scope={inv.get('scope_actual')}")

    pr = inv.get("pr_number")
    if inv.get("status") == "completed" and pr:
        log(f"#{num}: merging PR #{pr} (squash, delete-branch)")
        m = sh("gh", "pr", "merge", str(pr), "-R", REPO, "--squash", "--delete-branch")
        if m.returncode != 0:
            log(f"#{num}: merge failed: {(m.stderr or '').strip()[:300]}")
        else:
            log(f"#{num}: merged PR #{pr}")
    return inv


def main() -> int:
    log(f"START live closure of epic #{EPIC} ({REPO}); children={CHILDREN} (dependency order)")
    results = {}
    for n in CHILDREN:
        results[n] = run_child(n)
        if results[n].get("status") != "completed":
            log(f"#{n}: not completed ({results[n].get('status')}) — stopping; epic stays open")
            break

    states = {n: gh_state(n) for n in CHILDREN}
    log(f"child states after run: {states}")
    all_closed = all(states.get(n) == "CLOSED" for n in CHILDREN)

    if all_closed:
        body = (
            "All children delivered → closing.\n\n"
            + "\n".join(f"- #{n}: closed (PR #{results[n].get('pr_number')})" for n in CHILDREN)
            + "\n\nClosed by the dispatch pipeline live roundabout: foundational-first, the "
            "dispatch Engineer running claude-code on the Claude subscription (worktree-isolated, "
            "one PR per child with `Closes #`), squash-merged, then epic closure."
        )
        sh("gh", "issue", "comment", str(EPIC), "-R", REPO, "--body", body)
        c = sh("gh", "issue", "close", str(EPIC), "-R", REPO)
        log(f"epic #{EPIC} close exit={c.returncode}")
    else:
        body = (
            f"Epic #{EPIC} NOT closed — not every child completed this run.\n\n"
            + "\n".join(f"- #{n}: {states.get(n)} (invoice {results.get(n, {}).get('status')})"
                        for n in CHILDREN)
            + "\n\nThe dispatch Engineer ran live on the Claude subscription. Re-run to "
            "finish the remaining child(ren)."
        )
        sh("gh", "issue", "comment", str(EPIC), "-R", REPO, "--body", body)
        log(f"epic #{EPIC} left OPEN (digest posted)")

    log(f"DONE epic #{EPIC} {'CLOSED' if all_closed else 'OPEN'}; states={states}")
    return 0 if all_closed else 2


if __name__ == "__main__":
    raise SystemExit(main())
