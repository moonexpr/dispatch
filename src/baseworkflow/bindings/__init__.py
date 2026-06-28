#!/usr/bin/env python3
"""src.baseworkflow.bindings — the bind layer: action bodies + loop predicates.

This package is the entire "implement-with-code + bind" surface the YAML workflow
needs. It is organised one module per token namespace (mirroring
``app/config/actions/<namespace>/``) — ``github``, ``architect``, ``budget``,
``admin``, ``engineer`` — and each module registers its bodies (and, for
``engineer``, the loop predicates) into a ``TokenRegistry``.

The bodies are **pure** and **interface-driven**: an ordinary body is
``fn(inputs) -> {out_alias: value}`` (or ``fn(inputs, ctx)`` when it needs the run
Context), with no shelf access — the engine reads each action's declared
``interface.in`` into ``inputs`` and writes the returned dict to ``interface.out``.
The bodies compose the engine's own subsystems (``src/baseworkflow/subsystems/*``,
lifted out of the retired ``src/visitor/`` lineage so the engine is self-contained)
as independent, clean-interface functions: this is the non-invasive realisation of
the ``TODO(John)`` "all subsystems become independent, composable systems."

``build_registry()`` returns a fully-populated registry; ``src/baseworkflow``
hands it to ``engine.workflow.compile_workflow``.
"""
from __future__ import annotations

import os
import sys

# Bootstrap the repo paths the subsystem modules expect (repo idiom: src is not a
# package, subsystems import each other by bare name). Done before importing the
# namespace modules below, which import the engine's own subsystems.
_HERE = os.path.dirname(os.path.abspath(__file__))            # src/baseworkflow/bindings
_BW = os.path.dirname(_HERE)                                  # src/baseworkflow
_SRC = os.path.dirname(_BW)                                   # src
_ROOT = os.path.dirname(_SRC)                                 # repo root
# The subsystems the bind layer composes (prep / rescaffold / common / seam /
# adversary / decompose / strategy / resources / purpose / approval / …) now live
# in src/baseworkflow/subsystems/ — lifted out of the retired src/visitor/ lineage
# so the workflow engine owns its logic and is self-contained. They still import
# each other by bare name, so the subsystems dir goes on sys.path. ``src/tuning.py``
# stays at src/ (engine-wide tunables), reached via _SRC; ``engine`` via _ROOT.
_SUBSYS = os.path.join(_BW, "subsystems")                     # src/baseworkflow/subsystems
for _p in (_ROOT, _SRC, _SUBSYS, _BW):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine.workflow import TokenRegistry  # noqa: E402

from . import admin as _admin  # noqa: E402
from . import architect as _architect  # noqa: E402
from . import budget as _budget  # noqa: E402
from . import engineer as _engineer  # noqa: E402
from . import github as _github  # noqa: E402

_MODULES = (_github, _architect, _budget, _admin, _engineer)


def build_registry() -> TokenRegistry:
    """Return a ``TokenRegistry`` with every action bind and loop predicate the
    baseworkflow YAML references."""
    reg = TokenRegistry()
    for module in _MODULES:
        module.register(reg)
    return reg
