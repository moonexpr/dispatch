#!/usr/bin/env python3
"""install_claude_skills.py — install dispatch's Claude Code skills.

Copies the composable ``/dispatch`` skill family from this repository into a
personal Claude Code skills directory so the skills are available in every
project, not only when the dispatch repo is the open workspace. The skills are
the recommended way to interact with the pipeline: they interview the operator
with ``AskUserQuestion`` and steer work toward dispatch's strengths (small,
clear, checkable units) and away from its L3-class weaknesses (vague or sprawling
asks). See the wiki page "Interacting with dispatch" for the full rationale.

Cross-platform: pure standard library, identical behaviour on Windows, macOS and
Linux. The personal skills directory is resolved from the home directory —
``~/.claude/skills`` on macOS/Linux, ``%USERPROFILE%\\.claude\\skills`` on
Windows — and may be overridden with ``--target``.

Usage:
    python install_claude_skills.py            # install into ~/.claude/skills/
    python install_claude_skills.py --list     # show what would be installed
    python install_claude_skills.py --force     # overwrite an existing copy
    python install_claude_skills.py --target DIR

Exit status is 0 on success, 1 on a handled error (nothing to install, a
collision without --force, an unwritable target).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# The skill directories that make up the /dispatch family. Each is a directory
# under <repo>/.claude/skills/ containing a SKILL.md.
SKILL_DIR_NAMES = [
    "dispatch",
    "dispatch-scope",
    "dispatch-decompose",
    "dispatch-tick",
    "dispatch-review",
    "dispatch-oneshot",
]


def source_skills_dir() -> Path:
    """The .claude/skills directory that ships beside this script."""
    return Path(__file__).resolve().parent / ".claude" / "skills"


def default_target_dir() -> Path:
    """The personal Claude Code skills directory for the current user.

    ``Path.home()`` resolves to ``%USERPROFILE%`` on Windows and ``$HOME`` on
    macOS/Linux, so a single expression is correct on all three platforms.
    """
    return Path.home() / ".claude" / "skills"


def discover(source: Path) -> list[Path]:
    """Return the skill directories present under *source*, in declared order."""
    found = []
    for name in SKILL_DIR_NAMES:
        candidate = source / name
        if (candidate / "SKILL.md").is_file():
            found.append(candidate)
    return found


def install(skills: list[Path], target: Path, force: bool) -> int:
    """Copy each skill directory into *target*. Returns a process exit code."""
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # unwritable / bad path
        print(f"error: cannot create target {target}: {exc}", file=sys.stderr)
        return 1

    installed, skipped = [], []
    for skill in skills:
        dest = target / skill.name
        if dest.exists():
            if not force:
                print(f"  skip   {skill.name}  (exists; use --force to overwrite)")
                skipped.append(skill.name)
                continue
            shutil.rmtree(dest)
        shutil.copytree(skill, dest)
        print(f"  install {skill.name}  ->  {dest}")
        installed.append(skill.name)

    print()
    print(f"installed {len(installed)} skill(s); skipped {len(skipped)}.")
    if skipped and not force:
        print("re-run with --force to overwrite the skipped skills.")
    if installed:
        print("invoke them in Claude Code as /dispatch, /dispatch:scope, "
              "/dispatch:tick, /dispatch:decompose, /dispatch:review, /dispatch:oneshot.")
    # A run that copied nothing and skipped nothing is a no-op worth flagging.
    return 0 if (installed or skipped) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install dispatch's /dispatch Claude Code skill family.",
    )
    parser.add_argument(
        "--target", type=Path, default=None,
        help="skills directory to install into (default: ~/.claude/skills)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="overwrite skills that already exist in the target",
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_only",
        help="list the skills that would be installed, then exit",
    )
    args = parser.parse_args(argv)

    source = source_skills_dir()
    skills = discover(source)
    if not skills:
        print(f"error: no dispatch skills found under {source}", file=sys.stderr)
        return 1

    if args.list_only:
        print(f"skills available under {source}:")
        for skill in skills:
            print(f"  {skill.name}")
        return 0

    target = args.target if args.target is not None else default_target_dir()
    print(f"installing {len(skills)} skill(s) into {target}")
    return install(skills, target, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
