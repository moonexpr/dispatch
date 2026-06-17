#!/usr/bin/env python3
"""Create the dispatch-1.0 roadmap (milestone + labels + epics + issues) on
ReclaimByDesign/dispatch from 02-roadmap.json. Deterministic, idempotent-ish.

Env:
  DISPATCH_ROADMAP_LIVE=1     actually mutate GitHub (default: DRY — print only)
  DISPATCH_ROADMAP_SCOPE=full|lean|epics-only   (default full)
      full        all 8 epics + all 30 issues; E6/stretch get the milestone too.
      lean        same, but E6-* (research-mode) and *stretch* issues are filed
                  to a BACKLOG (no milestone, no day-label) so the 3-day
                  milestone stays realistic.
      epics-only  create the 8 epic trackers + milestone + labels + the two
                  DEC decisions only; child issues filed in a later pass.

Cross-linking: epics created first (numbers captured); each child gets
"Part of #<epic>" at the top; a final pass appends a child checklist to each
epic body and a resolved "Depends on #…" line to issues that have deps.
"""
import json, os, re, subprocess, sys

REPO = "ReclaimByDesign/dispatch"
SPEC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN = json.load(open(os.path.join(SPEC, "02-roadmap.json")))

LIVE = os.environ.get("DISPATCH_ROADMAP_LIVE") == "1"
SCOPE = os.environ.get("DISPATCH_ROADMAP_SCOPE", "full")
MARK = "<!-- sprint-linked -->"


def sh(args, input_text=None):
    """Run a command; in DRY mode print mutating gh calls instead of running."""
    mutating = args[:2] in (["gh", "issue"], ["gh", "label"], ["gh", "api"]) and (
        any(a in args for a in ("create", "edit")) or "-f" in args or "-X" in args)
    if not LIVE and mutating:
        printable = " ".join(a if " " not in a else f'"{a[:40]}…"' for a in args)
        print(f"   DRY: {printable}")
        return ""  # no number captured in dry
    res = subprocess.run(args, input=input_text, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(f"ERR ({' '.join(args[:4])}…): {res.stderr.strip()[:300]}\n")
        return ""
    return res.stdout.strip()


def ensure_labels(labels):
    print(f"\n== LABELS ({len(labels)}) ==")
    for l in labels:
        sh(["gh", "label", "create", l["name"], "--repo", REPO, "--color", l["color"],
            "--description", l["description"], "--force"])
        print(f"   label: {l['name']}")


def ensure_milestone(ms):
    print(f"\n== MILESTONE ==\n   {ms['title']}")
    if not LIVE:
        print("   DRY: gh api .../milestones -f title=… -f description=…")
        return ms["title"]
    # idempotent: find existing by title
    existing = sh(["gh", "api", f"repos/{REPO}/milestones", "--jq",
                   f'.[] | select(.title=="{ms["title"]}") | .number'])
    if existing:
        print(f"   (exists: milestone {existing})")
        return ms["title"]
    sh(["gh", "api", f"repos/{REPO}/milestones", "-f", f"title={ms['title']}",
        "-f", f"description={ms['description'][:1000]}"])
    return ms["title"]


def num_from_url(url):
    m = re.search(r"/issues/(\d+)", url or "")
    return int(m.group(1)) if m else None


def create_issue(title, body, labels, milestone_title):
    args = ["gh", "issue", "create", "--repo", REPO, "--title", title, "--body", body]
    for l in labels:
        args += ["--label", l]
    if milestone_title:
        args += ["--milestone", milestone_title]
    out = sh(args)
    n = num_from_url(out)
    return n


def main():
    print(f"REPO={REPO}  LIVE={LIVE}  SCOPE={SCOPE}")
    ms_title = PLAN["milestone"]["title"]
    ensure_labels(PLAN["new_labels"])
    ensure_milestone(PLAN["milestone"])

    idmap = {}   # local_id -> issue number

    # ---- pass 1: epics ----
    print(f"\n== EPICS ==")
    for ep in PLAN["epics"]:
        n = create_issue(ep["title"], ep["body_md"], ep["labels"], ms_title)
        idmap[ep["local_id"]] = n
        print(f"   {ep['local_id']:4} -> #{n}  {ep['title'][:60]}")

    if SCOPE == "epics-only":
        # only the two decisions get filed alongside epics
        children = [it for it in PLAN["standalone_issues"] if it["local_id"] in ("DEC-1", "DEC-2")]
        epics_children = []
    else:
        epics_children = [(ep["local_id"], it) for ep in PLAN["epics"] for it in ep["issues"]]
        children = list(PLAN["standalone_issues"])

    # ---- pass 2: child + standalone issues ----
    print(f"\n== ISSUES ==")
    rows = [(eid, it) for eid, it in epics_children] + [(it.get("epic", ""), it) for it in children]
    for parent_eid, it in rows:
        labels = list(it["labels"])
        milestone = ms_title
        backlog = False
        if SCOPE == "lean" and (it.get("stretch") or it["local_id"].startswith("E6-")):
            backlog = True
            milestone = None
            labels = [l for l in labels if not l.startswith("day-")]
            if "stretch" not in labels:
                labels.append("stretch")
        parent_num = idmap.get(parent_eid)
        top = f"**Part of #{parent_num}**\n\n" if parent_num else ""
        body = top + it["body_md"]
        n = create_issue(it["title"], body, labels, milestone)
        idmap[it["local_id"]] = n
        tag = " [BACKLOG]" if backlog else ""
        print(f"   {it['local_id']:7} -> #{n}  ->{parent_eid or '-':4}{tag}  {it['title'][:54]}")

    # ---- pass 3: cross-link (epic child checklists + resolved deps) ----
    print(f"\n== CROSS-LINK (edit pass) ==")
    # epic child checklists
    for ep in PLAN["epics"]:
        if SCOPE == "epics-only":
            break
        epic_n = idmap.get(ep["local_id"])
        kids = [it for it in ep["issues"]]
        lines = []
        for it in kids:
            kn = idmap.get(it["local_id"])
            if kn:
                lines.append(f"- [ ] #{kn} — {it['title']}")
        # standalone issues routed to this epic
        for it in PLAN["standalone_issues"]:
            if it.get("epic") == ep["local_id"] and idmap.get(it["local_id"]):
                lines.append(f"- [ ] #{idmap[it['local_id']]} — {it['title']} *(standalone)*")
        if lines and epic_n:
            new_body = ep["body_md"] + f"\n\n## Child issues {MARK}\n" + "\n".join(lines)
            sh(["gh", "issue", "edit", str(epic_n), "--repo", REPO, "--body", new_body])
            print(f"   epic #{epic_n}: +{len(lines)} child links")

    # resolved deps on issues that have them
    all_issue_specs = [it for ep in PLAN["epics"] for it in ep["issues"]] + PLAN["standalone_issues"]
    for it in all_issue_specs:
        n = idmap.get(it["local_id"])
        deps = [idmap.get(d) for d in it.get("depends_on", []) if idmap.get(d)]
        if n and deps:
            dep_str = ", ".join(f"#{d}" for d in deps)
            sh(["gh", "issue", "comment", str(n), "--repo", REPO,
                "--body", f"**Depends on:** {dep_str}  (sprint dependency)"])
            print(f"   #{n}: depends on {dep_str}")

    print(f"\nDONE. {'LIVE' if LIVE else 'DRY-RUN'} scope={SCOPE}. "
          f"local_id->number map has {len([v for v in idmap.values() if v])} resolved.")
    # persist the id map for traceability
    if LIVE:
        with open(os.path.join(SPEC, "02-roadmap-idmap.json"), "w") as f:
            json.dump(idmap, f, indent=2)


if __name__ == "__main__":
    main()
