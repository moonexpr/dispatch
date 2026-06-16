#!/usr/bin/env python3
"""intake.py — GitHub issue intake for the dispatch pipeline.

Fetches issues from a GitHub ProjectV2 board or a repository and emits a
normalized JSON array to stdout (or --output FILE).

Two modes
---------
  --project ORG/NUM    query a GitHub ProjectV2 (gh project item-list)
  --repo OWNER/REPO    query a GitHub repository (gh issue list)

Env-var equivalents (pipeline.env)
-----------------------------------
  INTAKE_PROJECT         ORG/NUM, e.g. ReclaimByDesign/5
  INTAKE_REPO            OWNER/REPO  (falls back to PIPELINE_REPO when unset)
  INTAKE_STATUS_FILTER   regex applied to the project Status field (case-insensitive)
  INTAKE_LABEL_FILTER    label name to filter repo issues by
  INTAKE_LIMIT           max items to fetch (default: 50)
  INTAKE_FIXTURE_PROJECT path to raw gh project item-list JSON (offline / tests)
  INTAKE_FIXTURE_REPO    path to raw gh issue list JSON (offline / tests)
  GH_BIN                 gh binary (default: gh)

Output schema (JSON array elements)
------------------------------------
  number          int      issue number within its repo
  title           str
  body            str
  labels          list[str]
  repository      str      "owner/repo" slug
  assignees       list[str]
  project_status  str|None project Status field value (None in repo mode)
  dispatch        str|None project Dispatch field value (None if absent)
  hours_estimate  int|None project Hours Estimate field (None if absent)
  source_url      str      full GitHub URL to the issue

Usage
-----
  python3 intake.py --project ReclaimByDesign/5
  python3 intake.py --project ReclaimByDesign/5 --status "Triage"
  python3 intake.py --repo owner/repo --label queued --output issues.json
  INTAKE_FIXTURE_PROJECT=fixtures/project-items-raw.json python3 intake.py --project ReclaimByDesign/5

Exit codes: 0 success; 1 runtime error; 2 bad args.

SECURITY NOTE
-------------
This service calls `gh` via subprocess to read GitHub data. Issue text is
treated as data — it is never interpreted, evaled, or passed to a shell.
Fixture paths are loaded via open(), not shell expansion.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@dataclass
class IntakeItem:
    number: int
    title: str
    body: str
    labels: List[str]
    repository: str
    assignees: List[str]
    project_status: Optional[str]
    dispatch: Optional[str]
    hours_estimate: Optional[int]
    source_url: str


# ---------------------------------------------------------------------------
# gh subprocess helper
# ---------------------------------------------------------------------------

def _gh(*args: str, gh_bin: str = "gh") -> Any:
    """Run `gh <args>` and return parsed JSON. Raises RuntimeError on failure."""
    cmd = [gh_bin, *args]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"gh command failed ({exc.returncode}): {' '.join(cmd)}\n{exc.stderr.strip()}"
        ) from exc
    return json.loads(result.stdout)


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Project mode
# ---------------------------------------------------------------------------

def _repo_slug(raw: str) -> str:
    """Strip https://github.com/ prefix if present."""
    return raw.removeprefix("https://github.com/")


def intake_from_project(
    org: str,
    num: str,
    *,
    status_filter: str = "",
    limit: int = 50,
    fixture_path: str = "",
    gh_bin: str = "gh",
) -> List[IntakeItem]:
    if fixture_path:
        raw = _load_json(fixture_path)
    else:
        raw = _gh(
            "project", "item-list", num,
            "--owner", org,
            "--format", "json",
            "--limit", str(limit),
            gh_bin=gh_bin,
        )

    items: List[IntakeItem] = []
    for item in raw.get("items", []):
        content = item.get("content", {})
        if content.get("type") != "Issue":
            continue

        status = item.get("status") or None
        if status_filter and not re.search(status_filter, status or "", re.IGNORECASE):
            continue

        hours_raw = item.get("hours Estimate")
        items.append(IntakeItem(
            number=content.get("number", 0),
            title=content.get("title") or item.get("title") or "",
            body=content.get("body") or "",
            labels=[],
            repository=_repo_slug(content.get("repository") or ""),
            assignees=list(item.get("assignees") or []),
            project_status=status,
            dispatch=item.get("dispatch") or None,
            hours_estimate=int(hours_raw) if hours_raw is not None else None,
            source_url=content.get("url") or "",
        ))

    return items


# ---------------------------------------------------------------------------
# Repo mode
# ---------------------------------------------------------------------------

def intake_from_repo(
    repo: str,
    *,
    label_filter: str = "",
    limit: int = 50,
    fixture_path: str = "",
    gh_bin: str = "gh",
) -> List[IntakeItem]:
    if fixture_path:
        raw = _load_json(fixture_path)
    else:
        cmd_args = [
            "issue", "list",
            "--state", "open",
            "--json", "number,title,body,labels,assignees,url",
            "--limit", str(limit),
        ]
        if repo:
            cmd_args += ["--repo", repo]
        if label_filter:
            cmd_args += ["--label", label_filter]
        raw = _gh(*cmd_args, gh_bin=gh_bin)

    items: List[IntakeItem] = []
    for issue in raw:
        items.append(IntakeItem(
            number=issue["number"],
            title=issue.get("title") or "",
            body=issue.get("body") or "",
            labels=[lbl["name"] for lbl in issue.get("labels") or []],
            repository=repo,
            assignees=[a["login"] for a in issue.get("assignees") or []],
            project_status=None,
            dispatch=None,
            hours_estimate=None,
            source_url=issue.get("url") or "",
        ))

    return items


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fetch GitHub issues and emit normalized JSON for the dispatch pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--project", metavar="ORG/NUM",
                     help="GitHub ProjectV2 to query (e.g. ReclaimByDesign/5)")
    src.add_argument("--repo", metavar="OWNER/REPO",
                     help="GitHub repository to query for open issues")
    p.add_argument("--status", metavar="REGEX",
                   default=os.environ.get("INTAKE_STATUS_FILTER", ""),
                   help="filter project items by Status field (case-insensitive regex)")
    p.add_argument("--label", metavar="LABEL",
                   default=os.environ.get("INTAKE_LABEL_FILTER", ""),
                   help="filter repo issues by label")
    p.add_argument("--limit", type=int,
                   default=int(os.environ.get("INTAKE_LIMIT", "50")),
                   help="max items to fetch (default: 50)")
    p.add_argument("--output", metavar="FILE",
                   help="write JSON array to FILE instead of stdout")
    return p


def main(argv: List[str]) -> int:
    p = _build_parser()
    args = p.parse_args(argv)

    gh_bin = os.environ.get("GH_BIN", "gh")

    # Resolve mode: CLI flags > env vars > PIPELINE_REPO fallback.
    project_str = args.project or os.environ.get("INTAKE_PROJECT", "")
    repo_str = args.repo or os.environ.get("INTAKE_REPO", "") or os.environ.get("PIPELINE_REPO", "")

    if not project_str and not repo_str:
        p.error("specify --project ORG/NUM or --repo OWNER/REPO "
                "(or set INTAKE_PROJECT / INTAKE_REPO)")

    try:
        if project_str:
            parts = project_str.split("/", 1)
            if len(parts) != 2 or not all(parts):
                p.error(f"--project must be ORG/NUM, got: {project_str!r}")
            org, num = parts
            items = intake_from_project(
                org, num,
                status_filter=args.status,
                limit=args.limit,
                fixture_path=os.environ.get("INTAKE_FIXTURE_PROJECT", ""),
                gh_bin=gh_bin,
            )
        else:
            items = intake_from_repo(
                repo_str,
                label_filter=args.label,
                limit=args.limit,
                fixture_path=os.environ.get("INTAKE_FIXTURE_REPO", ""),
                gh_bin=gh_bin,
            )
    except RuntimeError as exc:
        print(f"intake: {exc}", file=sys.stderr)
        return 1

    output = json.dumps([asdict(i) for i in items], ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(output)
            fh.write("\n")
        print(f"intake: wrote {len(items)} item(s) to {args.output}", file=sys.stderr)
    else:
        print(output)
        print(f"intake: emitted {len(items)} item(s)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
