#!/usr/bin/env python3
"""mock.py — the mock member of the service family (test/dry surfaces).

Mirrors the ``MockActionFactory`` discipline: the same controller code runs
against a scripted conversational surface with zero real effects. Domain-shaped
access fakes (a canned ``fetch_issue`` …) belong next to the consumer that
defines the domain interface; only the generic interactive mock lives here.

Leaf module: stdlib + sibling service leaf.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .service import InteractiveService


class ScriptedInteractiveService(InteractiveService):
    """An :class:`InteractiveService` that answers from a script instead of a
    person. ``answers`` maps a field ``key`` to its answer (or a queue of
    answers); anything unscripted falls back to ``default``. The ``transcript``
    records every exchange for assertions."""

    def __init__(self, domain: str = "operator", answers: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(domain)
        self._answers: Dict[str, Any] = dict(answers or {})
        self.transcript: List[Tuple[str, str, Any]] = []  # (key, prompt, answer)

    def ask(self, prompt: str, *, key: str = "", choices: Any = None, default: Any = None) -> Any:
        answer = default
        if key in self._answers:
            scripted = self._answers[key]
            if isinstance(scripted, list):
                answer = scripted.pop(0) if scripted else default
            else:
                answer = scripted
        self.transcript.append((key, prompt, answer))
        return answer
