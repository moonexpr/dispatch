"""foundation.kernel — the kernel INTERFACE layer (contracts only).

The kernel boots a request and routes it to a *runtime engine* (a Session). This
package declares the contracts — ``Session``, ``BootSpec``, ``SessionKind`` and the
daemon interface — and NOTHING that runs. Concrete runtime engines live OUTSIDE
``foundation`` (the interface-driven paradigm: foundation owns interfaces; the
repo-root ``kernel/`` package owns implementations, e.g. ``kernel/adhoc_session.py``).
"""
from __future__ import annotations

from foundation.kernel.session import BootSpec, Session, SessionKind

__all__ = ["BootSpec", "Session", "SessionKind"]
