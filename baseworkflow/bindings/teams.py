#!/usr/bin/env python3
"""bindings/teams — the ``teams:`` namespace: the agent-team roster.

``teams:roster`` enumerates the AVAILABLE agent teams from the declarative
manifests under ``app/config/teams/*.yml`` (versioned, offline-testable — the
same #base discipline as the action manifests). The Architect's
``architect:assign_teams`` step matches each work-plan slice against this
roster; a slice with no matching team gets a dedicated team-CREATION slice
whose deliverable is literally "add a manifest here".

Team manifest shape (one file per team):

    name: general-engineering
    description: Prototyper + tester pair for feature slices.
    capabilities: [general software, backend, python, testing]
    agents:
      - {role: prototyper, archetype: engineer}
      - {role: tester,     archetype: engineer}
"""
from __future__ import annotations

import glob
import os
from typing import Any, Dict, List

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEAMS_DIR = os.path.join(_ROOT, "app", "config", "teams")

# When no manifests exist yet, the roster degrades to the one staffing shape the
# orchestration already practices (#171): a prototyper + tester engineering pair.
_DEFAULT_TEAMS: List[Dict[str, Any]] = [{
    "name": "general-engineering",
    "description": "Prototyper + tester pair for general feature slices (#171 staffing).",
    "capabilities": ["general software", "backend", "frontend", "web", "python", "testing"],
    "agents": [
        {"role": "prototyper", "archetype": "engineer"},
        {"role": "tester", "archetype": "engineer"},
    ],
}]


def _load_team(path: str) -> Dict[str, Any]:
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    if not isinstance(doc, dict) or not doc.get("name"):
        raise ValueError(f"{path}: a team manifest needs at least a 'name'")
    return {
        "name": str(doc["name"]),
        "description": str(doc.get("description", "")),
        "capabilities": [str(c).lower() for c in (doc.get("capabilities") or ())],
        "agents": list(doc.get("agents") or ()),
    }


def roster(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Enumerate the available agent teams (manifest-backed; defaulted when the
    directory is empty/missing so offline + CI never depend on operator config)."""
    teams: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(TEAMS_DIR, "*.yml"))):
        try:
            teams.append(_load_team(path))
        except Exception as exc:  # noqa: BLE001 — one bad manifest never hides the rest
            teams.append({"name": os.path.basename(path), "error": str(exc),
                          "capabilities": [], "agents": []})
    source = "config"
    if not teams:
        teams, source = list(_DEFAULT_TEAMS), "default"
    return {"teams": {"teams": teams, "source": source, "dir": TEAMS_DIR}}


def register(reg: Any) -> None:
    reg.register_action("teams_roster", roster)
