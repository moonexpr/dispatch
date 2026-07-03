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

VALID_FORMATS = ("text", "json", "yaml")


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
        return json.loads(text)


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
