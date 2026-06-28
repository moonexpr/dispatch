#!/usr/bin/env python3
"""dispatch.py — entry point + router for the dispatch workflow engine.

Drives the **BaseWorkflow** engine (``engine/`` + ``src/baseworkflow``) — or the
**WebsiteWF** overlay engine (``src/websitewf``) when ``DISPATCH_ENGINE=websitewf`` —
directly over a GitHub issue. The router (this file) fetches the issue, builds the
``job`` + ``triage``, selects the engine, and runs its ``spec -> work -> build``
lifecycle. The engine's **Admin** phase owns the GitHub lifecycle (claim / PR /
closure) gated by ``PIPELINE_DRY_RUN``: dry-run is the default; ``-l/--live`` opts
into real ``gh`` mutations.

The legacy ``src/orchestration`` visitor-pattern tick is retired — its stage-walk
is dead code; the workflow engine is the single execution layer (the engineer runs
in-process via the deserialized orchestration script, so there is no ENGINEER_BIN
shell-out here).

  python3 dispatch.py -r OWNER/REPO [ISSUE] [-l/--live] [-h/--help]

  -r/--repo OWNER/REPO   target repo (or $PIPELINE_REPO)
  ISSUE                  issue number (default: the first open issue)
  -l/--live              PIPELINE_DRY_RUN=0 — mutate GitHub (default: dry-run)
  DISPATCH_ENGINE        baseworkflow (default) | websitewf
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
os.environ["PYTHONPATH"] = _ROOT + (
    os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_ENGINES = {
    "baseworkflow": "src.baseworkflow.baseworkflow",
    "websitewf": "src.websitewf.websitewf",
}


def _usage(code: int = 0) -> int:
    print(__doc__.strip())
    return code


def _parse(argv: list) -> dict:
    repo = os.environ.get("PIPELINE_REPO", "")
    live = False
    issue = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            raise SystemExit(_usage(0))
        elif a in ("-l", "--live"):
            live = True
        elif a in ("-r", "--repo"):
            i += 1
            repo = argv[i]
        elif a.isdigit():
            issue = int(a)
        else:
            print(f"dispatch: unknown argument {a!r}\n", file=sys.stderr)
            raise SystemExit(_usage(2))
        i += 1
    if not repo:
        print("dispatch: a repo is required (-r OWNER/REPO or $PIPELINE_REPO)\n", file=sys.stderr)
        raise SystemExit(_usage(2))
    return {"repo": repo, "live": live, "issue": issue}


def _gh_json(args: list) -> object:
    """Run ``gh ... --json`` and parse the result (read-only; always allowed)."""
    out = subprocess.check_output(["gh", *args], text=True)
    return json.loads(out)


def _fetch_job(repo: str, issue: int | None) -> dict:
    """Build the engine ``job`` from a GitHub issue (the first open one if unset).
    Issue text is untrusted input — carried as data, never executed (HANDOFF §8)."""
    if issue is None:
        listing = _gh_json(["issue", "list", "-R", repo, "--state", "open", "--limit", "1", "--json", "number"])
        if not listing:
            raise SystemExit(f"dispatch: no open issues on {repo}")
        issue = listing[0]["number"]
    d = _gh_json(["issue", "view", str(issue), "-R", repo, "--json", "number,title,body,labels"])
    job = {
        "issue": d["number"],
        "repo": repo,
        "title": d.get("title") or "",
        "body": d.get("body") or "",
        "labels": [lbl["name"] for lbl in (d.get("labels") or [])],
    }
    # Lightweight web-route hints for the WebsiteWF overlay (its bindings default
    # framework/path; surface them from the issue when present).
    text = f"{job['title']}\n{job['body']}"
    m = re.search(r"(?<!\w)(/[\w][\w/-]*)", job["title"])
    if m:
        job["route"] = m.group(1)
    low = text.lower()
    if "laravel" in low:
        job["framework"] = "laravel"
    elif "next" in low:
        job["framework"] = "nextjs"
    return job


def main(argv: list) -> int:
    opts = _parse(argv)
    os.environ["PIPELINE_REPO"] = opts["repo"]
    os.environ["PIPELINE_DRY_RUN"] = "0" if opts["live"] else "1"

    engine = os.environ.get("DISPATCH_ENGINE", "baseworkflow")
    if engine not in _ENGINES:
        print(
            f"dispatch: DISPATCH_ENGINE={engine!r} unknown ({sorted(_ENGINES)}); using baseworkflow",
            file=sys.stderr,
        )
        engine = "baseworkflow"
    mod = importlib.import_module(_ENGINES[engine])

    job = _fetch_job(opts["repo"], opts["issue"])
    # Triage: only implement-class issues reach a tick; classification is a router
    # follow-up (the legacy classifier lived in the retired visitor lineage).
    triage = {"action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.9}

    mode = "LIVE" if opts["live"] else "dry-run"
    print(f"dispatch: engine={engine} repo={opts['repo']} issue=#{job['issue']} mode={mode}")
    summary = mod.run_live(job, triage, dry_run=not opts["live"])

    result = summary["result"]
    deliv = summary.get("deliverables") or {}
    print(f"dispatch: tick complete — ok={result.ok}; deliverables={sorted(deliv)}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
