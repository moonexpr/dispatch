"""foundation.workflow.tuning — the tuning INTERFACE (contract only).

Tuning governs WORK SELECTION (which issue is picked) and PROMPT GENERATION (how the
work order reads). Those values live in one editable YAML surface; this module declares
the *contract* a workflow depends on, and nothing that reads YAML. The concrete,
YAML-backed implementation lives in ``baseworkflow.tuning`` (the interface-driven
paradigm: foundation owns contracts, baseworkflow owns implementations), registered into
the :mod:`foundation.service_container` and resolved here via :func:`get_tuning`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Tuple

from foundation.service_container import services


class Tuning(ABC):
    """The tunable surface consumers depend on. Implementations resolve the values from
    config (e.g. ``app/config/tuning.yml``); call sites never read the file directly."""

    # -- behaviour (selection / routing) ------------------------------------
    @abstractmethod
    def spec_for(self, path: str) -> Tuple[str, str]:
        """Map a file/area to a ``(function, domain)`` engineering specialization."""

    @abstractmethod
    def route_for_scope(self, scope: str) -> str:
        """Classifier scope -> generation model group (an unknown scope -> default)."""

    @abstractmethod
    def tier_for_attempt(self, attempt: Any) -> str:
        """Fix-ladder: a fix attempt -> model tier (past the last rung -> needs-human)."""

    # -- generation / decomposition values ----------------------------------
    @property
    @abstractmethod
    def scope_budget(self) -> Dict[str, int]: ...
    @property
    @abstractmethod
    def phase_split(self) -> Tuple[Tuple[str, float], ...]: ...
    @property
    @abstractmethod
    def swarm_max(self) -> int: ...
    @property
    @abstractmethod
    def decompose_complexity(self) -> Dict[str, Any]: ...
    @property
    @abstractmethod
    def decompose_templates(self) -> Dict[str, str]: ...
    @property
    @abstractmethod
    def decompose_features(self) -> Dict[str, Any]: ...
    @property
    @abstractmethod
    def decompose_research(self) -> Dict[str, Any]: ...
    @property
    @abstractmethod
    def res_caps(self) -> Dict[str, int]: ...
    @property
    @abstractmethod
    def intake_caps(self) -> Dict[str, int]: ...

    # -- routing / recovery scalars -----------------------------------------
    @property
    @abstractmethod
    def recovery_reaper_timeout_hours(self) -> float: ...
    @property
    @abstractmethod
    def confidence_threshold(self) -> float: ...
    @property
    @abstractmethod
    def fix_attempt_cap(self) -> int: ...
    @property
    @abstractmethod
    def needs_human_route(self) -> str: ...


def get_tuning() -> Tuning:
    """Resolve the registered :class:`Tuning` from the service container. The composition
    root (``baseworkflow`` on import) registers the concrete implementation; importing any
    ``baseworkflow`` module guarantees it is wired before resolution."""
    return services.resolve(Tuning)
