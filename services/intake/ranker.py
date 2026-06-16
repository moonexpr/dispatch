#!/usr/bin/env python3
"""ranker.py — rank intake items by dependency order using an LLM.

Reads a JSON array of IntakeItems (stdin or --input FILE), calls an LLM
(default: haiku) to identify blocking/dependency relationships from issue
body text, and returns the items re-ordered from most foundational to most
dependent.

The LLM reads all issue titles and bodies together so it can resolve
cross-references ("Parent epic: #N", "blocked by #N", "follow-up to #N",
"depends on #N") and return a ranked ordering.

Usage
-----
  python3 services/intake/pipeline.py --repo owner/repo | \\
    python3 services/intake/ranker.py

  python3 services/intake/ranker.py --input items.json --model sonnet

  # Offline (dry-run: prints ranking without reordering):
  python3 services/intake/ranker.py --input items.json --dry-run

Env vars
--------
  RANKER_MODEL   model alias to use (default: haiku)
  MODELS_BACKEND backend override (see services/models/models.py)

Output
------
  JSON array of IntakeItems in ranked order (stdout).
  Ranking rationale printed to stderr.

Exit codes: 0 success; 1 runtime error; 2 bad args.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

# Resolve models from sibling directory regardless of CWD.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../models"))
import models as _models  # noqa: E402

DEFAULT_MODEL = os.environ.get("RANKER_MODEL", "haiku")

SYSTEM_PROMPT = """\
You are a software project manager analyzing a list of GitHub issues.
Your job is to determine which issues are blocked by or depend on other issues,
and return a ranked ordering from most unblocked/foundational to most dependent.

Rules:
- An issue is foundational if nothing in the list needs to be done before it.
- An issue is dependent if its body references other issues as parents, blockers, or prerequisites.
- References to look for: "Parent epic: #N", "blocked by #N", "depends on #N",
  "follow-up to #N", "requires #N", sub-issue relationships.
- Epic issues (those with "[Epic]" in the title, or described as organizational
  containers or trackers for sub-issues) are ranked AFTER all their constituent
  sub-issues. An epic closes only when its sub-issues are complete; it is therefore
  the most dependent issue in its group, not the most foundational.
- If two issues are independent (and neither is an epic), order the lower-numbered
  one first (it was created earlier).
- Return ONLY valid JSON — no prose, no markdown fences.\
"""

USER_PROMPT_TEMPLATE = """\
Here are the GitHub issues to rank. Each has a number, title, and body.

{issue_list}

Return a JSON object with exactly these two keys:
  "ranked": an array of issue numbers ordered from most foundational to most dependent
  "reasoning": one sentence per issue explaining its position (keyed by issue number as a string)

Example shape:
{{
  "ranked": [3, 1, 7, 2],
  "reasoning": {{
    "3": "No dependencies mentioned; sets up the foundation.",
    "1": "Depends on #3 for the base scaffold.",
    "7": "Requires both #3 and #1 to be complete.",
    "2": "Explicitly blocked by #7."
  }}
}}
"""


def _format_issues(items: List[Dict[str, Any]]) -> str:
    parts = []
    for item in items:
        body = (item.get("body") or "").strip()
        body_preview = body[:400] + ("…" if len(body) > 400 else "")
        parts.append(
            f"Issue #{item['number']} — {item['title']}\n"
            f"Body: {body_preview or '(no body)'}"
        )
    return "\n\n".join(parts)


def _extract_json(text: str) -> Dict[str, Any]:
    """Parse LLM output that should be pure JSON, with a fence-stripping fallback."""
    text = text.strip()
    # Strip ```json ... ``` fences if the model added them anyway
    fenced = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM returned non-JSON output: {exc}\n\nRaw output:\n{text[:500]}") from exc


# --------------------------------------------------------------------------
# Offline deterministic ranking (RANKER_OFFLINE=1) — no network, no model.
# Mirrors the CLASSIFIER_OFFLINE seam: identical input always yields identical
# order. Parses dependency cross-references from issue text and orders items
# foundational -> dependent, tie-breaking by lower issue number.
# --------------------------------------------------------------------------
# Forward refs: "this issue depends on #N".
_FWD_DEP_PATTERNS = (
    r"blocked\s+by\s+#(\d+)",
    r"depends?\s+on\s+#(\d+)",
    r"requires?\s+#(\d+)",
    r"needs\s+#(\d+)",
    r"parent\s+epic:?\s*#(\d+)",
    r"follow[-\s]?up\s+to\s+#(\d+)",
    r"prerequisite:?\s*#(\d+)",
    r"after\s+#(\d+)",
)
# Reverse refs: "this issue unblocks/blocks #N" => #N depends on this issue.
_REV_DEP_PATTERNS = (
    r"unblocks?:?\s*(?:issue\s*)?#(\d+)",
    r"blocks?\s+#(\d+)",
    r"prerequisite\s+for\s+#(\d+)",
)


def _refs(text: str, patterns: tuple) -> set:
    out: set = set()
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            out.add(int(m.group(1)))
    return out


def _rank_offline(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    present = {int(i["number"]) for i in items}
    deps: Dict[int, set] = {int(i["number"]): set() for i in items}
    for item in items:
        n = int(item["number"])
        text = f"{item.get('title', '')}\n{item.get('body', '')}"
        for r in _refs(text, _FWD_DEP_PATTERNS):
            if r in present and r != n:
                deps[n].add(r)
        for r in _refs(text, _REV_DEP_PATTERNS):
            if r in present and r != n:
                deps[r].add(n)

    depth_cache: Dict[int, int] = {}

    def depth(n: int, stack: frozenset) -> int:
        if n in depth_cache:
            return depth_cache[n]
        if n in stack:                       # cycle guard
            return 0
        d = 0
        for p in deps[n]:
            d = max(d, 1 + depth(p, stack | {n}))
        depth_cache[n] = d
        return d

    ordered = sorted(items, key=lambda i: (depth(int(i["number"]), frozenset()),
                                           int(i["number"])))

    print("ranker: offline deterministic dependency ranking", file=sys.stderr)
    for i in ordered:
        n = int(i["number"])
        ds = sorted(deps[n])
        note = "foundational (no deps)" if not ds else \
            "depends on " + ", ".join(f"#{x}" for x in ds)
        print(f"  #{n}: depth={depth(n, frozenset())} — {note}", file=sys.stderr)
    return ordered


def rank(
    items: List[Dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
) -> List[Dict[str, Any]]:
    """Return items re-ordered foundational -> dependent.

    With RANKER_OFFLINE set, uses a deterministic, network-free dependency
    parse. Otherwise asks the LLM (default: haiku) to resolve the ordering.
    """
    if len(items) <= 1:
        return items

    if os.environ.get("RANKER_OFFLINE"):
        return _rank_offline(items)

    issue_list = _format_issues(items)
    prompt = USER_PROMPT_TEMPLATE.format(issue_list=issue_list)

    raw = _models.complete(model, prompt, system=SYSTEM_PROMPT, max_tokens=2048)
    result = _extract_json(raw)

    ranked_numbers = result.get("ranked", [])
    reasoning = result.get("reasoning", {})

    # Log reasoning to stderr
    print(f"ranker: model={model} ranked {len(ranked_numbers)} item(s)", file=sys.stderr)
    for num in ranked_numbers:
        note = reasoning.get(str(num), "")
        print(f"  #{num}: {note}", file=sys.stderr)

    # Re-order items to match ranked order; append any the LLM missed at the end
    by_number = {item["number"]: item for item in items}
    ordered = [by_number[n] for n in ranked_numbers if n in by_number]
    seen = {item["number"] for item in ordered}
    ordered += [item for item in items if item["number"] not in seen]

    return ordered


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="Rank intake items by dependency order.")
    p.add_argument("--input", metavar="FILE",
                   help="JSON array of IntakeItems (default: stdin)")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"model alias (default: {DEFAULT_MODEL})")
    p.add_argument("--offline", action="store_true",
                   help="deterministic dependency parse; no network/model (sets RANKER_OFFLINE)")
    p.add_argument("--dry-run", action="store_true",
                   help="print ranking reasoning only; do not reorder or emit JSON")
    p.add_argument("--output", metavar="FILE",
                   help="write ranked JSON to FILE instead of stdout")
    args = p.parse_args(argv)

    if args.offline:
        os.environ["RANKER_OFFLINE"] = "1"

    # Load items
    try:
        if args.input:
            with open(args.input, encoding="utf-8") as fh:
                items = json.load(fh)
        else:
            items = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ranker: failed to load input: {exc}", file=sys.stderr)
        return 2

    if not isinstance(items, list):
        print("ranker: input must be a JSON array", file=sys.stderr)
        return 2

    print(f"ranker: {len(items)} item(s) to rank", file=sys.stderr)

    try:
        ranked = rank(items, model=args.model)
    except RuntimeError as exc:
        print(f"ranker: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("ranker: dry-run; ranked order (numbers only):",
              [i["number"] for i in ranked], file=sys.stderr)
        return 0

    output = json.dumps(ranked, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output)
            fh.write("\n")
        print(f"ranker: wrote {len(ranked)} item(s) to {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
