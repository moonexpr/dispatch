"""kernel.adhoc_session — the concrete ADHOC runtime: run one dispatch tick, then terminate.

Implements :class:`foundation.kernel.session.Session` for the run-and-exit lifecycle. It
fetches the GitHub issue named by the :class:`BootSpec`, builds the engine ``job`` +
``triage``, selects the engine (``baseworkflow`` default | ``websitewf`` overlay), runs
its ``spec -> work -> build`` lifecycle, and returns a process exit code. The engine's
**Admin** phase owns the GitHub lifecycle (claim / PR / closure), gated by
``PIPELINE_DRY_RUN`` (dry-run unless the request is ``live``).

This is the body that used to live inline in the ``./dispatch`` entrypoint; the entrypoint
is now just bootstrap + routing.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

from foundation.kernel.session import BootSpec, Session, SessionKind

# Engine id -> dotted module exposing ``run_live(job, triage, dry_run=...)``.
_ENGINES: Dict[str, str] = {
    "baseworkflow": "baseworkflow.baseworkflow",
    "websitewf": "websitewf.websitewf",
}


def _gh_json(args: List[str]) -> Any:
    """Run ``gh ... --json`` and parse the result (read-only; always allowed)."""
    out = subprocess.check_output(["gh", *args], text=True)
    return json.loads(out)


def _fetch_job(repo: str, issue: Optional[int]) -> Dict[str, Any]:
    """Build the engine ``job`` from a GitHub issue (the first open one if unset). Issue
    text is untrusted input — carried as data, never executed (HANDOFF §8)."""
    if issue is None:
        listing = _gh_json(["issue", "list", "-R", repo, "--state", "open", "--limit", "1", "--json", "number"])
        if not listing:
            raise SystemExit(f"dispatch: no open issues on {repo}")
        issue = listing[0]["number"]
    d = _gh_json(["issue", "view", str(issue), "-R", repo, "--json", "number,title,body,labels"])
    job: Dict[str, Any] = {
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


def _debug_dump(summary: Dict[str, Any], job: Dict[str, Any], triage: Dict[str, Any]) -> None:
    """Print a tick's internals: the architect's decomposition (units → agents), the
    per-action execution trace (the multi-agent fan-out), the first failing action, and
    the engineering result. Enabled by ``-v/--verbose`` or ``DISPATCH_DEBUG``."""
    deliv = summary.get("deliverables") or {}
    ctx = summary.get("ctx")
    print("--- debug: job ---")
    print(f"  {{issue:{job.get('issue')}, title:{job.get('title')!r}, "
          f"route:{job.get('route')}, framework:{job.get('framework')}}}")
    print(f"  triage: {triage}")

    plan = deliv.get("plan") or {}
    units = plan.get("units") or []
    print(f"--- debug: decomposition — {len(units)} engineering unit(s)/agent(s) ---")
    for u in units:
        uid = u.get("id") if isinstance(u, dict) else u
        title = u.get("title", "") if isinstance(u, dict) else ""
        print(f"    • {uid}  {title}")
    osc = deliv.get("orchestration_script")
    phases = osc.get("phases") if isinstance(osc, dict) else None
    if phases is not None:
        print(f"    orchestration_script: {len(phases)} phase(s)")

    trace = getattr(ctx, "trace", None) or []
    print(f"--- debug: action trace ({len(trace)} actions) ---")
    first_fail = None
    for t in trace:
        mark = "ok " if t.get("ok") else "FAIL"
        if not t.get("ok") and first_fail is None:
            first_fail = t
        print(f"    [{mark}] {'  ' * int(t.get('depth', 0))}{t.get('kind')}:{t.get('name')}")
    if first_fail is not None:
        print(f"--- debug: FIRST FAILURE → {first_fail.get('kind')}:{first_fail.get('name')} "
              f"(depth {first_fail.get('depth')}) ---")
    print(f"--- debug: engineering_result ---\n  {str(deliv.get('engineering_result'))[:400]}")


class AdhocSession(Session):
    """Run one dispatch tick for the request, then terminate. The default runtime."""

    kind = SessionKind.ADHOC

    def run(self) -> int:
        spec: BootSpec = self.spec
        os.environ["PIPELINE_REPO"] = spec.repo
        os.environ["PIPELINE_DRY_RUN"] = "0" if spec.live else "1"
        if spec.verbose:
            # Stream the engine's per-action trace (foundation/actions/action.py) so a
            # long or hanging tick shows live progress, not just a post-run dump.
            os.environ["DISPATCH_DEBUG"] = "1"

        engine = spec.engine or "baseworkflow"
        if engine not in _ENGINES:
            print(f"dispatch: engine {engine!r} unknown ({sorted(_ENGINES)}); using baseworkflow",
                  file=sys.stderr)
            engine = "baseworkflow"
        mod = importlib.import_module(_ENGINES[engine])

        job = _fetch_job(spec.repo, spec.issue)
        # Triage: only implement-class issues reach a tick; classification is a router
        # follow-up (the legacy classifier lived in the retired visitor lineage).
        triage = {"action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.9}

        # The kernel initializes the SEED controller (ADR-003) via the request it
        # boots. DISPATCH_JOB_REQUEST=<json file> injects a non-interactive job
        # request (accepted or rejected on request satisfaction); the fetched
        # issue is the default GitHub-source request; the engines synthesize a
        # legacy job request when None.
        request = None
        job_file = os.environ.get("DISPATCH_JOB_REQUEST", "")
        if job_file:
            with open(job_file, encoding="utf-8") as fh:
                request = {"source": "job", "job": json.load(fh)}
        elif spec.issue is not None:
            request = {"source": "github", "repo": spec.repo, "issue": spec.issue}

        mode = "LIVE" if spec.live else "dry-run"
        print(f"dispatch: engine={engine} repo={spec.repo} issue=#{job['issue']} mode={mode}")
        summary = mod.run_live(job, triage, dry_run=not spec.live, request=request)

        result = summary["result"]
        deliv = summary.get("deliverables") or {}
        if spec.verbose:
            _debug_dump(summary, job, triage)
        print(f"dispatch: tick complete — ok={result.ok}; deliverables={sorted(deliv)}")
        if not result.ok:
            print(f"dispatch: result not ok — detail: {getattr(result, 'detail', None)} "
                  f"error: {getattr(result, 'error', None)}", file=sys.stderr)
        return 0 if result.ok else 1
