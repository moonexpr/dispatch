"""fix_dispatch.py — CI-failure fix ladder (HANDOFF §5.5).

Python port of scripts/fix-dispatch.sh. Owns
``fix-attempt-(N-1) -> fix-attempt-N`` and ``fix-attempt-3 -> needs-human``.
Not one of the six tick stages (it is event-triggered on CI failure), so it is
a direct port rather than a visitor element.

Attempt resolution (first match wins): PIPELINE_FIX_ATTEMPT env -> payload
.attempt -> (max fix-attempt-N label) + 1 -> default 1.
"""

from __future__ import annotations

import json
import os
import re
import sys

from . import common


def load_payload(arg=None) -> str:
    fixture = os.environ.get("PIPELINE_FIXTURE_PR")
    if fixture:
        return open(fixture).read()
    if arg and os.path.isfile(arg):
        return open(arg).read()
    return sys.stdin.read()


def pr_labels(payload: dict) -> list[str]:
    out = []
    for lbl in payload.get("labels") or []:
        out.append(lbl.get("name") if isinstance(lbl, dict) else lbl)
    return [x for x in out if x is not None]


def max_fix_attempt(payload: dict) -> int:
    m = 0
    for lbl in pr_labels(payload):
        if isinstance(lbl, str) and lbl.startswith("fix-attempt-"):
            n = lbl[len("fix-attempt-"):]
            if re.match(r"^[0-9]+$", n) and int(n) > m:
                m = int(n)
    return m


def resolve_attempt(payload: dict) -> str:
    override = os.environ.get("PIPELINE_FIX_ATTEMPT")
    if override:
        return override
    pa = payload.get("attempt")
    if pa not in (None, "", "null"):
        return str(pa)
    return str(max_fix_attempt(payload) + 1)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    payload = json.loads(load_payload(argv[0] if argv else None))
    pr = payload.get("pr") or payload.get("pull_request")
    pr = "" if pr is None else str(pr)
    if not pr:
        common.die("payload has no .pr")
    conclusion = payload.get("conclusion") or "failure"
    brief = payload.get("brief") or "(see CI logs)"
    attempt = resolve_attempt(payload)
    tier = common.tier_for_attempt(attempt)

    common.log(
        f"fix-dispatch: PR #{pr} conclusion={conclusion} attempt={attempt} -> "
        f"tier={tier} (dry_run={os.environ['PIPELINE_DRY_RUN']})"
    )

    if conclusion == "success":
        common.log(
            f"PR #{pr} CI is green; fix-dispatch is a no-op (closure handles "
            f"success). Exiting 0."
        )
        return 0

    if tier == "needs-human":
        common.log(f"PR #{pr}: attempt {attempt} exceeds cap (3) -> escalating to operator.")
        common.gh_mutate("pr", "edit", pr, "--add-label", "needs-human")
        common.gh_mutate(
            "pr", "comment", pr, "--body",
            f"Pipeline: fix-attempt cap exceeded (attempt {attempt} > 3). "
            f"Brief: {brief}. Needs human.",
        )
        common.log(f"operator-notification path taken for PR #{pr}.")
        return 0

    prev = int(attempt) - 1
    if prev >= 1:
        common.gh_mutate(
            "pr", "edit", pr,
            "--remove-label", f"fix-attempt-{prev}",
            "--add-label", f"fix-attempt-{attempt}",
        )
    else:
        common.gh_mutate("pr", "edit", pr, "--add-label", f"fix-attempt-{attempt}")
    common.log(
        f"PR #{pr}: labeled fix-attempt-{attempt} (tier {tier}); engineer handles CI fix."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except common.PipelineExit as exc:
        sys.exit(exc.code)
