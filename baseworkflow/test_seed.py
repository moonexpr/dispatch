#!/usr/bin/env python3
"""test_seed.py — the seed controller inside baseworkflow (ADR-003 e2e).

The lifecycle now STARTS with the ``seed.intake`` controller (a ``controller:``
reference — the three-layer discipline), kernel-initialized via ``input.request``.
Covers all three intake sources — GitHub issue, operator interaction (which may
leave requirements open), non-interactive job request (accepted or rejected) —
plus: the supersede handoff (#190) with its audit trail, the fallback +
``terminal_when`` early completion on rejection, per-request satisfaction on a
partial service registry, and the legacy job/triage synthesis path.

No pytest in this repo — a runnable, self-asserting module (exit 0 = pass).
Run: ``python3 baseworkflow/test_seed.py``.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import baseworkflow as bw  # noqa: E402
import services as bw_services  # noqa: E402  (baseworkflow/services.py)
from foundation.services import ServiceRegistry  # noqa: E402

CHECKS = {"n": 0}


def ok(label: str) -> None:
    CHECKS["n"] += 1
    print(f"  ok  {label}")


JOB = {"issue": 7, "title": "Add retry", "body": "Implement retry with backoff.", "labels": []}
TRIAGE = {"action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.9}

ISSUE_FIXTURE = {
    7: {
        "number": 7,
        "title": "Add retry",
        "body": "Implement retry.\n\n## Acceptance criteria\n- retries 3x\n- backs off",
        "labels": ["enhancement"],
    }
}


def audited(summary, event: str) -> bool:
    return any(e.get("event") == event for e in summary["interpreter"].events)


def test_github_intake() -> None:
    request = {"source": "github", "repo": "o/r", "issue": 7}
    services = bw_services.build_mock_services(issues=ISSUE_FIXTURE)
    s = bw.run_mock(JOB, TRIAGE, request=request, services=services)
    assert s["result"].ok
    wi = s["deliverables"].get("work_item")
    assert wi and wi["source"] == "github" and wi["title"] == "Add retry", wi
    assert "retries 3x" in wi["acceptance"]
    assert wi["provenance"] == {"kind": "github.issue", "repo": "o/r", "issue": 7}
    assert "decision" not in s["deliverables"], "accepted request must not fall back"
    assert audited(s, "supersede.accepted") and audited(s, "supersede.completed")
    assert "plan" in s["deliverables"], "lifecycle continues into spec after the seed"
    ok("github intake: issue pulled via github.access; supersede handed off; lifecycle continued")


def test_interactive_intake_leaves_needs_open() -> None:
    request = {"source": "interactive", "fields": {"title": "Fix login"}}
    # The operator answers `goal` but declines `acceptance` — an interactive
    # request may not fulfill all requirements and still proceeds.
    services = bw_services.build_mock_services(answers={"goal": "Users can log in again"})
    s = bw.run_mock(JOB, TRIAGE, request=request, services=services)
    assert s["result"].ok
    wi = s["deliverables"].get("work_item")
    assert wi and wi["source"] == "interactive", wi
    assert wi["goal"] == "Users can log in again"
    assert wi["open_needs"] == ["acceptance"], wi["open_needs"]
    assert audited(s, "supersede.accepted")
    ok("interactive intake: missing fields asked; declined ones recorded as open_needs")


def test_job_intake_accepted() -> None:
    request = {"source": "job", "job": {"title": "T", "goal": "G", "acceptance": "A"}}
    s = bw.run_mock(JOB, TRIAGE, request=request)
    wi = s["deliverables"].get("work_item")
    assert wi and wi["provenance"] == {"kind": "job.request"} and wi["acceptance"] == "A", wi
    assert "decision" not in s["deliverables"]
    ok("job intake: a complete non-interactive request is accepted and handed off")


def test_job_intake_rejected_terminates_early() -> None:
    request = {"source": "job", "job": {"title": "only a title"}}
    s = bw.run_mock(JOB, TRIAGE, request=request)
    assert s["result"].ok, "a rejection is a successful determination, not a failure"
    decision = s["deliverables"].get("decision")
    assert decision and decision["accepted"] is False and decision["missing"] == ["goal"], decision
    assert "work_item" not in s["deliverables"]
    assert "plan" not in s["deliverables"], "terminal_when must stop the lifecycle at the seed"
    assert not audited(s, "supersede.accepted")
    ok("job intake: incomplete request rejected; decision recorded; lifecycle ends early")


def test_unknown_source_falls_back() -> None:
    s = bw.run_mock(JOB, TRIAGE, request={"source": "carrier-pigeon"})
    decision = s["deliverables"].get("decision")
    assert decision and "unknown request source" in decision["reason"], decision
    assert "work_item" not in s["deliverables"]
    ok("unroutable source: fallback records the reason; nothing spawned")


def test_partial_services_reject_per_request() -> None:
    # A host with github.access but NO operator.interactive: interactive requests
    # are rejected on request satisfaction; the workflow itself still constructs.
    services = ServiceRegistry([bw_services.CannedGitHubIssueAccess(ISSUE_FIXTURE)])
    s = bw.run_mock(JOB, TRIAGE, request={"source": "interactive", "fields": {}}, services=services)
    decision = s["deliverables"].get("decision")
    assert decision and decision["unmet"] == ["service:operator.interactive"], decision
    # ...while a github request on the same host sails through.
    s2 = bw.run_mock(JOB, TRIAGE, request={"source": "github", "repo": "o/r", "issue": 7}, services=services)
    assert s2["deliverables"].get("work_item"), s2["deliverables"].keys()
    ok("per-request satisfaction: partial hosts take what they can, reject what they can't")


def test_legacy_job_synthesis() -> None:
    # Existing callers pass only job/triage: a job-source request is synthesized
    # from the job, the seed accepts, and the full lifecycle runs as before.
    s = bw.run_mock(JOB, TRIAGE)
    wi = s["deliverables"].get("work_item")
    assert wi and wi["source"] == "job" and wi["title"] == JOB["title"], wi
    assert "plan" in s["deliverables"]
    ok("legacy path: job/triage-only callers get a synthesized, accepted request")


def main() -> int:
    test_github_intake()
    test_interactive_intake_leaves_needs_open()
    test_job_intake_accepted()
    test_job_intake_rejected_terminates_early()
    test_unknown_source_falls_back()
    test_partial_services_reject_per_request()
    test_legacy_job_synthesis()
    print(f"\nseed: {CHECKS['n']} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
