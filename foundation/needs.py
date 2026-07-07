#!/usr/bin/env python3
"""foundation.needs — the dependency formalism: Needs, Provisions, and NeedSets.

A **Need** is a first-class declaration that an action (and, by union, a
controller or workflow) requires something from its environment. A **Provision**
is the matching offer. A **NeedSet** carries the composition algebra: needs
compose upward by union — an action's needs, a controller's needs (the union of
its actions'), a workflow's needs (the union of its controllers') — and a
subject is **satisfied** by a provision set iff every non-optional need is met
(ADR-003). Because both sides are plain data, satisfaction is decidable
*statically*: the workflow validator answers "can this environment run this
workflow?" without executing anything, and the same objects drive per-request
accept/reject decisions at run time.

Three kinds (an open vocabulary, validated where parsed):

  * ``service:<domain>.<facet>`` — a standard API service must be registered
    (see :mod:`foundation.services`; facets are ``access`` / ``interactive``).
  * ``input:<key>``  — a runtime input must be seeded onto the input shelf.
  * ``config:<key>`` — a key must be present in the workflow's seeded ``config``.

Leaf module: stdlib only. Safe for anything under ``foundation/`` to import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

KIND_SERVICE = "service"
KIND_INPUT = "input"
KIND_CONFIG = "config"
VALID_NEED_KINDS = (KIND_SERVICE, KIND_INPUT, KIND_CONFIG)


class NeedError(Exception):
    """A malformed need declaration (located message where possible)."""


@dataclass(frozen=True)
class Need:
    """One declared dependency. ``kind:name`` is the identity; ``optional``
    needs never block satisfaction (they surface in reports instead)."""

    kind: str
    name: str
    optional: bool = False
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    # -- terse constructors ---------------------------------------------------
    @classmethod
    def service(cls, name: str, *, optional: bool = False, note: str = "") -> "Need":
        return cls(KIND_SERVICE, name, optional=optional, note=note)

    @classmethod
    def input(cls, name: str, *, optional: bool = False, note: str = "") -> "Need":
        return cls(KIND_INPUT, name, optional=optional, note=note)

    @classmethod
    def config(cls, name: str, *, optional: bool = False, note: str = "") -> "Need":
        return cls(KIND_CONFIG, name, optional=optional, note=note)

    def satisfied_by(self, provision: "Provision") -> bool:
        return self.kind == provision.kind and self.name == provision.name

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"kind": self.kind, "name": self.name}
        if self.optional:
            d["optional"] = True
        if self.note:
            d["note"] = self.note
        return d

    def __str__(self) -> str:
        return self.key + (" (optional)" if self.optional else "")


@dataclass(frozen=True)
class Provision:
    """One offered resource, in the same ``kind:name`` key space as
    :class:`Need`. ``source`` names the provider (a service class,
    ``workflow.inputs``, ``workflow.seed.config``) for reporting."""

    kind: str
    name: str
    source: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    def __str__(self) -> str:
        return f"{self.key} [{self.source}]" if self.source else self.key


class NeedSet:
    """An immutable set of Needs with the composition algebra: union (``|``),
    :meth:`unmet`, :meth:`satisfied_by`. Deduplicates by ``kind:name`` — a
    required declaration wins over an optional duplicate, so a controller that
    unions a strict member with a lenient one stays strict."""

    def __init__(self, needs: Iterable[Need] = ()) -> None:
        by_key: Dict[str, Need] = {}
        for n in needs:
            prior = by_key.get(n.key)
            if prior is None or (prior.optional and not n.optional):
                by_key[n.key] = n
        self._needs: Tuple[Need, ...] = tuple(sorted(by_key.values(), key=lambda n: n.key))

    # -- set protocol ---------------------------------------------------------
    def __iter__(self) -> Iterator[Need]:
        return iter(self._needs)

    def __len__(self) -> int:
        return len(self._needs)

    def __bool__(self) -> bool:
        return bool(self._needs)

    def __contains__(self, item: Any) -> bool:
        key = item.key if isinstance(item, (Need, Provision)) else str(item)
        return any(n.key == key for n in self._needs)

    def __or__(self, other: Iterable[Need]) -> "NeedSet":
        return NeedSet((*self._needs, *other))

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, NeedSet) and self._needs == other._needs

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"NeedSet({', '.join(n.key for n in self._needs) or 'empty'})"

    # -- views ----------------------------------------------------------------
    @property
    def required(self) -> Tuple[Need, ...]:
        return tuple(n for n in self._needs if not n.optional)

    @property
    def optional(self) -> Tuple[Need, ...]:
        return tuple(n for n in self._needs if n.optional)

    # -- satisfaction ---------------------------------------------------------
    def unmet(self, provisions: Iterable[Provision], *, include_optional: bool = False) -> "NeedSet":
        """The needs no provision satisfies. Optional needs are excluded unless
        ``include_optional`` (reports want them; satisfaction never blocks on them)."""
        offered = {p.key for p in provisions}
        pool = self._needs if include_optional else self.required
        return NeedSet(n for n in pool if n.key not in offered)

    def satisfied_by(self, provisions: Iterable[Provision]) -> bool:
        """True iff every non-optional need is met — the ADR-003 definition of
        a *satisfied* action/controller/workflow."""
        return not self.unmet(provisions)

    def to_list(self) -> List[Dict[str, Any]]:
        return [n.to_dict() for n in self._needs]


def parse_needs(spec: Any, *, where: str = "needs") -> Tuple[Need, ...]:
    """Parse a YAML ``needs:`` block into Need objects. Each item is a mapping in
    one of two shapes: the sugar form — exactly one kind key plus flags
    (``{service: github.access, optional: true}``) — or the explicit form
    (``{kind: service, name: github.access}``). Raises :class:`NeedError` with a
    located message on any shape problem."""
    if spec is None:
        return ()
    if not isinstance(spec, (list, tuple)):
        raise NeedError(f"{where}: must be a list of need mappings, got {type(spec).__name__}")
    out: List[Need] = []
    for i, item in enumerate(spec):
        loc = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise NeedError(f"{loc}: a need is a mapping, got {type(item).__name__}")
        optional = bool(item.get("optional", False))
        note = str(item.get("note", ""))
        if "kind" in item:
            kind, name = str(item["kind"]), str(item.get("name", ""))
        else:
            kinds = [k for k in VALID_NEED_KINDS if k in item]
            if len(kinds) != 1:
                raise NeedError(
                    f"{loc}: a need names exactly one kind of {VALID_NEED_KINDS} "
                    f"(or explicit kind:/name:), got keys {sorted(item)}"
                )
            kind, name = kinds[0], str(item[kinds[0]])
        if kind not in VALID_NEED_KINDS:
            raise NeedError(f"{loc}: unknown need kind {kind!r} (one of {VALID_NEED_KINDS})")
        if not name:
            raise NeedError(f"{loc}: need of kind {kind!r} is missing a name")
        out.append(Need(kind, name, optional=optional, note=note))
    return tuple(out)


def input_provisions(inputs: Iterable[str], seed: Optional[Dict[str, Any]] = None) -> Tuple[Provision, ...]:
    """The provisions a workflow document itself offers: its declared runtime
    ``inputs`` and static ``seed`` keys (kind ``input``), plus every key of a
    dict-valued ``seed.config`` (kind ``config``)."""
    out: List[Provision] = [Provision(KIND_INPUT, str(k), source="workflow.inputs") for k in (inputs or ())]
    seed = seed or {}
    out.extend(Provision(KIND_INPUT, str(k), source="workflow.seed") for k in seed)
    config = seed.get("config")
    if isinstance(config, dict):
        out.extend(Provision(KIND_CONFIG, str(k), source="workflow.seed.config") for k in config)
    return tuple(out)
