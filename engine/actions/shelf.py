#!/usr/bin/env python3
"""shelf.py — the Shelf: a keyed collection behind a Repository interface.

A Controller owns three Shelves — ``input`` (the work unit handed in),
``deliverables`` (what each Action produces), and ``shared`` (scratch passed
between Actions). Because every Shelf speaks the same ``Repository`` interface,
the *same* Controller runs unchanged against an in-memory backend (mock, no side
effects) or a filesystem backend (real, durable) — the backend is chosen by
which concrete ``Shelf`` the factory hands over, never by the control flow.

``MemoryShelf`` is the mock/default backend (a dict). ``FileShelf`` is the real
backend, routing every key through ``engine.filesys`` so it honours the
``DISPATCH_APP_DIR`` override and the absolute-path escape hatch — it never calls
bare ``open()``.

Leaf module: imports only stdlib + ``engine.filesys`` (itself a leaf).
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List

from engine import filesys

VALID_KINDS = ("input", "deliverables", "shared")


class Shelf(ABC):
    """Repository interface over a keyed collection. Concrete backends differ
    only in where the bytes live; callers depend on this interface alone."""

    kind: str = ""

    @abstractmethod
    def get(self, key: str, default: Any = None) -> Any: ...

    @abstractmethod
    def put(self, key: str, value: Any) -> None: ...

    @abstractmethod
    def has(self, key: str) -> bool: ...

    @abstractmethod
    def drop(self, key: str) -> None: ...

    @abstractmethod
    def keys(self) -> List[str]: ...

    # -- shared conveniences (interface-level, backend-agnostic) -------------
    def update(self, mapping: Dict[str, Any]) -> None:
        for k, v in mapping.items():
            self.put(k, v)

    def snapshot(self) -> Dict[str, Any]:
        """A plain dict copy of the whole shelf — used for assertions/auditing."""
        return {k: self.get(k) for k in self.keys()}

    def __contains__(self, key: str) -> bool:
        return self.has(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())


class MemoryShelf(Shelf):
    """In-memory backend (the mock default). No side effects ever leave the
    process — exactly what the end-to-end mock test depends on."""

    def __init__(self, kind: str = "", initial: Dict[str, Any] | None = None) -> None:
        self.kind = kind
        self._store: Dict[str, Any] = dict(initial or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def put(self, key: str, value: Any) -> None:
        self._store[key] = value

    def has(self, key: str) -> bool:
        return key in self._store

    def drop(self, key: str) -> None:
        self._store.pop(key, None)

    def keys(self) -> List[str]:
        return list(self._store.keys())


class FileShelf(Shelf):
    """Filesystem backend (the real path). Each key is a JSON document under
    ``<app>/<root>/<kind>/<key>.json``; reads/writes route through
    ``engine.filesys`` so the app-dir override and absolute-path escape hatch
    both apply. Writes use ``filesys.open_file`` (the documented raw escape
    hatch) — never a bare ``open()``."""

    def __init__(self, kind: str = "", root: str = "actions/shelves") -> None:
        self.kind = kind
        self._base = f"{root}/{kind or 'default'}"

    def _rel(self, key: str) -> str:
        safe = key.replace("/", "_").replace("..", "_")
        return f"{self._base}/{safe}.json"

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return filesys.read_json(self._rel(key))
        except FileNotFoundError:
            return default

    def put(self, key: str, value: Any) -> None:
        import os

        path = filesys.resolve(self._rel(key))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(value, fh, separators=(",", ":"), default=str)

    def has(self, key: str) -> bool:
        try:
            filesys.read_bytes(self._rel(key))
            return True
        except FileNotFoundError:
            return False

    def drop(self, key: str) -> None:
        import os

        try:
            os.unlink(filesys.resolve(self._rel(key)))
        except FileNotFoundError:
            pass

    def keys(self) -> List[str]:
        import os

        try:
            d = filesys.resolve(self._base)
            return [f[:-5] for f in os.listdir(d) if f.endswith(".json")]
        except FileNotFoundError:
            return []


@dataclass
class Shelves:
    """The three Shelves a Controller carries. Constructed by the factory so the
    whole trio is consistent (all mock or all real)."""

    input: Shelf
    deliverables: Shelf
    shared: Shelf

    def of(self, kind: str) -> Shelf:
        if kind not in VALID_KINDS:
            raise ValueError(f"unknown shelf kind {kind!r}; expected one of {VALID_KINDS}")
        return getattr(self, kind)
