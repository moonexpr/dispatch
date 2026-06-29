#!/usr/bin/env python3
"""verify.py — resolve the verify gate command for a work order.

The gate is the command the Engineer must make green before a PR is ready, and it
is repo-specific: the dispatch repo's ``bash scripts/smoke.sh`` is meaningless in a
Next.js target. This resolver derives a sensible gate from the *target* repo's
tooling, with an explicit override taking precedence and a neutral instruction as
the fallback when nothing is detected (better than asserting a script that does
not exist).

Resolution order
----------------
  1. override (``--verify-cmd`` / ``DISPATCH_VERIFY_CMD``) — used verbatim.
  2. detection from the target's signal files:
       ``scripts/smoke.sh``    -> ``bash scripts/smoke.sh`` (explicit project gate)
       ``package.json`` scripts -> ``npm test`` / ``npm run build`` / ``npm run lint``
       ``Makefile`` ``test:`` target -> ``make test``
     Live targets (``--repo`` / ``--project``) are probed via ``gh api``; the
     offline fixture/self path reads ``repo_root`` from disk.
  3. generic fallback (``GENERIC``).

Deterministic given the same repo contents. Results are cached per repo so an
``--all`` run probes each target at most once. The ``scripts/smoke.sh`` check is
first so the dispatch repo (whose canonical gate is smoke.sh) keeps its existing
behavior regardless of any other tooling present.
"""
from __future__ import annotations

import base64
import json
import os
from typing import Dict, Optional

from engine import proc  # capturing subprocess wrapper (ProcError on launch/non-zero)

GENERIC = "run the repository's test/build gate and make it green"


def gate_ref(verify_cmd: str) -> str:
    """Inline reference to the gate for prose and checklists: a backticked command
    when one was detected, or a bare noun phrase when it is the GENERIC fallback
    (so a work order never reads ```run the ... gate` passes``)."""
    return "the project's test/build gate" if verify_cmd == GENERIC else f"`{verify_cmd}`"


def _from_package_json(text: str) -> Optional[str]:
    try:
        scripts = (json.loads(text) or {}).get("scripts", {}) or {}
    except (ValueError, AttributeError):
        return None
    for name, cmd in (("test", "npm test"), ("build", "npm run build"),
                      ("lint", "npm run lint")):
        if scripts.get(name):
            return cmd
    return None


def _from_makefile(text: str) -> Optional[str]:
    for line in text.splitlines():
        if line.startswith("test:"):
            return "make test"
    return None


def _detect_local(repo_root: str) -> Optional[str]:
    if not repo_root:
        return None
    if os.path.isfile(os.path.join(repo_root, "scripts", "smoke.sh")):
        return "bash scripts/smoke.sh"
    for fname, parse in (("package.json", _from_package_json),
                         ("Makefile", _from_makefile)):
        path = os.path.join(repo_root, fname)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    cmd = parse(fh.read())
            except OSError:
                cmd = None
            if cmd:
                return cmd
    return None


def _gh_contents(repo_slug: str, path: str, gh_bin: str) -> Optional[str]:
    """Decoded text of ``repo_slug:path`` via ``gh api``, or None if absent."""
    out = proc.run_text(
        [gh_bin, "api", f"repos/{repo_slug}/contents/{path}", "--jq", ".content"])
    if not out:
        return None
    try:
        return base64.b64decode(out).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return None


def _detect_live(repo_slug: str, gh_bin: str) -> Optional[str]:
    if _gh_contents(repo_slug, "scripts/smoke.sh", gh_bin) is not None:
        return "bash scripts/smoke.sh"
    pj = _gh_contents(repo_slug, "package.json", gh_bin)
    if pj:
        cmd = _from_package_json(pj)
        if cmd:
            return cmd
    mk = _gh_contents(repo_slug, "Makefile", gh_bin)
    if mk:
        cmd = _from_makefile(mk)
        if cmd:
            return cmd
    return None


def resolve_verify_cmd(*, repo_slug: str = "", repo_root: str = "",
                       override: Optional[str] = None, live: bool = False,
                       gh_bin: str = "gh",
                       cache: Optional[Dict[str, str]] = None) -> str:
    """Resolve the verify gate. Override wins; else detect; else ``GENERIC``.

    ``live`` selects the ``gh api`` probe (for ``--repo`` / ``--project`` targets);
    otherwise detection reads ``repo_root`` from the local filesystem.
    """
    if override:
        return override
    key = (repo_slug or repo_root or "").strip()
    if cache is not None and key in cache:
        return cache[key]
    detected = (_detect_live(repo_slug, gh_bin) if (live and repo_slug)
                else _detect_local(repo_root))
    cmd = detected or GENERIC
    if cache is not None:
        cache[key] = cmd
    return cmd
