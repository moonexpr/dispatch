#!/usr/bin/env python3
"""mutate.py — deterministic, offline overlay-patch resolver for demo fixtures.

Takes a base snapshot (E8-1, scripts/demo/snapshots/<slug>.json) plus an
overlay scenario file and emits a fully-resolved issue-list fixture to stdout
(or --out FILE). This is how a tester crafts a test scenario OFFLINE, without
ever touching GitHub.

Overlay schema (object form)
----------------------------
  {
    "base": "snapshots/demo-current.json",   # relative to scripts/demo/; --base overrides
    "ops": [
      {"op": "set-body",     "number": 103, "body": ""},
      {"op": "remove-issue", "number": 105}
    ]
  }

Ops (keyed by issue `number`)
  set-labels    {number, labels:[...]}   replace the issue's labels
  clear-labels  {number}                 empty the issue's labels
  set-body      {number, body}           replace the body
  append-body   {number, body}           append to the body
  set-title     {number, title}          replace the title
  add-issue     {issue:{...}}            append a full issue object
  remove-issue  {number}                 drop the issue

Full-replacement escape hatch
-----------------------------
If the scenario's top level is a JSON ARRAY (not an object with `ops`), it is
materialized literally (covers hand-authored queues and the empty-base case).

Determinism
-----------
Output is sorted by issue `number` with stable (sorted) key ordering and NO
wall-clock, so base+overlay is a pure function -> byte-identical across runs
(the property the §7.18 idempotency assertion proves). The base snapshot file
on disk is NEVER mutated, so reset is free.

Labels are emitted as gh's {name:...} object shape (a drop-in for both
`./dispatch --fixture` and intake.py, which reads lbl['name']);
set-labels/clear-labels write objects. An overlay may use the convenient
["a","b"] string form — it is coerced to objects on output.

Exit codes: 0 success; 1 runtime error; 2 bad args.

SECURITY NOTE
-------------
This resolver is 100% offline: no network, no subprocess, no eval/exec. Issue
text from the snapshot/overlay is treated strictly as DATA — never evaled,
shell-expanded, or interpreted (matches services/intake/intake.py and the
CLAUDE.md untrusted-input posture). Files are loaded via open(), not shell
expansion. Zero exec capability is enforced by the §8 AST check in smoke.sh.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))   # scripts/demo/


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _normalize_labels(labels: Any) -> List[Dict[str, str]]:
    """Coerce a labels value to gh's [{name:...}] object shape.

    Accepts either ["a","b"] (flattened) or [{"name":"a"}] (gh shape) so a
    hand-authored overlay may use the convenient string form; output is always
    objects, keeping the result a drop-in for intake.py.
    """
    out: List[Dict[str, str]] = []
    for lbl in labels or []:
        name = lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
        out.append({"name": name})
    return out


def _require_number(op: Dict[str, Any], i: int) -> int:
    if "number" not in op:
        raise ValueError(f"op[{i}] {op.get('op')!r} requires a `number`")
    return int(op["number"])


def _sorted(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(issues, key=lambda it: int(it["number"]))


def resolve(base_issues: List[Dict[str, Any]], scenario: Any) -> List[Dict[str, Any]]:
    """Apply `scenario` to `base_issues`, returning a fresh sorted issue list.

    `base_issues` is never mutated (every issue is shallow-copied first).
    """
    # Full-replacement escape hatch: a top-level array is materialized literally.
    if isinstance(scenario, list):
        return _sorted([dict(it) for it in scenario])
    if not isinstance(scenario, dict):
        raise ValueError("scenario must be a JSON object {ops:[...]} or a JSON array")

    by_num: Dict[int, Dict[str, Any]] = {}
    for it in base_issues:
        by_num[int(it["number"])] = dict(it)   # copy; never mutate the input

    ops = scenario.get("ops", [])
    if not isinstance(ops, list):
        raise ValueError("scenario.ops must be a list")

    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ValueError(f"op[{i}] must be an object")
        kind = op.get("op")

        if kind == "add-issue":
            issue = op.get("issue")
            if not isinstance(issue, dict) or "number" not in issue:
                raise ValueError(f"op[{i}] add-issue needs an `issue` object with a number")
            issue = dict(issue)
            if "labels" in issue:
                issue["labels"] = _normalize_labels(issue["labels"])
            by_num[int(issue["number"])] = issue
            continue

        if kind == "remove-issue":
            by_num.pop(_require_number(op, i), None)
            continue

        # Field-targeting ops require an existing target issue.
        n = _require_number(op, i)
        if n not in by_num:
            raise ValueError(f"op[{i}] {kind}: issue #{n} is not in the base snapshot")
        target = by_num[n]

        if kind == "set-labels":
            target["labels"] = _normalize_labels(op.get("labels", []))
        elif kind == "clear-labels":
            target["labels"] = []
        elif kind == "set-body":
            target["body"] = str(op.get("body", ""))
        elif kind == "append-body":
            target["body"] = str(target.get("body", "")) + str(op.get("body", ""))
        elif kind == "set-title":
            target["title"] = str(op.get("title", ""))
        else:
            raise ValueError(f"op[{i}] unknown op: {kind!r}")

    return _sorted(list(by_num.values()))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Resolve an overlay scenario against a base snapshot (offline, deterministic).")
    p.add_argument("--scenario", required=True, metavar="FILE",
                   help="overlay scenario JSON (object with ops, or a literal array)")
    p.add_argument("--base", default="", metavar="SNAP",
                   help="base snapshot JSON array (overrides the scenario's `base` key)")
    p.add_argument("--out", default="", metavar="FILE",
                   help="write resolved fixture here instead of stdout")
    return p


def main(argv: List[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        scenario = _load_json(args.scenario)
    except (OSError, ValueError) as exc:
        print(f"mutate: cannot read scenario {args.scenario}: {exc}", file=sys.stderr)
        return 1

    base_issues: List[Dict[str, Any]] = []
    if isinstance(scenario, dict):
        base_path = args.base
        if not base_path and scenario.get("base"):
            base_path = os.path.join(HERE, scenario["base"])
        if not base_path:
            print("mutate: scenario has `ops` but no `base` (and no --base given)", file=sys.stderr)
            return 2
        try:
            base_issues = _load_json(base_path)
        except (OSError, ValueError) as exc:
            print(f"mutate: cannot read base {base_path}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(base_issues, list):
            print(f"mutate: base {base_path} is not a JSON array", file=sys.stderr)
            return 1

    try:
        resolved = resolve(base_issues, scenario)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"mutate: resolve failed: {exc}", file=sys.stderr)
        return 1

    # sort_keys + sorted-by-number + no wall-clock => byte-identical across runs.
    text = json.dumps(resolved, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
