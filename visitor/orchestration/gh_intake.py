"""gh_intake.py — fetch issues from a GitHub ProjectV2 or repo.

Python port of scripts/gh-intake.sh. Emits a normalized JSON array to stdout
(or --output file) for downstream pipeline consumption. Not a tick stage; a
direct port. Modes: --project ORG/NUM (board) or --repo OWNER/REPO (open issues
by label), with INTAKE_* env equivalents and offline fixtures.
"""

from __future__ import annotations

import json
import os
import re
import sys

from engine import proc

from . import common


def _jc(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _gh(*args) -> str:
    common.require_tool(os.environ["GH_BIN"])
    return proc.run_checked(
        [os.environ["GH_BIN"], *[str(a) for a in args]],
        fail_msg="gh-intake: gh call failed (see stderr)",
        on_fail=common.die,
    )


def intake_from_project(project_arg: str) -> list:
    org, _, num = project_arg.partition("/")
    if not org or org == num or not num:
        common.die(f"gh-intake: --project must be ORG/NUM (got '{project_arg}')")
    status_filter = os.environ.get("INTAKE_STATUS_FILTER", "")
    limit = os.environ.get("INTAKE_LIMIT", "50")
    common.log(
        f"gh-intake: project {org}/{num} status_filter='{status_filter}' limit={limit}"
    )
    fixture = os.environ.get("INTAKE_FIXTURE_PROJECT")
    if fixture:
        raw = json.loads(open(fixture).read())
    else:
        raw = json.loads(
            _gh("project", "item-list", num, "--owner", org, "--format", "json", "--limit", limit)
        )
    items = raw.get("items", [])
    if status_filter:
        items = [i for i in items if re.search(status_filter, i.get("status") or "", re.I)]
    items = [i for i in items if (i.get("content") or {}).get("type") == "Issue"]
    out = []
    for i in items:
        content = i.get("content") or {}
        out.append(
            {
                "number": content.get("number", 0),
                "title": content.get("title") or i.get("title") or "",
                "body": content.get("body") or "",
                "labels": [],
                "repository": content.get("repository") or "",
                "assignees": i.get("assignees") or [],
                "project_status": i.get("status"),
                "dispatch": i.get("dispatch"),
                "hours_estimate": i.get("hours Estimate"),
                "source_url": content.get("url") or "",
            }
        )
    return out


def intake_from_repo(repo_arg: str) -> list:
    label_filter = os.environ.get("INTAKE_LABEL_FILTER", "")
    limit = os.environ.get("INTAKE_LIMIT", "50")
    common.log(f"gh-intake: repo {repo_arg} label_filter='{label_filter}' limit={limit}")
    fixture = os.environ.get("INTAKE_FIXTURE_REPO")
    if fixture:
        raw = json.loads(open(fixture).read())
    else:
        cmd = [
            "issue", "list", "--state", "open",
            "--json", "number,title,body,labels,assignees,url",
            "--limit", limit,
        ]
        if repo_arg:
            cmd += ["--repo", repo_arg]
        if label_filter:
            cmd += ["--label", label_filter]
        raw = json.loads(_gh(*cmd))
    out = []
    for i in raw:
        out.append(
            {
                "number": i.get("number"),
                "title": i.get("title"),
                "body": i.get("body") or "",
                "labels": [lbl.get("name") for lbl in (i.get("labels") or [])],
                "repository": repo_arg,
                "assignees": [a.get("login") for a in (i.get("assignees") or [])],
                "project_status": None,
                "dispatch": None,
                "hours_estimate": None,
                "source_url": i.get("url") or "",
            }
        )
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = ""
    project_arg = ""
    repo_arg = ""
    output_file = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--project":
            mode, project_arg = "project", argv[i + 1]; i += 2
        elif a == "--repo":
            mode, repo_arg = "repo", argv[i + 1]; i += 2
        elif a == "--status":
            os.environ["INTAKE_STATUS_FILTER"] = argv[i + 1]; i += 2
        elif a == "--label":
            os.environ["INTAKE_LABEL_FILTER"] = argv[i + 1]; i += 2
        elif a == "--limit":
            os.environ["INTAKE_LIMIT"] = argv[i + 1]; i += 2
        elif a == "--output":
            output_file = argv[i + 1]; i += 2
        elif a in ("--help", "-h"):
            print(__doc__.strip())
            return 0
        else:
            common.die(f"gh-intake: unknown flag: {a}")

    if not mode:
        if os.environ.get("INTAKE_PROJECT"):
            mode, project_arg = "project", os.environ["INTAKE_PROJECT"]
        elif os.environ.get("INTAKE_REPO") or os.environ.get("PIPELINE_REPO"):
            mode = "repo"
            repo_arg = os.environ.get("INTAKE_REPO") or os.environ.get("PIPELINE_REPO")
        else:
            common.die(
                "gh-intake: specify --project ORG/NUM or --repo OWNER/REPO "
                "(or set INTAKE_PROJECT / INTAKE_REPO)"
            )

    if mode == "project":
        result = intake_from_project(project_arg)
    elif mode == "repo":
        result = intake_from_repo(repo_arg)
    else:
        common.die(f"gh-intake: unknown mode '{mode}'")

    count = len(result)
    rendered = _jc(result)
    if output_file:
        with open(output_file, "w") as fh:
            fh.write(rendered + "\n")
        common.log(f"gh-intake: wrote {count} item(s) to {output_file}")
    else:
        print(rendered)
        common.log(f"gh-intake: emitted {count} item(s)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except common.PipelineExit as exc:
        sys.exit(exc.code)
