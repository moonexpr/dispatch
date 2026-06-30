"""foundation.kernel.daemon — interface for the long-lived (daemon) runtime.

A daemon :class:`Session` fronts a networking service (a web server) and stays alive
serving requests instead of running one tick and terminating. This is INTERFACE ONLY:
no concrete implementation ships yet and the kernel router does not route to it.

TODO(John): design the daemon/web-server surface before adding a concrete
implementation under the repo-root ``kernel/`` package — bind address/port, the
request-handler contract, health/readiness probes, and graceful-shutdown semantics.
"""
from __future__ import annotations

from abc import abstractmethod

from foundation.kernel.session import Session, SessionKind


class DaemonSession(Session):
    """Long-lived runtime that fronts a networking service. Interface only.

    A concrete daemon would bind a listener, serve requests until signalled, and shut
    down gracefully. None of that exists yet — see the module ``TODO(John)``."""

    kind = SessionKind.DAEMON

    @abstractmethod
    def serve(self) -> int:
        """Start the networking service and block until shutdown, returning an exit code.

        TODO(John): define the web-server interface this method fulfils — the listener
        (bind host/port), the request-handler contract, and graceful shutdown. Stubbed
        until the daemon path is needed.
        """
        raise NotImplementedError

    def run(self) -> int:  # pragma: no cover - stub until a concrete daemon exists
        # The router would call run() -> serve() once a concrete DaemonSession lands.
        raise NotImplementedError("daemon runtime not implemented yet — TODO(John)")
