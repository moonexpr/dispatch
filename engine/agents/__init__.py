#!/usr/bin/env python3
"""engine.agents — the abstract agent foundation.

Houses :class:`BaseAgent` (the abstract archetype) and :class:`AgentOutcome` (its
uniform, never-raising result). Concrete role archetypes — ``EngineerAgent`` /
``ArchitectAgent`` / ``AdminAgent`` — live in the app layer at ``src/agents/`` and
subclass :class:`BaseAgent`. The execution backends are in :mod:`engine.agent_sdk`.
"""
from __future__ import annotations

from engine.agents.base_agent import AgentOutcome, BaseAgent

__all__ = ["BaseAgent", "AgentOutcome"]
