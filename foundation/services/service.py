#!/usr/bin/env python3
"""service.py — the standard API service layer actions derive from (mechanism).

Every service domain (``github``, ``operator``, ``job``, …) presents up to two
**facets**, deliberately split (ADR-003) so each half is independently mockable,
independently provisioned, and independently *needed*:

  * ``AccessService`` (facet ``access``) — programmatic resource access: pure
    request/response against an external surface (an API, a queue, a
    filesystem). No human in the loop.
  * ``InteractiveService`` (facet ``interactive``) — a conversational surface
    that can *acquire* missing information at run time via :meth:`ask` — which
    is why an interactive request may start with unfulfilled requirements and
    still proceed.

A service's identity is ``<domain>.<facet>`` — exactly the name a
``service:``-kind :class:`~foundation.needs.Need` declares — and each service
grants the matching :class:`~foundation.needs.Provision`. The
:class:`ServiceRegistry` is the string-addressable provision surface: its
aggregate :meth:`~ServiceRegistry.provisions` feeds static satisfaction checks,
and carried on the run ``Context`` (``ctx.services``) it is the runtime
counterpart — ``ctx.service("github.access")`` fails in the needs vocabulary
when the environment does not provide what an action declared.

Concrete services live with consumers (e.g. ``seedwf/services.py``), mirroring
how ``TokenRegistry`` bindings work: ``foundation`` owns the mechanism, never a
domain implementation.

Leaf module: stdlib + ``foundation.needs`` (itself a stdlib leaf).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from foundation.needs import KIND_SERVICE, Provision

ACCESS = "access"
INTERACTIVE = "interactive"
VALID_FACETS = (ACCESS, INTERACTIVE)


class UnknownService(RuntimeError):
    """A service id was requested but never registered — an unmet service need."""

    def __init__(self, sid: str, available) -> None:
        self.sid = sid
        self.available = sorted(available)
        known = ", ".join(self.available) or "(none)"
        super().__init__(f"unmet need service:{sid} — registered services: {known}")


class Service(ABC):
    """Base identity: a ``domain`` plus a ``facet`` (fixed by the subclass
    family). ``provisions`` is what registering this service offers to
    satisfaction checks; ``probe`` is the readiness seam (default: registered
    means ready) for a future liveness-aware validator."""

    domain: str = ""
    facet: str = ""

    def __init__(self, domain: Optional[str] = None) -> None:
        if domain:
            self.domain = domain
        if not self.domain:
            raise ValueError(f"{type(self).__name__} needs a service domain")
        if self.facet not in VALID_FACETS:
            raise ValueError(f"{type(self).__name__} facet {self.facet!r} not in {VALID_FACETS}")

    @property
    def id(self) -> str:
        return f"{self.domain}.{self.facet}"

    def provisions(self) -> Tuple[Provision, ...]:
        return (Provision(KIND_SERVICE, self.id, source=type(self).__name__),)

    def probe(self) -> bool:
        """Readiness check (reserved seam). Registered services default to ready."""
        return True

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<{type(self).__name__} service:{self.id}>"


class AccessService(Service):
    """Programmatic resource access. Concrete subclasses add the domain's own
    request/response methods (``fetch_issue``, ``list_jobs``, …)."""

    facet = ACCESS


class InteractiveService(Service):
    """A conversational surface. The defining capability is :meth:`ask` — the
    run-time acquisition of information no other provision supplied."""

    facet = INTERACTIVE

    @abstractmethod
    def ask(self, prompt: str, *, key: str = "", choices: Any = None, default: Any = None) -> Any:
        """Put one question to the interacting party and return the answer.
        ``key`` names the field being resolved (drives scripted mocks);
        ``default`` is returned when the party declines to answer."""
        raise NotImplementedError


class ServiceRegistry:
    """String-addressable (``domain.facet``) service registration and lookup.
    Registered by the composition root (a kernel session, a test harness),
    carried on the run ``Context``, and consulted — via :meth:`provisions` —
    by the static workflow validator."""

    def __init__(self, services: Any = ()) -> None:
        self._services: Dict[str, Service] = {}
        for s in services or ():
            self.register(s)

    def register(self, service: Service) -> Service:
        """Register (or replace) the service under its ``domain.facet`` id."""
        self._services[service.id] = service
        return service

    def get(self, sid: str) -> Service:
        try:
            return self._services[sid]
        except KeyError:
            raise UnknownService(sid, self._services.keys())

    def has(self, sid: str) -> bool:
        return sid in self._services

    def ids(self) -> List[str]:
        return sorted(self._services)

    def provisions(self) -> Tuple[Provision, ...]:
        out: List[Provision] = []
        for sid in sorted(self._services):
            out.extend(self._services[sid].provisions())
        return tuple(out)
