#!/usr/bin/env python3
"""research.py — ARCHITECT gap-detection heuristic (E6-1, Pillar 2, decision D3).

Decide, per job, whether the deterministic offline architect has enough grounded
information to plan well — or whether the job should be routed to a *research*
unit of work instead of guessing. Per D3 the architect NEVER does live research;
it only chooses the mode. This module computes the verdict; E6-2 consumes it to
emit the research work order, E6-3 wires the research->impl DAG edge.

Pure and offline: no network, no subprocess, no wall-clock. Identical
(job, discovered, tuning) always yields the identical verdict — the same
determinism contract as resources.py / decompose.py / classify.py.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tuning  # noqa: E402

# Enumerated reasons (fixed set). Downstream rendering and smoke assertions key
# off these exact strings — do not reword without updating both.
REASON_LABEL = "label"
REASON_NO_FILES = "no-matching-files"
REASON_LOW_CONF = "low-confidence"
REASON_NONE = "none"

# "Grounding tokens": text that *looks like* it points at concrete code — a file
# with a code extension or an explicit function call. If a body carries such
# tokens but resources.discover() matched ZERO real repo files, the job is
# referencing things that do not exist here, so it needs research. Deliberately
# NARROW (code-ext filenames + func() calls only) to avoid prose false positives
# like "and/or" or "e.g." — a vague body with no concrete tokens falls through to
# the low-confidence signal instead.
_CODE_EXT = ("py", "sh", "js", "jsx", "ts", "tsx", "json", "md", "yaml", "yml",
             "toml", "cfg", "ini", "txt", "html", "css", "scss", "sql", "go", "rs", "rb")
_FILE_RE = re.compile(r"\b[\w./-]+\.(?:" + "|".join(_CODE_EXT) + r")\b")
_FUNC_RE = re.compile(r"\b[a-z_][a-z0-9_]+\(\)")


def _has_grounding_tokens(text: str) -> bool:
    """True if `text` names a code-extension file or a function call."""
    t = text or ""
    return bool(_FILE_RE.search(t) or _FUNC_RE.search(t))


def _label_names(job: Dict[str, Any]) -> List[str]:
    """Issue labels as plain strings (accepts ["queued"] or [{"name": "queued"}])."""
    out: List[str] = []
    for lb in (job.get("labels") or []):
        name = lb.get("name") if isinstance(lb, dict) else lb
        if name:
            out.append(str(name))
    return out


def _slugify(s: str, maxlen: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "untitled"


def topic_for(job: Dict[str, Any]) -> str:
    """Deterministic slug for the docs/research/<topic>.md path E6-2 will use."""
    num = job.get("number", job.get("issue", "x"))
    return f"issue-{num}-{_slugify(job.get('title', ''))}"


def detect_gap(job: Dict[str, Any], discovered: List[str], *,
               tuning_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return {"needs_research": bool, "reason": str, "topic": str}.

    `job`        : the triage/job dict (number, title, body, labels, confidence).
    `discovered` : resources.discover(body, repo_root) — repo files the body
                   references that actually exist (the caller computes it so this
                   stays pure/testable).
    `tuning_cfg` : the generation.research config; defaults to tuning.RESEARCH.

    needs_research is True when ANY signal fires, in this precedence:
      1. label            — the issue carries the configured needs-research label
      2. no-matching-files — body names file/API-like tokens but discovered is empty
      3. low-confidence    — the job's classifier confidence is below the threshold
    With generation.research.enabled false the verdict is always False (kill-switch).
    """
    cfg = tuning_cfg if tuning_cfg is not None else tuning.RESEARCH
    topic = topic_for(job)

    if not cfg.get("enabled", True):
        return {"needs_research": False, "reason": REASON_NONE, "topic": topic}

    # (1) explicit operator/architect label.
    label = cfg.get("needs_research_label", "needs-research")
    if label in _label_names(job):
        return {"needs_research": True, "reason": REASON_LABEL, "topic": topic}

    # (2) references concrete code that does not exist in this repo.
    if _has_grounding_tokens(job.get("body", "")) and not discovered:
        return {"needs_research": True, "reason": REASON_NO_FILES, "topic": topic}

    # (3) the classifier was not confident enough to plan robustly.
    threshold = float(cfg.get("low_confidence_threshold", 0.55))
    conf = job.get("confidence")
    if conf is not None and float(conf) < threshold:
        return {"needs_research": True, "reason": REASON_LOW_CONF, "topic": topic}

    return {"needs_research": False, "reason": REASON_NONE, "topic": topic}
