#!/usr/bin/env python3
"""foundation.agents — the abstract agent foundation.

Houses :class:`BaseAgent` (the abstract archetype) and :class:`AgentOutcome` (its
uniform, never-raising result). Concrete role archetypes are supplied by higher
layers and subclass :class:`BaseAgent`. The execution backends are in
:mod:`foundation.agent_sdk`.
"""
from __future__ import annotations

from foundation.agents.base_agent import AgentOutcome, BaseAgent

__all__ = ["BaseAgent", "AgentOutcome"]
