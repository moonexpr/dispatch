"""tick_consolidation.py — opt-in tick-level super-PR (DISPATCH_TICK_PR).

The per-issue path (default) is: engineer commits + pushes a ``pipeline/issue-N``
branch, the Administrator (intake) opens ONE consolidated PR for that branch. That
already collapses a *decomposed* issue's unit-commits into a single PR.

This module adds the **cross-issue** layer: when ``DISPATCH_TICK_PR=1``, the
per-issue intake DEFERS PR creation (it records the completed branch and relabels,
but opens no PR), and after the claim loop :func:`consolidate_tick` merges every
branch the engineers completed THIS tick into one integration branch
(``pipeline/tick-<id>``) and opens ONE PR carrying every ``Closes #N``, then arms
squash auto-merge. So a 9-issue tick lands as ONE big PR instead of nine.

Default OFF — nothing here runs unless the flag is set, so the per-issue
consolidated PR stands and the acceptance suite (which asserts the dispatch stage
intends no merge) is unaffected.

Dry-run discipline (HANDOFF §8): under ``PIPELINE_DRY_RUN!=0`` every git/gh call is
recorded via ``common.run`` (the greppable ``DRY-RUN: …`` line) and no clone, merge,
push, or network mutation happens. The live path clones the target repo into the
worktree root (never this checkout), builds the integration branch there, and
removes the clone on exit.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from engine import proc

from . import common


def enabled() -> bool:
    """Is the tick-level super-PR opt-in flag set?"""
    return os.environ.get("DISPATCH_TICK_PR", "0") == "1"


# --------------------------------------------------------------------------- #
# Tick-scoped record of completed branches. The per-issue intake runs in a       #
# SEPARATE process (the engineer→intake bridge), so it cannot hand the branch    #
# back to dispatch.py in memory — it appends to this tick file, which            #
# consolidate_tick() reads after the claim loop (mirrors common.py's .claimed    #
# sink).                                                                          #
# --------------------------------------------------------------------------- #
def _branches_file() -> str:
    explicit = os.environ.get("DISPATCH_TICK_BRANCHES_FILE")
    if explicit:
        return explicit
    ad = os.environ.get("DISPATCH_ARTIFACTS_DIR", ".dispatch")
    tick = os.environ.get("DISPATCH_TICK_ID", "tick-unknown")
    return f"{ad}/{tick}.completed-branches"


def record_completed(issue: str, branch: str) -> None:
    """Append one ``issue\\tbranch`` line for a completed unit (best-effort)."""
    if not branch:
        return
    f = _branches_file()
    try:
        Path(f).parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a") as fh:
            fh.write(f"{issue}\t{branch}\n")
    except OSError:
        pass


def _read_completed() -> list[tuple[str, str]]:
    """Return [(issue, branch), …] recorded this tick, de-duplicated in order."""
    f = _branches_file()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        if f and os.path.getsize(f) > 0:
            for ln in Path(f).read_text().splitlines():
                if not ln.strip() or "\t" not in ln:
                    continue
                issue, branch = ln.split("\t", 1)
                if branch and branch not in seen:
                    seen.add(branch)
                    out.append((issue, branch))
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- #
# The consolidation itself.                                                       #
# --------------------------------------------------------------------------- #
def _default_branch() -> str:
    return os.environ.get("PIPELINE_DEFAULT_BRANCH", "main")


def _super_pr_body(units: list[tuple[str, str]]) -> str:
    closes = "\n".join(f"- Closes #{issue}" for issue, _ in units)
    branch_list = "\n".join(f"- `{branch}` (#{issue})" for issue, branch in units)
    return (
        "Consolidated tick PR opened by the dispatch Administrator. The engineers "
        "committed each issue to its own branch; this single PR squash-merges them "
        f"all.\n\n## Issues\n{closes}\n\n## Branches merged\n{branch_list}"
    )


def consolidate_tick() -> dict:
    """Open ONE integration PR for every branch completed this tick. No-op (and no
    network) unless the flag is set and at least one branch was recorded. Returns a
    small record of what was done / intended.

    Live path: clone the target repo, create ``pipeline/tick-<id>`` off the default
    branch, merge each completed issue branch into it, push, open ONE PR carrying
    every ``Closes #N``, and arm ``pr merge --auto --squash``. A branch that fails
    to merge (conflict) is skipped and noted — the others still ship. Dry-run path:
    record the intended git/gh calls, mutate nothing."""
    if not enabled():
        return {"enabled": False}
    units = _read_completed()
    if not units:
        common.log("tick-pr: no completed branches this tick — no super-PR")
        return {"enabled": True, "units": [], "pr_number": None}

    tick = os.environ.get("DISPATCH_TICK_ID", "tick-unknown")
    integration = f"pipeline/{tick}"
    base = _default_branch()
    repo = os.environ.get("PIPELINE_REPO", "")
    dry = common.is_dry_run()
    common.log(
        f"tick-pr: consolidating {len(units)} completed branch(es) into {integration} "
        f"-> ONE PR (base {base}, dry_run={dry})"
    )

    merged: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    if dry:
        # Record the intended integration without cloning/merging.
        common.run("git", "checkout", "-b", integration, f"origin/{base}")
        for issue, branch in units:
            common.run("git", "merge", "--no-ff", f"origin/{branch}",
                       "-m", f"Integrate #{issue} ({branch})")
            merged.append((issue, branch))
        common.run("git", "push", "-u", "origin", integration)
        common.gh_mutate("pr", "create", "--base", base, "--head", integration,
                         "--title", f"Tick {tick}: consolidate {len(units)} issue(s)",
                         "--body", _super_pr_body(merged))
        common.gh_mutate("pr", "merge", integration, "--auto", "--squash")
        return {"enabled": True, "units": units, "merged": merged, "skipped": skipped,
                "integration": integration, "pr_number": None, "dry_run": True}

    # --- live: clone, build the integration branch, push, open the PR ---
    gh_bin = os.environ.get("GH_BIN", "gh")
    work_root = os.environ.get("PIPELINE_WORKTREE_ROOT", str(common.PIPELINE_ROOT / ".worktrees"))
    os.makedirs(work_root, exist_ok=True)
    clone_dir = tempfile.mkdtemp(prefix=f"{tick}-superpr-", dir=work_root)
    shutil.rmtree(clone_dir, ignore_errors=True)  # gh clone wants a non-existent dest
    pr_number = None
    try:
        if proc.run([gh_bin, "repo", "clone", repo, clone_dir], capture=True).returncode != 0:
            common.log(f"tick-pr: clone of {repo} failed — no super-PR")
            return {"enabled": True, "units": units, "pr_number": None, "error": "clone-failed"}

        def _git(*a):
            return proc.run(["git", "-C", clone_dir, *a], capture=True)

        if _git("checkout", "-b", integration, f"origin/{base}").returncode != 0:
            common.log(f"tick-pr: could not create {integration} off origin/{base}")
            return {"enabled": True, "units": units, "pr_number": None, "error": "branch-failed"}

        for issue, branch in units:
            _git("fetch", "origin", branch)
            res = _git("merge", "--no-ff", "FETCH_HEAD", "-m", f"Integrate #{issue} ({branch})")
            if res.returncode != 0:
                _git("merge", "--abort")
                skipped.append((issue, branch))
                common.log(f"tick-pr: #{issue} ({branch}) did not merge cleanly — skipped")
            else:
                merged.append((issue, branch))

        if not merged:
            common.log("tick-pr: no branch merged cleanly — no super-PR opened")
            return {"enabled": True, "units": units, "merged": [], "skipped": skipped,
                    "pr_number": None}

        if _git("push", "-u", "origin", integration).returncode != 0:
            common.log(f"tick-pr: push of {integration} failed — no super-PR")
            return {"enabled": True, "units": units, "merged": merged, "skipped": skipped,
                    "pr_number": None, "error": "push-failed"}

        pr = proc.run(
            [gh_bin, "pr", "create", "--repo", repo, "--base", base, "--head", integration,
             "--title", f"Tick {tick}: consolidate {len(merged)} issue(s)",
             "--body", _super_pr_body(merged)],
            capture=True,
        )
        url = (pr.stdout or "").strip()
        tail = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        if pr.returncode == 0 and tail.isdigit():
            pr_number = int(tail)
            common.log(f"tick-pr: opened super-PR #{pr_number} ({len(merged)} issue(s))")
            # Arm squash auto-merge; branch protection still holds the merge.
            proc.run([gh_bin, "pr", "merge", str(pr_number), "--auto", "--squash",
                      "--repo", repo], capture=True)
        else:
            common.log(f"tick-pr: super-PR creation failed (rc={pr.returncode})")
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)

    return {"enabled": True, "units": units, "merged": merged, "skipped": skipped,
            "integration": integration, "pr_number": pr_number, "dry_run": False}
