"""fix_dispatch.py — CI-failure fix ladder (HANDOFF §5.5).

Python port of scripts/fix-dispatch.sh. Owns
``fix-attempt-(N-1) -> fix-attempt-N`` and ``fix-attempt-3 -> needs-human``.
Not one of the six tick stages (it is event-triggered on CI failure), so it is
a direct port rather than a visitor element.

On each fix attempt it also feeds the CI failure back as a revised
diagnose-then-replan directive (#137) — recorded on the PR thread + a
``fix-rescaffold`` run-ledger event — so the next attempt targets the diagnosed
cause instead of retrying the identical work order at a bigger model tier.

Attempt resolution (first match wins): PIPELINE_FIX_ATTEMPT env -> payload
.attempt -> (max fix-attempt-N label) + 1 -> default 1.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Shared #137 rescaffold directive lives in src/architect/rescaffold.py (a leaf,
# importable by both this CI-failure ladder and the workflow-engine invoice
# action). architect/ is not a package, so add it to the path and import bare —
# the same flat-subsystem idiom the bindings layer uses.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "architect"))
from rescaffold import rescaffold_directive as _rescaffold_directive  # noqa: E402

from . import adversary, common  # noqa: E402


def load_payload(arg=None) -> str:
    fixture = os.environ.get("PIPELINE_FIXTURE_PR")
    if fixture:
        return open(fixture).read()
    if arg and os.path.isfile(arg):
        return open(arg).read()
    return sys.stdin.read()


def _validate_pr_event(payload) -> None:
    """Fail-soft pr-event.v1 boundary guard (#145).

    Same unified PR-event contract closure.py validates; `attempt` resolves to
    one source of truth (payload field wins over fix-attempt-N labels). Surface
    a drift on the live path, never break the fix ladder. Validate a stamped
    copy additively (raw payload = v1 object minus ``schema_version``).
    """
    try:
        from . import boundaries
        if isinstance(payload, dict):
            boundaries.warn_if_invalid(payload, boundaries.PR_EVENT_SCHEMA,
                                       label="pr-event/fix-ladder")
    except Exception:
        pass


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
    _validate_pr_event(payload)
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

    # #111 cross-model adversarial weigh-in before escalating (advisory, fail-open,
    # dry-run-safe). Covers both the tier bump and the fix-attempt-3 -> needs-human
    # cap below; recorded to the ledger but NEVER vetoes the escalation.
    wi = adversary.weigh_in(
        "fix-ladder",
        {"pr": pr, "attempt": attempt, "tier": tier, "conclusion": conclusion},
        untrusted_text=brief,
        dry_run=common.is_dry_run(),
    )
    common.log(
        f"PR #{pr}: adversary weigh-in ({wi.get('backend') or 'none'}): "
        f"{wi.get('verdict') or wi.get('reason') or wi.get('error') or 'n/a'}"
    )
    common.ledger_emit("adversary-weigh-in", pr, json.dumps(wi, ensure_ascii=False))

    if tier == common.NEEDS_HUMAN_ROUTE:
        cap = common.fix_attempt_cap()
        common.log(f"PR #{pr}: attempt {attempt} exceeds cap ({cap}) -> escalating to operator.")
        common.gh_mutate("pr", "edit", pr, "--add-label", common.NEEDS_HUMAN_ROUTE)
        common.gh_mutate(
            "pr", "comment", pr, "--body",
            f"Pipeline: fix-attempt cap exceeded (attempt {attempt} > {cap}). "
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
    # #137 — feed the CI failure back as a revised diagnose-then-replan directive
    # (durable on the PR thread = source of truth) + ledger it, so the next attempt
    # gets the diagnosis instead of retrying the identical order. The tier bump
    # stays the capability lever; this adds the missing understanding step.
    common.gh_mutate(
        "pr", "comment", pr, "--body",
        _rescaffold_directive(attempt, tier, conclusion, brief),
    )
    common.ledger_emit(
        "fix-rescaffold", "",
        json.dumps({"pr": pr, "attempt": attempt, "tier": tier,
                    "conclusion": conclusion, "rescaffold": True}, ensure_ascii=False),
    )
    common.log(
        f"PR #{pr}: labeled fix-attempt-{attempt} (tier {tier}); rescaffold directive "
        f"posted (CI failure fed back, not an identical retry)."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except common.PipelineExit as exc:
        sys.exit(exc.code)
