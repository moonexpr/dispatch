#!/usr/bin/env python3
"""oneshot_feed.py — feed tasks straight into the dispatch engine, bypassing intake.

The normal entry point (``dispatch.py``) fetches a job from a GitHub issue and runs
one tick. This feeder does the same spec → work → build run, but builds the engine
``job`` from a **local task spec** instead of a GitHub issue — so an operator can
drive the pipeline directly without filing an issue and waiting for intake. Tasks
are processed **sequentially**: each task walks the full pipeline before the next
begins.

It mirrors ``dispatch.py``'s environment exactly (PYTHONPATH bootstrap, pipeline.env
load, ``MODELS_BACKEND=cli`` default, dry-run gating) and calls the same engine
``run_live(job, triage, dry_run=...)`` — so behaviour is identical to a real tick,
minus the GitHub issue fetch.

Tasks file: a JSON array of objects, each:
    {
      "title": "...",                 # required
      "body":  "...",                 # required — goal + acceptance criteria + scope
      "repo":  "owner/repo",          # optional (else --repo / $PIPELINE_REPO)
      "engine": "baseworkflow",       # optional (baseworkflow | websitewf)
      "labels": ["..."],              # optional
      "route":  "/pricing",           # optional web route hint (websitewf)
      "framework": "nextjs",          # optional
      "usecase": "scaffold-foundation",  # optional websitewf use-case (see below)
      "triage": {"action": "implement", "scope": "m",
                 "route": "gen-default", "confidence": 0.9}   # optional override
    }

WebsiteWF use-cases (the ``usecase`` field / ``--usecase`` flag)
---------------------------------------------------------------
The websitewf engine is not one workflow but a set of use-case overlays selected
by the ``WEBSITEWF_USECASE`` env var (``websitewf.websitewf._USECASES``): e.g.
``scaffold-foundation`` (create the Next.js/Laravel app skeleton) vs the default
proof-vertical overlay (ADD one route/feature to an *existing* app). Until this
feeder could set that var, every oneshot task ran under the default overlay — so
feeding a "build a website" plan into a bare repo produced feature code with no
foundation, each engineer improvising a different stack against an empty clone.

``usecase`` closes that gap: set the first task's use-case to ``scaffold-foundation``
so the foundation is built before the feature tasks run. It maps 1:1 to
``WEBSITEWF_USECASE`` and is applied per task (a task without ``usecase`` falls back
to ``--usecase``, then to the default overlay). Ignored by the baseworkflow engine.

Usage:
    python scripts/oneshot_feed.py tasks.json
    python scripts/oneshot_feed.py tasks.json --repo owner/repo
    python scripts/oneshot_feed.py tasks.json --engine websitewf --live -v
    # foundation-first: scaffold, then features (per-task usecase in the JSON)
    python scripts/oneshot_feed.py tasks.json --engine websitewf --usecase scaffold-foundation --live

Dry-run is the default — the engine's real inference returns a deterministic
placeholder and no GitHub mutation occurs. ``--live`` runs the real model. Because
there is no backing GitHub issue, ``--live`` is for engineering-only runs; the
Admin phase's issue-keyed GitHub steps have no real issue to act on.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # app/scripts/ -> repo root

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
os.environ["PYTHONPATH"] = _ROOT + (
    os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from foundation import runtime as _runtime

    _preserve = ("PIPELINE_DRY_RUN", "PIPELINE_REPO", "DISPATCH_ENGINE",
                 "MODELS_BACKEND", "CLAUDE_CODE_OAUTH_TOKEN")
    for _envfile in (".env", "pipeline.env"):
        _runtime.load_dotenv(os.path.join(_ROOT, _envfile), preserve=_preserve)
except Exception:  # noqa: BLE001 — dotenv is optional.
    pass

os.environ.setdefault("MODELS_BACKEND", "cli")

_ENGINES = {
    "baseworkflow": "baseworkflow.baseworkflow",
    "websitewf": "websitewf.websitewf",
}

_DEFAULT_TRIAGE = {"action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.9}


def _title_body_from(task: dict) -> tuple[str, str]:
    """Resolve (title, body) from a task that may be structured or a bare prompt.

    A ``prompt`` seeds both when the structural fields are absent: title is the
    first non-empty line (trimmed), body is the whole prompt. Structural values
    still win."""
    title = task.get("title") or ""
    body = task.get("body") or ""
    prompt = (task.get("prompt") or "").strip()
    if prompt and not (title and body):
        first = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "")
        title = title or first[:72].rstrip()
        body = body or prompt
    return title, body


def _build_job(task: dict, repo: str, index: int) -> dict:
    """Build the engine ``job`` from a local task spec (no GitHub fetch).

    The task body is operator-authored here, but is still carried as DATA and never
    executed — same discipline as untrusted issue text (HANDOFF §8). The negative
    ``issue`` is a LOCAL work-id for the engineer's clone/branch naming, NOT a
    GitHub issue: the build phase honours the negative marker and performs no
    issue-keyed GitHub mutation (see admin.publish/consolidate_pr/intake_invoice)."""
    title, body = _title_body_from(task)
    if not title or not body:
        raise SystemExit(f"oneshot: task #{index + 1} needs 'title'+'body' or a 'prompt'")
    job = {
        # Synthetic, negative issue id marks this as a no-GitHub oneshot job.
        "issue": task.get("issue", -(index + 1)),
        "repo": task.get("repo") or repo,
        "title": title,
        "body": body,
        "labels": list(task.get("labels") or []),
    }
    if task.get("route"):
        job["route"] = task["route"]
    if task.get("framework"):
        job["framework"] = task["framework"]
    return job


def _build_request(task: dict, repo: str) -> dict:
    """Build the seed controller's raw intake (the front door) from the task.

    The remodel: inputs need not be in the issue format. A real GitHub issue ->
    ``github`` (the issue is pulled); a bare ``prompt`` -> the seed content-
    classifies it and handles it interactively (fields seeded from the prompt,
    optional gaps left open); otherwise a non-interactive ``job`` request. The
    text is carried as DATA, never executed."""
    req: dict = {"repo": task.get("repo") or repo}
    issue = task.get("issue")
    if issue and int(issue) > 0:
        req["source"] = "github"
        req["issue"] = issue
    elif task.get("prompt") and not (task.get("title") or task.get("body")):
        # No explicit source: seed_ingest infers 'interactive' and seeds fields
        # from the prompt, so a bare prompt needs no title/goal split.
        req["prompt"] = task["prompt"]
    else:
        title, body = _title_body_from(task)
        req["source"] = "job"
        req["job"] = {
            "title": title,
            "goal": body or title,
            "acceptance": task.get("acceptance") or "",
        }
    return req


def _provision(dest: str, template: str) -> None:
    """Ensure the demo target repo exists and is seeded from a template before the
    pipeline clones it (engineer:act_clone needs a cloneable repo). Delegates to
    the persisted helper so the same provisioning is reusable and greppable."""
    script = os.path.join(_ROOT, "app", "scripts", "demo", "provision-from-template.sh")
    if not os.path.isfile(script):
        raise SystemExit(f"oneshot: provisioning helper not found: {script}")
    cmd = [script, dest] + ([template] if template else [])
    print(f"oneshot: provisioning {dest} from {template or '<default>'} ...", flush=True)
    if subprocess.call(cmd) != 0:
        raise SystemExit(f"oneshot: provisioning {dest} failed")


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Feed local tasks straight into the dispatch engine.")
    ap.add_argument("tasks", help="JSON array of task specs (or '-' for stdin)")
    ap.add_argument("-r", "--repo", default=os.environ.get("PIPELINE_REPO", ""))
    ap.add_argument("-e", "--engine", default=os.environ.get("DISPATCH_ENGINE", "baseworkflow"))
    ap.add_argument("-l", "--live", action="store_true", help="PIPELINE_DRY_RUN=0 — run the real model")
    ap.add_argument("-v", "--verbose", action="store_true", help="stream the engine action trace")
    ap.add_argument("-u", "--usecase", default=os.environ.get("WEBSITEWF_USECASE") or None,
                    help="run-wide websitewf use-case (WEBSITEWF_USECASE); per-task 'usecase' overrides it. "
                         "e.g. scaffold-foundation")
    ap.add_argument("--provision", default="", metavar="OWNER/REPO",
                    help="create + seed this repo from --template before feeding (so the "
                         "pipeline has a cloneable target). Demo convenience.")
    ap.add_argument("--template", default="", metavar="DIR",
                    help="template directory to seed the --provision repo from")
    args = ap.parse_args(argv)

    raw = sys.stdin.read() if args.tasks == "-" else open(args.tasks, encoding="utf-8").read()
    try:
        tasks = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"oneshot: tasks file is not valid JSON: {exc}", file=sys.stderr)
        return 1
    if not isinstance(tasks, list) or not tasks:
        print("oneshot: tasks file must be a non-empty JSON array", file=sys.stderr)
        return 1

    engine = args.engine if args.engine in _ENGINES else "baseworkflow"
    if engine != args.engine:
        print(f"oneshot: unknown engine {args.engine!r}; using baseworkflow", file=sys.stderr)
    os.environ["DISPATCH_ENGINE"] = engine
    os.environ["PIPELINE_DRY_RUN"] = "0" if args.live else "1"
    if args.verbose:
        os.environ["DISPATCH_DEBUG"] = "1"
    mod = importlib.import_module(_ENGINES[engine])

    # Known websitewf use-case slugs, for validating the usecase field/flag. An
    # unknown value silently degrades to the default overlay in the engine, so we
    # warn loudly here rather than let a typo scaffold nothing. None → not websitewf.
    known_usecases = None
    if engine == "websitewf":
        known_usecases = {slug for slug in getattr(mod, "_USECASES", {}) if slug}

    mode = "LIVE" if args.live else "dry-run"
    print(f"oneshot: engine={engine} mode={mode} tasks={len(tasks)} (bypassing GitHub intake)")

    # Enable live-trace sidecars by default so the run is watchable in the debug viewer
    # (tools/debugviewer → "live runs"). Opt out with DISPATCH_LIVE_TRACE=0.
    if os.environ.get("DISPATCH_LIVE_TRACE", "1") not in ("0", "", "false", "False"):
        os.environ.setdefault("DISPATCH_LIVE_TRACE", "1")
        try:
            from foundation import trace
            print(f"oneshot: live trace → {trace.trace_dir()} (watch it in the debug viewer)")
        except Exception:  # noqa: BLE001
            pass

    if args.provision:
        _provision(args.provision, args.template)

    failures = 0
    for i, task in enumerate(tasks):
        job = _build_job(task, args.repo, i)
        request = _build_request(task, args.repo)
        triage = {**_DEFAULT_TRIAGE, **(task.get("triage") or {})}

        # Select the websitewf use-case overlay for THIS task (WEBSITEWF_USECASE is
        # read per WebsiteWF instance inside run_live). Per-task 'usecase' wins over
        # the run-wide --usecase; absence means the default (feature-addition) overlay.
        usecase = task.get("usecase") or args.usecase
        if usecase:
            os.environ["WEBSITEWF_USECASE"] = usecase
            if known_usecases is not None and usecase not in known_usecases:
                print(f"oneshot: WARNING — task {i + 1} use-case {usecase!r} is not a known "
                      f"websitewf overlay {sorted(known_usecases)}; the engine will fall back "
                      f"to the DEFAULT feature-addition overlay (no foundation scaffold).",
                      file=sys.stderr)
        else:
            os.environ.pop("WEBSITEWF_USECASE", None)

        overlay = usecase if usecase else ("default-overlay" if engine == "websitewf" else "n/a")
        print(f"\noneshot: ── task {i + 1}/{len(tasks)} — {job['title']!r} "
              f"(local #{job['issue']}, source={request.get('source') or 'prompt'}, "
              f"use-case={overlay}) ──")
        summary = mod.run_live(job, triage, dry_run=not args.live, request=request)
        result = summary["result"]
        deliv = sorted(summary.get("deliverables") or {})
        print(f"oneshot: task {i + 1} complete — ok={result.ok}; deliverables={deliv}")
        if not result.ok:
            failures += 1
            print(f"oneshot: task {i + 1} not ok — detail: {getattr(result, 'detail', None)} "
                  f"error: {getattr(result, 'error', None)}", file=sys.stderr)

    print(f"\noneshot: done — {len(tasks) - failures}/{len(tasks)} task(s) ok.")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
