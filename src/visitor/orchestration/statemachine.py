"""statemachine.py — the dispatch label vocabulary, as a finite state machine.

The label set + lifecycle transitions are DATA, declared in
``app/config/state_machine.yml``, not Python. This module loads that YAML
(through the ``engine.filesys`` facade) and assembles an
:class:`engine.structures.StateMachine` from it via the engine's Builder (a
creational pattern), then exposes the label helpers the pipeline needs.

Keeping the state in YAML and the mechanism in ``engine.structures`` keeps
pipeline state out of the engine — the engine knows *how* a state machine works,
not *which* states this pipeline has.
"""
from __future__ import annotations

import functools
from typing import Dict, List

from engine import filesys, structures


@functools.lru_cache(maxsize=1)
def _config() -> dict:
    return filesys.read_yaml("config/state_machine.yml")


@functools.lru_cache(maxsize=1)
def machine() -> "structures.StateMachine":
    """The dispatch lifecycle FSM, built from ``state_machine.yml``."""
    return structures.StateMachineBuilder().from_mapping(_config()).build()


def _states() -> Dict[str, dict]:
    return _config().get("states") or {}


def all_pipeline_labels() -> List[str]:
    """Every label name, ordered lifecycle-ladder -> flags -> fix-ladder.

    This is the order ``bootstrap_labels`` creates them in (and smoke depends on).
    """
    m = machine()
    return [*m.states_where(kind="state"),
            *m.states_where(kind="flag"),
            *m.states_where(kind="fix")]


def label_color(name: str) -> str:
    return (_states().get(name) or {}).get("color", "ededed")


def label_desc(name: str) -> str:
    return (_states().get(name) or {}).get("desc", "pipeline label")
