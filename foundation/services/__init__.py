#!/usr/bin/env python3
"""foundation.services — standard API services, split into access and
interactive facets (mechanism only; concrete domains live with consumers).

See ADR-003 and :mod:`foundation.services.service` for the contract.
"""
from __future__ import annotations

from .mock import ScriptedInteractiveService
from .service import (
    ACCESS,
    INTERACTIVE,
    VALID_FACETS,
    AccessService,
    InteractiveService,
    Service,
    ServiceRegistry,
    UnknownService,
)

__all__ = [
    "ACCESS", "INTERACTIVE", "VALID_FACETS",
    "Service", "AccessService", "InteractiveService",
    "ServiceRegistry", "UnknownService",
    "ScriptedInteractiveService",
]
