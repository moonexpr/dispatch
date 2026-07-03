#!/usr/bin/env python3
"""purpose.py — classify an issue's PURPOSE (drives the engineering harness).

Purpose ∈ {prototype, refactor, new-feature, research, test, …} answers *what
kind of work this is*, and through ``workplan-rules.yml``'s ``purpose.harness``
map it selects the engineering harness the Engineer runs under (informational —
surfaced in the work plan's HARNESS section).

Resolution (data-driven, ``workplan_config.load()``):
  1. LABEL-first — an issue label matching ``purpose.labels`` wins (explicit
     operator intent beats inference).
  2. KEYWORD fallback — the first ``purpose.keyword_rules`` entry with any token
     in the lowercased "title\\nbody" wins.
  3. DEFAULT — ``purpose.default`` (new-feature).

Pure and deterministic: same (job, labels) in -> same record out. No network,
no model, no wall-clock.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import workplan_config  # noqa: E402


def _harness_for(rules: Dict[str, Any], purpose: str) -> Dict[str, str]:
    hmap = (rules.get("harness") or {})
    h = hmap.get(purpose) or {}
    return {"harness": str(h.get("harness", "standard")),
            "harness_ref": str(h.get("ref", "")),
            "harness_setup": str(h.get("setup", "")),
            "harness_rationale": str(h.get("rationale", ""))}


def classify_purpose(job: Dict[str, Any],
                     labels: Optional[List[str]] = None) -> Dict[str, str]:
    """Return {purpose, source, harness, harness_rationale}.

    ``source`` is "label" | "keyword" | "default" — provenance for traceability.
    """
    rules = (workplan_config.load().get("purpose", {}) or {})
    lbls = [str(l).strip().lower() for l in (labels or []) if str(l).strip()]

    # 1. label-first
    label_map = {str(k).lower(): str(v) for k, v in (rules.get("labels") or {}).items()}
    for lbl in lbls:
        if lbl in label_map:
            purpose = label_map[lbl]
            return {"purpose": purpose, "source": "label", **_harness_for(rules, purpose)}

    # 2. keyword fallback
    text = ((job.get("title") or "") + "\n" + (job.get("body") or "")).lower()
    for rule in (rules.get("keyword_rules") or []):
        tokens = [str(t).lower() for t in (rule.get("any") or [])]
        if any(tok in text for tok in tokens):
            purpose = str(rule.get("purpose") or rules.get("default") or "new-feature")
            return {"purpose": purpose, "source": "keyword", **_harness_for(rules, purpose)}

    # 3. default
    purpose = str(rules.get("default") or "new-feature")
    return {"purpose": purpose, "source": "default", **_harness_for(rules, purpose)}


def main(argv: List[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Classify an issue's work purpose.")
    p.add_argument("--title", default="")
    p.add_argument("--body", default="")
    p.add_argument("--labels", default="", help="comma-separated issue labels")
    p.add_argument("--issue-json", default=None,
                   help="JSON object with title/body/labels (overrides flags)")
    args = p.parse_args(argv)
    if args.issue_json:
        with open(args.issue_json, encoding="utf-8") as fh:
            obj = json.load(fh)
        job = {"title": obj.get("title", ""), "body": obj.get("body", "")}
        labels = [(l.get("name") if isinstance(l, dict) else l)
                  for l in (obj.get("labels") or [])]
    else:
        job = {"title": args.title, "body": args.body}
        labels = [s for s in args.labels.split(",") if s]
    print(json.dumps(classify_purpose(job, labels), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
