"""foundation.kernel.session — the runtime-engine interface (interface only).

The kernel bootstraps the environment, builds a :class:`BootSpec`, and routes it to a
:class:`Session` — a *runtime engine* that executes the request. Two kinds:

  * ``ADHOC``  — runs the request to completion and **terminates** (one dispatch tick).
  * ``DAEMON`` — a **long-lived** process fronting a networking service (e.g. a web
                 server). Interface only for now — see :mod:`foundation.kernel.daemon`.

This module is pure contract: an abstract :class:`Session`, the :class:`BootSpec` data
carried into it, and the :class:`SessionKind` discriminant. Concrete engines implement
:class:`Session` in the repo-root ``kernel/`` package, never here.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SessionKind(Enum):
    """Which runtime lifecycle a request needs."""

    ADHOC = "adhoc"    # run to completion, then terminate
    DAEMON = "daemon"  # long-lived, fronts a networking service


@dataclass(frozen=True)
class BootSpec:
    """The bootstrapped request the kernel routes to a :class:`Session` — everything a
    runtime engine needs to execute, and nothing about *how* it runs. Issue text and
    other GitHub-sourced fields are untrusted data (HANDOFF §8); a Session carries them
    as data, never as instructions."""

    repo: str
    issue: Optional[int] = None
    live: bool = False
    verbose: bool = False
    engine: str = "baseworkflow"          # baseworkflow | websitewf
    kind: SessionKind = SessionKind.ADHOC


class Session(ABC):
    """A runtime engine that executes ONE :class:`BootSpec`. The implementation owns the
    lifecycle — adhoc runs and returns; a daemon serves until shutdown — so the kernel
    router stays lifecycle-agnostic: it picks the Session and calls :meth:`run`."""

    #: The lifecycle this Session implements; the router matches it to the request.
    kind: SessionKind

    def __init__(self, spec: BootSpec) -> None:
        self.spec = spec

    @abstractmethod
    def run(self) -> int:
        """Execute the session and return a process exit code (0 = success)."""
        raise NotImplementedError
