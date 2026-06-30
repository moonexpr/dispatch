#!/usr/bin/env python3
"""script.py — the serialized Program (orchestration script) and the InferenceSpec.

An *orchestration script* is a declarative, JSON-able description of an agent
team and how its phases wire together. A caller emits the script; later,
``factory.deserialize(...)`` rebuilds it into an executable ``Program``. The
script itself is pure data — no callables, no model handles — so it survives a
round-trip through any Shelf (including the JSON-backed ``FileShelf``). The
*runner* that actually fulfils each phase (mock oracle vs. real model) is
injected by the factory at deserialize time, never baked into the script. That
is what lets one script run mock or real unchanged.

``InferenceSpec`` is the runtime spec an ``Inference`` action carries. It is built
either directly by a caller or from a script ``AgentSpec`` at deserialize time.
It can carry an ``oracle`` (a deterministic stand-in the mock runner calls) or a
``fixture`` (a canned output) — these are the seams that let end-to-end mock tests
exercise real logic with no model and no side effects.

Leaf module: stdlib only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple


@dataclass
class InferenceSpec:
    """What an ``Inference`` action needs to run. ``oracle``/``fixture`` are the
    mock seams; ``prompt``/``system``/``model``/``tools`` drive the real path."""

    prompt: str = ""
    system: str = ""
    model: str = "gen-default"
    tools: Tuple[str, ...] = ()
    fmt: str = "text"
    max_tokens: int = 1024
    # Mock seams (never serialized into a script):
    oracle: Optional[Callable[[Any, Any], Any]] = None  # (payload, ctx) -> value
    fixture: Any = None
    mock_usage: int = 500  # deterministic token cost the mock runner reports

    def messages(self, payload: Any) -> list:
        """Render the spec + payload into a chat-messages list for the real
        backend. Phase handoff is explicit: the prior output is inlined as text."""
        msgs = []
        if self.system:
            msgs.append({"role": "system", "content": self.system})
        body = self.prompt
        if payload not in (None, ""):
            body = f"{body}\n\n--- prior output ---\n{payload}"
        msgs.append({"role": "user", "content": body})
        return msgs


@dataclass(frozen=True)
class AgentSpec:
    """The agent definition carried by an orchestration-script phase. Becomes an
    ``InferenceSpec`` (an AgentDefinition, in SDK terms) at deserialize time."""

    description: str = ""
    prompt: str = ""
    tools: Tuple[str, ...] = ()
    model: str = "gen-default"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "description": self.description,
            "prompt": self.prompt,
            "tools": list(self.tools),
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentSpec":
        return cls(
            description=d.get("description", ""),
            prompt=d.get("prompt", ""),
            tools=tuple(d.get("tools", ()) or ()),
            model=d.get("model", "gen-default"),
        )


@dataclass(frozen=True)
class PhaseSpec:
    """One phase of the orchestration script: an agent, its dependencies, whether
    it fans out in parallel, its share of the engineering budget, and the names of
    its abort predicates (compiled to hooks at deserialize time).

    ``slice_id`` groups the phases that belong to one work unit (one feature). A
    feature unit emits two phases — a prototyper and a tester — sharing the unit's
    ``slice_id`` so the wave executor can attribute both agents to the same slice
    and the Admin can consolidate a slice's commits together (issue #171). Defaults
    to ``""`` so older scripts round-trip unchanged."""

    id: str
    agent: AgentSpec
    depends_on: Tuple[str, ...] = ()
    parallel: bool = False
    budget: int = 0
    abort_when: Tuple[str, ...] = ()
    slice_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent.to_dict(),
            "depends_on": list(self.depends_on),
            "parallel": self.parallel,
            "budget": self.budget,
            "abort_when": list(self.abort_when),
            "slice_id": self.slice_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PhaseSpec":
        return cls(
            id=d["id"],
            agent=AgentSpec.from_dict(d.get("agent", {})),
            depends_on=tuple(d.get("depends_on", ()) or ()),
            parallel=bool(d.get("parallel", False)),
            budget=int(d.get("budget", 0)),
            abort_when=tuple(d.get("abort_when", ()) or ()),
            slice_id=str(d.get("slice_id", "") or ""),
        )


@dataclass(frozen=True)
class OrchestrationScript:
    """A serialized Program: an ordered set of phases plus the permission policy.
    ``allow_tools`` is the global allowlist the permission Governor enforces — any
    Program that spawns subagents must include ``Agent`` here."""

    phases: Tuple[PhaseSpec, ...] = ()
    permission: str = "deny-by-default"
    allow_tools: Tuple[str, ...] = ()
    budget: int = 0  # the total token budget; 0 -> fall back to the sum of per-phase budgets

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phases": [p.to_dict() for p in self.phases],
            "permission": self.permission,
            "allow_tools": list(self.allow_tools),
            "budget": self.budget,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OrchestrationScript":
        return cls(
            phases=tuple(PhaseSpec.from_dict(p) for p in d.get("phases", ())),
            permission=d.get("permission", "deny-by-default"),
            allow_tools=tuple(d.get("allow_tools", ()) or ()),
            budget=int(d.get("budget", 0)),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "OrchestrationScript":
        return cls.from_dict(json.loads(text))
