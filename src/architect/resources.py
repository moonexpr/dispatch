#!/usr/bin/env python3
"""resources.py — ARCHITECT resource gathering for self-contained work orders.

Operator requirement: a work plan must embed every resource the Engineer would
otherwise have to read, so engineering needs NO further reading or research.
This module discovers the files a job references and reads them — plus the
repo's worker contract (CLAUDE.md) and the Invoice schema — so workorder.py can
inline them directly.

Deterministic and offline: identical (job, repo state) -> identical output.
Reads only regular files inside repo_root; issue text is used merely to
*discover* candidate paths, and every candidate is validated against the real
filesystem before anything is embedded (issue text stays untrusted data).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tuning  # noqa: E402

# Explicit repo-relative paths, e.g. scripts/lib/common.sh, src/intake/x.py
_PATH_RE = re.compile(r'(?:scripts|src|schemas|\.github|docs)/[\w./-]+\.\w+')
# Bare filenames with a known extension, e.g. dispatch.sh, models.py
_FILE_RE = re.compile(r'\b[\w-]+\.(?:sh|py|json|md|ya?ml)\b')
# Referenced function names, e.g. load_queued_issues()
_FUNC_RE = re.compile(r'\b([a-z_][a-z0-9_]+)\(\)')

# Embedding caps — tunable via app/config/tuning.yml (generation.resources).
_MAX_FILES = tuning.RES_CAPS["max_files"]   # cap embedded referenced files (logged when exceeded)
_CAP_BYTES = tuning.RES_CAPS["cap_bytes"]   # cap bytes per embedded file (logged when truncated)


def _safe_isfile(repo_root: str, rel: str):
    p = os.path.normpath(os.path.join(repo_root, rel))
    if not p.startswith(os.path.abspath(repo_root) + os.sep):
        return None                          # path traversal guard
    return p if os.path.isfile(p) else None


def has_file(repo_root: str, rel: str) -> bool:
    """True iff repo-relative `rel` is a regular file inside `repo_root` (the same
    traversal guard `discover`/`gather` use). Callers use this to decide whether a
    committed `docs/research/<topic>.md` exists without re-implementing the guard.
    `repo_root` is absolutized first so the guard works with a relative root too."""
    return _safe_isfile(os.path.abspath(repo_root), rel) is not None


def _resolve_basename(repo_root: str, name: str):
    """Find a bare filename under common dirs. Deterministic (sorted walk)."""
    for base in ("scripts", "src", "schemas", ".github", "."):
        root = os.path.join(repo_root, base)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            filenames.sort()
            if name in filenames:
                return os.path.join(dirpath, name)
    return None


def discover(text: str, repo_root: str) -> List[str]:
    """Return repo-relative paths referenced by `text` that actually exist."""
    repo_root = os.path.abspath(repo_root)
    found: List[str] = []
    seen = set()

    def add(abspath):
        rel = os.path.relpath(abspath, repo_root)
        if rel not in seen:
            seen.add(rel)
            found.append(rel)

    for m in _PATH_RE.findall(text):
        p = _safe_isfile(repo_root, m)
        if p:
            add(p)
    for name in _FILE_RE.findall(text):
        if any(os.path.basename(r) == name for r in found):
            continue
        p = _resolve_basename(repo_root, name)
        if p:
            add(p)

    found.sort()
    return found


def functions(text: str) -> List[str]:
    return sorted(set(_FUNC_RE.findall(text)))


def _cap(data: str, cap: int):
    if len(data) > cap:
        return data[:cap].rstrip() + "\n… (truncated)\n", True
    return data, False


def _read_capped(path: str, cap: int = _CAP_BYTES):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return _cap(fh.read(), cap)


# Sections of the worker contract to embed; everything else (intro pointer,
# solo-dev operator mode, ruflo execution-layer detail) is dropped as N/A to an
# autonomous worker.
_CONTRACT_KEEP = ("the contract", "security posture", "working agreement")


def _filter_contract(text: str) -> str:
    """Embed only the worker-relevant sections of CLAUDE.md: the H1 title plus the
    contract / security-posture / working-agreement sections. If none of those
    headings are found, return the text unchanged so a differently-structured
    contract still embeds."""
    lines = text.splitlines()
    title = next((ln for ln in lines
                  if ln.startswith("# ") and not ln.startswith("## ")), "")
    kept: List[str] = []
    keep = False
    for ln in lines:
        if ln.startswith("## "):
            keep = any(k in ln[3:].strip().lower() for k in _CONTRACT_KEEP)
            if keep:
                kept.append(ln)
            continue
        if keep:
            kept.append(ln)
    if not kept:
        return text
    body = "\n".join(kept).strip()
    return f"{title}\n\n{body}" if title else body


def gather(job: Dict[str, Any], repo_root: str,
           research_rel: str = None) -> Dict[str, Any]:
    """Collect everything the work order should embed for `job`.

    `research_rel` (E6-3 / #56): the repo-relative `docs/research/<topic>.md`
    artifact for this job's topic. When it names a committed file, it is embedded
    as the LEADING resource (a research-backed order leads with its grounding) even
    though the issue body may not reference it by path. It counts against the same
    `max_files` / `cap_bytes` budget + traversal guard as any other embedded file,
    and is reported in `research_file` for callers/renderers. `discovered` (which
    feeds decomposition and gap-detection) is left untouched — body-referenced
    files only — so embedding the research artifact never perturbs those signals.
    """
    repo_root = os.path.abspath(repo_root)
    text = f"{job.get('title', '')}\n{job.get('body', '')}"

    discovered = discover(text, repo_root)
    truncations: List[str] = []

    # E6-3 (#56): committed research artifact leads the embed set when present.
    research_file = None
    embed_paths = list(discovered)
    if research_rel and _safe_isfile(repo_root, research_rel) and research_rel not in embed_paths:
        embed_paths = [research_rel] + embed_paths
        research_file = research_rel

    files = []
    for rel in embed_paths[:_MAX_FILES]:
        content, trunc = _read_capped(os.path.join(repo_root, rel))
        if trunc:
            truncations.append(rel)
        files.append({"path": rel, "content": content, "truncated": trunc})
    dropped = embed_paths[_MAX_FILES:]

    contract = None
    cpath = os.path.join(repo_root, "CLAUDE.md")
    if os.path.isfile(cpath):
        with open(cpath, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        contract, ctr = _cap(_filter_contract(raw), tuning.RES_CAPS["contract_cap"])
        if ctr:
            truncations.append("CLAUDE.md")

    schema = None
    spath = os.path.join(repo_root, "schemas", "invoice.json")
    if os.path.isfile(spath):
        schema, _ = _read_capped(spath, cap=tuning.RES_CAPS["schema_cap"])

    return {
        "discovered": discovered,
        "files": files,
        "dropped": dropped,
        "truncations": truncations,
        "functions": functions(text),
        "contract": contract,
        "invoice_schema": schema,
        "research_file": research_file,   # E6-3 (#56): embedded research path or None
    }
