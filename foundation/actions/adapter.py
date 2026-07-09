#!/usr/bin/env python3
"""adapter.py — format coercion between an Action's plain-text channel and
structured data.

An Action's uniform channel is text. When a step needs structured I/O
(JSON or YAML payloads), an Adapter sits between the text and the object:
``decode(text) -> obj`` on the way in, ``encode(obj) -> text`` on the way out.

Format coercion is backend-agnostic — JSON is JSON whether the upstream output
came from a mock fixture or a real model — so the *same* Adapter instances serve
both factories. The factory still vends them (``factory.adapter(fmt)``) so the
object family stays consistent, but Mock and Real return identical adapters.

Leaf module: stdlib + PyYAML (already a dependency via foundation.filesys).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from .result import ActionError

VALID_FORMATS = ("text", "json", "yaml")


def _salvage_json(s: str) -> Any:
    """Best-effort extract a JSON object/array embedded in prose or code fences —
    models often wrap the payload. Returns the parsed value, or ``None`` if nothing
    parseable is found."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = s.find(opener), s.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(s[start:end + 1])
            except ValueError:
                continue
    return None


class Adapter(ABC):
    """Two-way coercion between text and a structured object."""

    format: str = ""

    @abstractmethod
    def encode(self, obj: Any) -> str: ...

    @abstractmethod
    def decode(self, text: str) -> Any: ...


class TextAdapter(Adapter):
    """Identity adapter — the default. Text in, text out."""

    format = "text"

    def encode(self, obj: Any) -> str:
        return obj if isinstance(obj, str) else str(obj)

    def decode(self, text: str) -> Any:
        return text


class JsonAdapter(Adapter):
    """Compact JSON, matching the repo's greppable ``separators=(',', ':')``
    ledger convention so encoded payloads byte-match across runs."""

    format = "json"

    def encode(self, obj: Any) -> str:
        if isinstance(obj, str):
            return obj
        return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)

    def decode(self, text: str) -> Any:
        if not isinstance(text, str):
            return text
        s = text.strip()
        # An empty model response carries no structured payload. Fail SAFE to an
        # empty object rather than crashing the whole tick with a bare
        # JSONDecodeError — the inference layer's failures-as-data contract. (This
        # is the common real failure: a model that times out / returns nothing.)
        if not s:
            return {}
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            # Models often wrap the payload in prose or ``` fences — salvage the
            # embedded object/array before giving up.
            salvaged = _salvage_json(s)
            if salvaged is not None:
                return salvaged
            # Genuinely non-JSON, non-empty output: surface a HANDLED ActionError so
            # the leaf boundary records a clean failure (not an uncaught crash).
            raise ActionError(
                f"json adapter: model output is not valid JSON "
                f"({len(s)} chars, starts {s[:60]!r})"
            )


class YamlAdapter(Adapter):
    """YAML for human-facing structured deliverables."""

    format = "yaml"

    def encode(self, obj: Any) -> str:
        import yaml

        if isinstance(obj, str):
            return obj
        return yaml.safe_dump(obj, sort_keys=True, default_flow_style=False)

    def decode(self, text: str) -> Any:
        import yaml

        if not isinstance(text, str):
            return text
        return yaml.safe_load(text)


def adapter_for(fmt: str) -> Adapter:
    """Resolve a format name to its Adapter. Shared by both factories."""
    table = {"text": TextAdapter, "json": JsonAdapter, "yaml": YamlAdapter}
    try:
        return table[fmt]()
    except KeyError:
        raise ValueError(f"unknown adapter format {fmt!r}; expected one of {VALID_FORMATS}")
