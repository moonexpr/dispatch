#!/usr/bin/env python3
"""agents — backend-agnostic agent archetypes (the abstraction side of a Bridge).

Five concrete role definitions — :class:`EngineerAgent`, :class:`ArchitectAgent`,
:class:`AdminAgent`, :class:`FileResearcher`, :class:`WebResearcher` — each a
reusable profile (persona + tool access + default route)
you **puppet** by passing a per-session prompt. The execution backend (the ``claude``
CLI, the Agent SDK, or a single-shot chat model via OpenRouter / HuggingFace) is
chosen at ``run()`` time through :func:`foundation.agent_sdk.make_runner`; the definition
never changes. See :class:`foundation.agents.BaseAgent` for the abstract base and
:mod:`foundation.agent_sdk` for the backend layer.

Importable as ``agents`` because ``the repo root is on ``sys.path`` (the bindings-package
bootstrap in ``baseworkflow/bindings/__init__``).
"""
from __future__ import annotations

from typing import Dict, Type

from foundation.agents import AgentOutcome, BaseAgent

from agents.admin import AdminAgent
from agents.architect import ArchitectAgent
from agents.engineer import EngineerAgent
from agents.file_researcher import FileResearcher
from agents.web_researcher import WebResearcher

__all__ = ["BaseAgent", "AgentOutcome", "EngineerAgent", "ArchitectAgent", "AdminAgent",
           "FileResearcher", "WebResearcher", "make_agent"]

#: role name -> archetype class. Mirrors ``foundation.agent_sdk._RUNNERS`` (the backend
#: registry) on the definition side.
_ARCHETYPES: Dict[str, Type[Agent]] = {
    "engineer": EngineerAgent,
    "architect": ArchitectAgent,
    "admin": AdminAgent,
    "file-researcher": FileResearcher,
    "web-researcher": WebResearcher,
}


def make_agent(role: str, **kwargs) -> Agent:
    """Factory: instantiate the archetype for ``role`` (``"engineer"`` | ``"architect"``
    | ``"admin"``); ``kwargs`` pass through to the constructor (model/route/backend/
    tools/timeout/system_prompt overrides). Unknown roles raise ``KeyError`` — an
    archetype must be registered, never guessed."""
    return _ARCHETYPES[role.strip().lower()](**kwargs)
