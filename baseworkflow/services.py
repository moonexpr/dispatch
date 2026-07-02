#!/usr/bin/env python3
"""services.py — baseworkflow's concrete services (ADR-003).

``foundation.services`` owns the mechanism (facets, registry, provisions); this
module owns the domains the seed intake needs: read-only GitHub access (the
``gh`` CLI, matching the kernel's read discipline) and the operator's
interactive surface (a TTY prompt). ``build_services`` is the real family the
kernel registers; ``build_mock_services`` is the offline family the tests and
the CI validator use — same provision surface, canned data, zero network.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, Optional

from foundation.services import (
    AccessService,
    InteractiveService,
    ScriptedInteractiveService,
    ServiceRegistry,
)


class GitHubIssueAccess(AccessService):
    """Read-only issue access via ``gh`` (reads are always allowed — the same
    discipline as the kernel's job fetch). Issue text is untrusted input:
    carried as data, never executed (HANDOFF §8)."""

    domain = "github"

    def fetch_issue(self, repo: str, number: Any) -> Dict[str, Any]:
        out = subprocess.check_output(
            ["gh", "issue", "view", str(number), "-R", repo, "--json", "number,title,body,labels"],
            text=True,
        )
        d = json.loads(out)
        return {
            "number": d.get("number"),
            "title": d.get("title") or "",
            "body": d.get("body") or "",
            "labels": [lbl["name"] for lbl in (d.get("labels") or [])],
        }


class CannedGitHubIssueAccess(GitHubIssueAccess):
    """The mock member of the github.access family: issues come from a fixture
    keyed by issue number — no ``gh``, no network."""

    def __init__(self, issues: Optional[Dict[int, Dict[str, Any]]] = None) -> None:
        super().__init__()
        self._issues = dict(issues or {})

    def fetch_issue(self, repo: str, number: Any) -> Dict[str, Any]:
        issue = self._issues.get(int(number))
        if issue is None:
            raise KeyError(f"no canned issue {number} for {repo}")
        return dict(issue)


class OperatorInteractive(InteractiveService):
    """The operator's conversational surface: one question per missing field on
    the controlling TTY. Declining (empty answer / EOF) returns ``default`` —
    an interactive request may leave requirements open."""

    domain = "operator"

    def ask(self, prompt: str, *, key: str = "", choices: Any = None, default: Any = None) -> Any:
        try:
            answer = input(f"{prompt} ").strip()
        except EOFError:
            return default
        return answer or default


def build_services() -> ServiceRegistry:
    """The real service family (composition root: the kernel session)."""
    return ServiceRegistry([GitHubIssueAccess(), OperatorInteractive()])


def build_mock_services(
    issues: Optional[Dict[int, Dict[str, Any]]] = None,
    answers: Optional[Dict[str, Any]] = None,
) -> ServiceRegistry:
    """The offline family — canned issues, scripted answers. Used by the tests
    and the CI validator (same provision surface as :func:`build_services`)."""
    return ServiceRegistry(
        [CannedGitHubIssueAccess(issues), ScriptedInteractiveService("operator", answers)]
    )
