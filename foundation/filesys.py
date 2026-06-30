#!/usr/bin/env python3
"""filesys.py — a file-access facade rooted at the app directory.

Every durable resource callers read (the config YAMLs in ``app/config/``, agent
persona files, and any packaged data added later) is reached through this one
module instead of each consumer re-deriving its own
``os.path.join(os.path.dirname(__file__), "..", ...)`` dance. Centralising the
path math here means the on-disk layout — where ``app/`` lives and what sits
under it — is described in exactly ONE place: a future mover edits this file,
not every loader.

Root resolution
---------------
The root is the **app directory** — ``<repo>/app`` by default, overridable with
``$DISPATCH_APP_DIR`` so the tree can be relocated (tests, containers, an
installed copy) without code changes. A *relative* path resolves under that
root; an *absolute* path is honoured as-is. The absolute passthrough is what
lets per-file override env vars (``DISPATCH_SELECTION_WEIGHTS``,
``DISPATCH_WORKPLAN_RULES``) hand in paths outside the app tree exactly
as they did before this facade existed.

Layering
--------
``foundation`` is domain-agnostic; this is the one module that points it at
``app/`` — a deliberate, named seam (``foundation/models.py`` already reached
into the app's config; centralising the path math here makes that coupling
explicit and overridable rather than scattered). It is a **leaf** module: stdlib
+ an in-function PyYAML import only, with no imports from other
``foundation.*`` modules, so anything may import it without cycle risk and
without forcing PyYAML on text/bytes callers.
"""
from __future__ import annotations

import json
import os
from typing import IO, Any

# repo root = parent of foundation/ ; the app dir is <repo>/app unless overridden.
_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_ENGINE_DIR)
_DEFAULT_APP_DIR = os.path.join(_REPO_ROOT, "app")


def app_dir() -> str:
    """Absolute path to the app directory (``$DISPATCH_APP_DIR`` or ``<repo>/app``).

    Read from the environment on every call so tests/containers can repoint the
    tree at runtime without re-importing.
    """
    return os.environ.get("DISPATCH_APP_DIR") or _DEFAULT_APP_DIR


def resolve(relpath: str) -> str:
    """Absolute on-disk path for ``relpath``, anchored at the app dir.

    An absolute ``relpath`` is returned unchanged — this is the seam the
    per-file override env vars rely on to hand in a path outside the app tree.
    """
    if os.path.isabs(relpath):
        return relpath
    return os.path.join(app_dir(), relpath)


def exists(relpath: str) -> bool:
    """Does the resolved path exist on disk?"""
    return os.path.exists(resolve(relpath))


def open_file(relpath: str, mode: str = "r", **kwargs) -> IO[Any]:
    """``open()`` a file resolved under the app dir (raw escape hatch)."""
    return open(resolve(relpath), mode, **kwargs)


def read_text(relpath: str, *, encoding: str = "utf-8") -> str:
    """Return the full text of a file under the app dir."""
    with open(resolve(relpath), "r", encoding=encoding) as fh:
        return fh.read()


def read_bytes(relpath: str) -> bytes:
    """Return the raw bytes of a file under the app dir."""
    with open(resolve(relpath), "rb") as fh:
        return fh.read()


def read_yaml(relpath: str) -> Any:
    """Parse a YAML file under the app dir with ``yaml.safe_load``.

    Mirrors the codebase's universal ``yaml.safe_load(fh) or {}`` idiom: an
    empty document yields ``{}``. ``safe_load`` never executes arbitrary tags.
    Raises ``FileNotFoundError`` for a missing file — callers that tolerate that
    (the selection-weights / workplan-rules loaders) keep their own try/except
    so they degrade to in-code defaults rather than crashing.
    """
    import yaml  # local import: text/bytes callers shouldn't need PyYAML present
    with open(resolve(relpath), "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def read_json(relpath: str) -> Any:
    """Parse a JSON file under the app dir."""
    with open(resolve(relpath), "r", encoding="utf-8") as fh:
        return json.load(fh)
