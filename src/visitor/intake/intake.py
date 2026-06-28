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
  INTAKE_FIXTURE_CONVERSATION path to a conversation/history fixture: a JSON object
                         keyed by issue number (string), each value
                         {"comments": [...], "history": [...]} (offline / tests)
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
  conversation    list     issue comment thread: [{author, created_at, body}]  (#132)
  history         list     relevant commit/PR slice: [{kind, ref, summary, url}] (#132)

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
    # Priority inputs (selection ordering). Default None/empty so snapshots and
    # fixtures written before these fields existed still load via IntakeItem(**d).
    milestone: Optional[str] = None
    iteration: Optional[str] = None
    # Conversation + relevant history (#132). The comment thread and a commit/PR
    # slice that ground the work order. Default empty so older snapshots/fixtures
    # (written before these fields existed) still load via IntakeItem(**d), and so
    # the cheap list path — which never enriches — round-trips unchanged.
    conversation: List[Dict[str, str]] = field(default_factory=list)
    history: List[Dict[str, str]] = field(default_factory=list)


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


def _field_title(v: Any) -> Optional[str]:
    """Normalize a milestone/iteration value (string or {title}/{name} object)."""
    if not v:
        return None
    if isinstance(v, dict):
        return v.get("title") or v.get("name") or None
    return str(v)


def _label_names(*sources: Any) -> List[str]:
    """Flatten label lists that may hold strings or {name} objects."""
    out: List[str] = []
    for src in sources:
        for lbl in src or []:
            name = lbl.get("name") if isinstance(lbl, dict) else lbl
            if name:
                out.append(name)
    return out


# ---------------------------------------------------------------------------
# Conversation + relevant history (#132)
# ---------------------------------------------------------------------------
# intake also embeds the issue's comment THREAD (triage, repro steps, maintainer
# guidance) and a RELEVANT-HISTORY slice (linked PRs / recent commits) so the
# architect's work order starts grounded. Both are UNTRUSTED data, exactly like
# the body — fetched and stored verbatim, never interpreted. An offline fixture
# seam (INTAKE_FIXTURE_CONVERSATION / fixture_conversation=) keeps smoke + demo
# harnesses from making any live gh call.

def _normalize_comments(raw: Any) -> List[Dict[str, str]]:
    """Flatten a `gh issue view --json comments` list to [{author, created_at, body}]."""
    out: List[Dict[str, str]] = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        author = c.get("author")
        login = author.get("login") if isinstance(author, dict) else author
        out.append({
            "author": str(login or ""),
            "created_at": str(c.get("createdAt") or c.get("created_at") or ""),
            "body": str(c.get("body") or ""),
        })
    return out


def _normalize_history(raw: Any) -> List[Dict[str, str]]:
    """Flatten a relevant-history list to [{kind, ref, summary, url}].

    Accepts pre-shaped fixture rows ({kind, ref, summary, url}) as-is, and the
    raw `gh pr list` shape ({number, title, url}) — mapped to a 'pr' row."""
    out: List[Dict[str, str]] = []
    for h in raw or []:
        if not isinstance(h, dict):
            continue
        if "ref" in h or "kind" in h:
            out.append({
                "kind": str(h.get("kind") or "ref"),
                "ref": str(h.get("ref") or ""),
                "summary": str(h.get("summary") or ""),
                "url": str(h.get("url") or ""),
            })
        elif "number" in h:        # raw gh pr/commit row
            out.append({
                "kind": "pr",
                "ref": f"#{h.get('number')}",
                "summary": str(h.get("title") or ""),
                "url": str(h.get("url") or ""),
            })
    return out


def fetch_conversation(
    repo: str,
    number: int,
    *,
    fixture_path: str = "",
    gh_bin: str = "gh",
) -> Dict[str, List[Dict[str, str]]]:
    """Fetch the issue's comment thread + relevant history for one issue.

    Returns {"conversation": [...], "history": [...]}. With `fixture_path` set
    (or INTAKE_FIXTURE_CONVERSATION in env), reads the offline fixture instead of
    calling gh — the seam that keeps smoke + demo harnesses offline. The fixture
    is a JSON object keyed by issue number (string), each value carrying optional
    "comments" and "history" lists. A missing key yields empty lists.
    """
    if fixture_path:
        table = _load_json(fixture_path)
        entry = (table or {}).get(str(number)) or {}
        return {
            "conversation": _normalize_comments(entry.get("comments")),
            "history": _normalize_history(entry.get("history")),
        }
    # Live: one `gh issue view` for comments. History (linked PRs) is best-effort
    # and tolerant of failure — a thread is still useful without it.
    convo = _gh("issue", "view", str(number), "--repo", repo,
                "--json", "comments", gh_bin=gh_bin)
    history: List[Dict[str, str]] = []
    try:
        prs = _gh("pr", "list", "--repo", repo, "--search", f"linked:{number}",
                  "--state", "all", "--json", "number,title,url",
                  "--limit", "10", gh_bin=gh_bin)
        history = _normalize_history(prs)
    except RuntimeError:
        history = []
    return {
        "conversation": _normalize_comments((convo or {}).get("comments")),
        "history": history,
    }


def enrich_conversation(
    items: List["IntakeItem"],
    *,
    fixture_path: str = "",
    gh_bin: str = "gh",
) -> List["IntakeItem"]:
    """Populate each item's `conversation` + `history` in place and return `items`.

    Offline when a fixture is supplied; otherwise one `gh issue view` per item.
    Used by the provider to ground selected issues; the cheap list path that does
    not need the thread simply never calls this."""
    fx = fixture_path or os.environ.get("INTAKE_FIXTURE_CONVERSATION", "")
    for it in items:
        data = fetch_conversation(it.repository or "", it.number,
                                  fixture_path=fx, gh_bin=gh_bin)
        it.conversation = data["conversation"]
        it.history = data["history"]
    return items


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
            labels=_label_names(content.get("labels"), item.get("labels")),
            repository=_repo_slug(content.get("repository") or ""),
            assignees=list(item.get("assignees") or []),
            project_status=status,
            dispatch=item.get("dispatch") or None,
            hours_estimate=int(hours_raw) if hours_raw is not None else None,
            source_url=content.get("url") or "",
            milestone=_field_title(content.get("milestone") or item.get("milestone")
                                   or item.get("Milestone")),
            iteration=_field_title(item.get("iteration") or item.get("Iteration")),
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
            "--json", "number,title,body,labels,assignees,url,milestone",
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
            labels=_label_names(issue.get("labels")),
            repository=repo,
            assignees=[a["login"] for a in issue.get("assignees") or []],
            project_status=None,
            dispatch=None,
            hours_estimate=None,
            source_url=issue.get("url") or "",
            milestone=_field_title(issue.get("milestone")),
            iteration=None,   # iteration is a ProjectV2 field; absent in repo mode
        ))

    return items


# ---------------------------------------------------------------------------
# Provider — the single source of issue information for every consumer
# ---------------------------------------------------------------------------
# intake.py is the ONLY component that performs a live GitHub (web) fetch for
# issues. Selectors (src/intake/pipeline.py, src/architect/dispatch.py)
# never call gh themselves: they call provide_items(), which prefers a locally
# saved snapshot of issue information and falls back to one — and only one —
# live fetch, persisting the result so every other selector reads it offline.
# All selectors then build the local DAG from this saved information.

def snapshot_path(explicit: str = "") -> str:
    """Resolve the saved-issue-info snapshot path: explicit arg > env
    DISPATCH_ISSUES_SNAPSHOT > ${DISPATCH_ARTIFACTS_DIR:-.dispatch}/issues.json."""
    if explicit:
        return explicit
    env = os.environ.get("DISPATCH_ISSUES_SNAPSHOT")
    if env:
        return env
    base = os.environ.get("DISPATCH_ARTIFACTS_DIR", ".dispatch")
    return os.path.join(base, "issues.json")


def _validate_issue_boundary(item: "IntakeItem") -> None:
    """Fail-soft issue.v1 boundary guard (#145).

    Surfaces a drift in the normalized intake item — the single source of issue
    information every downstream selector reads — on the live path without ever
    breaking intake. The saved snapshot is the SAME dict minus ``schema_version``;
    we validate a stamped copy additively. Import + validation are best-effort.
    """
    try:
        _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from src.orchestration import boundaries
    except Exception:
        return
    try:
        boundaries.warn_if_invalid(asdict(item), boundaries.ISSUE_SCHEMA,
                                   label=f"issue#{item.number}")
    except Exception:
        pass


def save_items(items: List[IntakeItem], path: str) -> None:
    """Persist issue information as the canonical local snapshot."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    for it in items:
        _validate_issue_boundary(it)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([asdict(i) for i in items], fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def load_items(path: str) -> List[IntakeItem]:
    """Load saved issue information (a snapshot written by save_items)."""
    raw = _load_json(path)
    return [IntakeItem(**d) for d in raw]


def provide_items(
    *,
    project: str = "",
    repo: str = "",
    label_filter: str = "",
    status_filter: str = "",
    limit: int = 50,
    gh_bin: str = "gh",
    snapshot: str = "",
    refresh: bool = False,
    persist: bool = True,
    fixture_project: str = "",
    fixture_repo: str = "",
    with_conversation: bool = False,
    fixture_conversation: str = "",
) -> List[IntakeItem]:
    """THE single entry point every consumer uses to obtain issue information.

    This is the only function that may perform a live GitHub fetch. Resolution,
    in order (a consumer never reaches gh except through this one function):

      1. an explicit fixture (test seam) for the chosen source -> load offline
      2. a saved snapshot present and `refresh` is False        -> load offline
      3. otherwise                                              -> one live fetch
         via intake_from_{project,repo}, then persist the snapshot so every other
         selector reads the same saved information offline.

    When `with_conversation` is set, each item is additionally grounded with its
    comment thread + relevant history (#132) via enrich_conversation — offline
    when a conversation fixture is supplied. Items already carrying a conversation
    (a snapshot that captured it) are left untouched, so the enrichment never
    re-fetches what a snapshot already holds.
    """
    fp = fixture_project or os.environ.get("INTAKE_FIXTURE_PROJECT", "")
    fr = fixture_repo or os.environ.get("INTAKE_FIXTURE_REPO", "")

    def _ground(items: List[IntakeItem]) -> List[IntakeItem]:
        if with_conversation:
            pending = [it for it in items if not it.conversation and not it.history]
            if pending:
                enrich_conversation(pending, fixture_path=fixture_conversation,
                                    gh_bin=gh_bin)
        return items

    # 1. Explicit fixture seam (offline; never cached as a snapshot).
    if project and fp:
        org, num = project.split("/", 1)
        return _ground(intake_from_project(org, num, status_filter=status_filter,
                                           limit=limit, fixture_path=fp, gh_bin=gh_bin))
    if repo and fr:
        return _ground(intake_from_repo(repo, label_filter=label_filter, limit=limit,
                                        fixture_path=fr, gh_bin=gh_bin))

    # 2. Saved snapshot (offline) unless a refresh was requested.
    snap = snapshot_path(snapshot)
    if not refresh and os.path.exists(snap):
        return _ground(load_items(snap))

    # 3. The one live fetch, then persist for downstream offline reads.
    if project:
        org, num = project.split("/", 1)
        items = intake_from_project(org, num, status_filter=status_filter,
                                    limit=limit, fixture_path="", gh_bin=gh_bin)
    else:
        items = intake_from_repo(repo, label_filter=label_filter, limit=limit,
                                 fixture_path="", gh_bin=gh_bin)
    _ground(items)
    if persist:
        try:
            save_items(items, snap)
        except OSError:
            pass
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
