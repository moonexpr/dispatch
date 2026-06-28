#!/usr/bin/env python3
"""Patch: create the research-mode label (<=100 char desc) + the 4 E6 objects
that failed when the label was rejected. Live. Idempotent guard: skips if E6
epic already resolved in the idmap.
"""
import json, os, re, subprocess, sys

REPO = "ReclaimByDesign/dispatch"
SPEC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN = json.load(open(os.path.join(SPEC, "02-roadmap.json")))
IDMAP_PATH = os.path.join(SPEC, "02-roadmap-idmap.json")
idmap = json.load(open(IDMAP_PATH))
MS = PLAN["milestone"]["title"]


def sh(args):
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(f"ERR ({' '.join(args[:4])}…): {r.stderr.strip()[:200]}\n")
        return ""
    return r.stdout.strip()


def num(url):
    m = re.search(r"/issues/(\d+)", url or "")
    return int(m.group(1)) if m else None


# 1. label (short description, <=100 chars)
sh(["gh", "label", "create", "research-mode", "--repo", REPO, "--color", "1d76db",
    "--description", "Architect research-gathering work-order mode (D3); later orders embed committed research.",
    "--force"])
print("label research-mode ensured")

if idmap.get("E6") and isinstance(idmap["E6"], int):
    print(f"E6 already exists as #{idmap['E6']}; nothing to do.")
    sys.exit(0)

e6 = next(ep for ep in PLAN["epics"] if ep["local_id"] == "E6")

# 2. epic
en = num(sh(["gh", "issue", "create", "--repo", REPO, "--title", e6["title"],
             "--body", e6["body_md"], "--milestone", MS]
            + sum([["--label", l] for l in e6["labels"]], [])))
idmap["E6"] = en
print(f"E6 -> #{en}")

# 3. children
for it in e6["issues"]:
    body = f"**Part of #{en}**\n\n" + it["body_md"]
    n = num(sh(["gh", "issue", "create", "--repo", REPO, "--title", it["title"],
                "--body", body, "--milestone", MS]
               + sum([["--label", l] for l in it["labels"]], [])))
    idmap[it["local_id"]] = n
    print(f"{it['local_id']} -> #{n}")

# 4. epic child checklist
lines = [f"- [ ] #{idmap[it['local_id']]} — {it['title']}" for it in e6["issues"] if idmap.get(it["local_id"])]
new_body = e6["body_md"] + "\n\n## Child issues <!-- sprint-linked -->\n" + "\n".join(lines)
sh(["gh", "issue", "edit", str(en), "--repo", REPO, "--body", new_body])
print(f"epic #{en}: +{len(lines)} child links")

# 5. dep comments
for it in e6["issues"]:
    n = idmap.get(it["local_id"])
    deps = [idmap.get(d) for d in it.get("depends_on", []) if idmap.get(d)]
    if n and deps:
        dep_str = ", ".join(f"#{d}" for d in deps)
        sh(["gh", "issue", "comment", str(n), "--repo", REPO,
            "--body", f"**Depends on:** {dep_str}  (sprint dependency)"])
        print(f"#{n}: depends on {dep_str}")

json.dump(idmap, open(IDMAP_PATH, "w"), indent=2)
print("idmap updated:", len([v for v in idmap.values() if v]), "resolved")
