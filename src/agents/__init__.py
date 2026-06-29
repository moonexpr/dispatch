#!/usr/bin/env python3
"""agents — backend-agnostic agent archetypes (the abstraction side of a Bridge).

Three concrete role definitions — :class:`EngineerAgent`, :class:`ArchitectAgent`,
:class:`AdminAgent` — each a reusable profile (persona + tool access + default route)
you **puppet** by passing a per-session prompt. The execution backend (the ``claude``
CLI, the Agent SDK, or a single-shot chat model via OpenRouter / HuggingFace) is
chosen at ``run()`` time through :func:`engine.agent_sdk.make_runner`; the definition
never changes. See :class:`engine.agents.BaseAgent` for the abstract base and
:mod:`engine.agent_sdk` for the backend layer.

Importable as ``agents`` because ``src/`` is on ``sys.path`` (the bindings-package
bootstrap in ``src/baseworkflow/bindings/__init__``).
"""
from __future__ import annotations

from typing import Dict, Type

from engine.agents import AgentOutcome, BaseAgent

from agents.admin import AdminAgent
from agents.architect import ArchitectAgent
from agents.engineer import EngineerAgent

__all__ = ["BaseAgent", "AgentOutcome", "EngineerAgent", "ArchitectAgent", "AdminAgent",
           "make_agent"]

#: role name -> archetype class. Mirrors ``engine.agent_sdk._RUNNERS`` (the backend
#: registry) on the definition side.
_ARCHETYPES: Dict[str, Type[Agent]] = {
    "engineer": EngineerAgent,
    "architect": ArchitectAgent,
    "admin": AdminAgent,
}


def make_agent(role: str, **kwargs) -> Agent:
    """Factory: instantiate the archetype for ``role`` (``"engineer"`` | ``"architect"``
    | ``"admin"``); ``kwargs`` pass through to the constructor (model/route/backend/
    tools/timeout/system_prompt overrides). Unknown roles raise ``KeyError`` — an
    archetype must be registered, never guessed."""
    return _ARCHETYPES[role.strip().lower()](**kwargs)
