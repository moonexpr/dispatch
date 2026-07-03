"""foundation.service_container — a minimal, typed dependency-injection container.

The composition root registers a concrete implementation against its INTERFACE (an
abstract base class); call sites RESOLVE the interface and stay ignorant of the concrete
class. This is the seam that lets ``foundation`` declare contracts (e.g.
``foundation.workflow.tuning.Tuning``) while the implementations live elsewhere
(``baseworkflow.tuning.YamlTuning``) and are wired in exactly once.

    from foundation.service_container import services
    services.register(Tuning, YamlTuning())     # composition root, once
    tuning = services.resolve(Tuning)            # any call site

A provider is either a ready instance (registered via :meth:`register`) or a zero-arg
factory (via :meth:`register_factory`, resolved lazily and cached as a singleton unless
``singleton=False``). Registration and resolution are thread-safe. ``foundation`` never
imports its implementers, so the dependency direction stays one-way (interface ← impl).
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional, Tuple, Type, TypeVar

T = TypeVar("T")
Factory = Callable[[], Any]


class ServiceError(RuntimeError):
    """No provider is registered for a requested interface."""


class ServiceContainer:
    """Maps an interface type to its provider. Resolve returns the singleton instance."""

    def __init__(self) -> None:
        # interface -> (instance | factory, is_factory, singleton)
        self._providers: Dict[type, Tuple[Any, bool, bool]] = {}
        self._instances: Dict[type, Any] = {}
        self._lock = threading.RLock()

    def register(self, interface: Type[T], instance: T) -> None:
        """Register a ready instance as the singleton for ``interface`` (replaces any
        prior registration)."""
        with self._lock:
            self._providers[interface] = (instance, False, True)
            self._instances[interface] = instance

    def register_factory(self, interface: Type[T], factory: Factory, *, singleton: bool = True) -> None:
        """Register a zero-arg factory for ``interface``, invoked on first resolve.
        Cached as a singleton unless ``singleton=False`` (then invoked per resolve)."""
        with self._lock:
            self._providers[interface] = (factory, True, singleton)
            self._instances.pop(interface, None)

    def has(self, interface: type) -> bool:
        with self._lock:
            return interface in self._providers

    def resolve(self, interface: Type[T]) -> T:
        """Return the instance registered for ``interface``; raise :class:`ServiceError`
        if none is registered. Lazily builds + caches a factory's singleton."""
        with self._lock:
            if interface in self._instances:
                return self._instances[interface]
            entry = self._providers.get(interface)
            if entry is None:
                raise ServiceError(
                    f"no provider registered for {getattr(interface, '__name__', interface)!r} "
                    f"— the composition root must register it first"
                )
            provider, is_factory, singleton = entry
            instance = provider() if is_factory else provider
            if singleton:
                self._instances[interface] = instance
            return instance

    def try_resolve(self, interface: Type[T]) -> Optional[T]:
        """Resolve, or return None when nothing is registered (no raise)."""
        return self.resolve(interface) if self.has(interface) else None

    def reset(self) -> None:
        """Drop all registrations (test isolation)."""
        with self._lock:
            self._providers.clear()
            self._instances.clear()


#: The process-wide default container. The composition root populates it; call sites
#: resolve from it. Tests may construct a private :class:`ServiceContainer` instead.
services = ServiceContainer()
