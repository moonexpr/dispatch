#!/usr/bin/env python3
"""manifest.py — the per-action interface file (the Valve ``Template`` analogue).

Each workflow action is described by its own small YAML file — its *interface
rule* — referenced from the workflow by a namespaced token (``github:generate_work_units``).
The manifest declares everything about the action that is *not* its code:

  * ``token``       — the namespaced name (``ns:name``) the workflow composes by.
  * ``kind``        — ``inference`` (a judgment, oracle-backed under mock) or
                      ``procedure`` (a deterministic step).
  * ``bind``        — the name the implementation is registered under in the
                      ``TokenRegistry`` (the code seam).
  * ``fmt``         — the structured-output format for an inference (text/json/yaml).
  * ``raw``         — the body self-manages its shelf I/O and may return a Result
                      (the recursion / sub-program seam); the compiler does not
                      auto-wire the interface around it.
  * ``permission``  — the tool allowlist for a permission Governor (``[]`` =
                      deny-by-default); omitted = no permission Governor.
  * ``budget``      — the named budget bucket whose meter+cap this action charges;
                      omitted = no budget Governor.
  * ``interface``   — ``in``/``out`` maps of ``alias -> shelf.key``. This is the
                      *visible* data flow Valve's ``StartWaveOutput`` makes explicit:
                      the compiler reads ``in`` from the shelves into the body and
                      writes the body's result to ``out``, and the validator checks
                      that every ``in`` is seeded or produced upstream.

Leaf module: stdlib + an in-function PyYAML import (matching ``engine.filesys``).
"""
from __future__ import annotations

import glob as _glob
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

VALID_KINDS = ("inference", "procedure", "proxy")
VALID_SHELVES = ("input", "deliverables", "shared")


class ManifestError(Exception):
    """A malformed action interface file."""


@dataclass(frozen=True)
class IORef:
    """One ``alias -> shelf.key`` binding in an action's interface."""

    alias: str
    shelf: str
    key: str

    @property
    def ref(self) -> str:
        return f"{self.shelf}.{self.key}"


@dataclass(frozen=True)
class ActionManifest:
    """The parsed interface rule for one action token.

    A ``kind == "proxy"`` manifest is special: it does not carry its own body.
    Instead it *wraps* the action in :attr:`proxy_target` and reroutes that
    target's shelf I/O via :attr:`rewire_in` / :attr:`rewire_out` (each an
    ``alias -> new shelf.key`` remap of one of the target's interface aliases).
    Proxy manifests are built by the overlay loader from a resolved target, not
    parsed from a manifest file — see :func:`proxy_manifest`. ``bind`` mirrors the
    target's bind so the validator's registration check still resolves.
    """

    token: str
    kind: str
    bind: str
    fmt: str = "text"
    raw: bool = False
    permission: Optional[Tuple[str, ...]] = None
    budget: Optional[str] = None
    inputs: Tuple[IORef, ...] = ()
    outputs: Tuple[IORef, ...] = ()
    source: str = ""
    # -- proxy-only (kind == "proxy") ---------------------------------------
    proxy_target: Optional["ActionManifest"] = None
    rewire_in: Tuple[IORef, ...] = ()
    rewire_out: Tuple[IORef, ...] = ()

    @property
    def namespace(self) -> str:
        return self.token.split(":", 1)[0]


def _parse_io(spec: Any, where: str) -> Tuple[IORef, ...]:
    if spec is None:
        return ()
    if not isinstance(spec, dict):
        raise ManifestError(f"{where}: must be a map of alias -> 'shelf.key', got {type(spec).__name__}")
    refs = []
    for alias, ref in spec.items():
        if not isinstance(ref, str) or "." not in ref:
            raise ManifestError(f"{where}: {alias!r} must be 'shelf.key', got {ref!r}")
        shelf, key = ref.split(".", 1)
        if shelf not in VALID_SHELVES:
            raise ManifestError(f"{where}: {alias!r} unknown shelf {shelf!r} (one of {VALID_SHELVES})")
        refs.append(IORef(alias=str(alias), shelf=shelf, key=key))
    return tuple(refs)


def manifest_from_dict(d: Dict[str, Any], *, source: str = "") -> ActionManifest:
    """Build an :class:`ActionManifest` from a parsed YAML mapping, raising
    :class:`ManifestError` with a located message on any shape problem."""
    token = d.get("token")
    if not token or ":" not in str(token):
        raise ManifestError(f"{source}: manifest needs a namespaced 'token' (ns:name), got {token!r}")
    kind = d.get("kind", "procedure")
    if kind not in VALID_KINDS:
        raise ManifestError(f"{source}: token {token!r} kind {kind!r} not in {VALID_KINDS}")
    bind = d.get("bind")
    if not bind:
        raise ManifestError(f"{source}: token {token!r} missing 'bind' (the registered impl name)")
    iface = d.get("interface") or {}
    perm = d.get("permission")
    return ActionManifest(
        token=str(token),
        kind=str(kind),
        bind=str(bind),
        fmt=str(d.get("fmt", "text")),
        raw=bool(d.get("raw", False)),
        permission=tuple(str(t) for t in perm) if perm is not None else None,
        budget=(str(d["budget"]) if d.get("budget") is not None else None),
        inputs=_parse_io(iface.get("in"), f"{token}.interface.in"),
        outputs=_parse_io(iface.get("out"), f"{token}.interface.out"),
        source=source,
    )


def proxy_manifest(
    target: ActionManifest,
    rewire_in_spec: Any,
    rewire_out_spec: Any,
    *,
    source: str = "",
) -> ActionManifest:
    """Build a ``kind == "proxy"`` manifest that wraps ``target`` and reroutes its
    shelf I/O. ``rewire_in_spec`` / ``rewire_out_spec`` are ``{target_alias ->
    'shelf.key'}`` maps naming, for one of the target's interface aliases, a NEW
    shelf location to source the input from (``in``) or write the output to
    (``out``). The target's body is untouched; the reroute happens around it at the
    shelf level (so it works for ``raw`` targets too). Used by the overlay loader's
    ``proxy:`` op."""
    rin = _parse_io(rewire_in_spec, f"{target.token}.proxy.rewire.in")
    rout = _parse_io(rewire_out_spec, f"{target.token}.proxy.rewire.out")
    in_aliases = {r.alias for r in target.inputs}
    out_aliases = {r.alias for r in target.outputs}
    for r in rin:
        if r.alias not in in_aliases:
            raise ManifestError(
                f"{target.token}: proxy rewire.in alias {r.alias!r} is not an input of the "
                f"target (inputs: {sorted(in_aliases)})"
            )
    for r in rout:
        if r.alias not in out_aliases:
            raise ManifestError(
                f"{target.token}: proxy rewire.out alias {r.alias!r} is not an output of the "
                f"target (outputs: {sorted(out_aliases)})"
            )
    rin_by_alias = {r.alias: r for r in rin}
    # Effective interface for the data-flow validator: a rewired input now reads
    # from its NEW source; the target's default outputs stay produced and the
    # rewired sinks are produced too.
    eff_inputs = tuple(rin_by_alias.get(r.alias, r) for r in target.inputs)
    eff_outputs = target.outputs + rout
    return ActionManifest(
        token=target.token,
        kind="proxy",
        bind=target.bind,
        fmt=target.fmt,
        raw=target.raw,
        permission=None,
        budget=None,
        inputs=eff_inputs,
        outputs=eff_outputs,
        source=source or target.source,
        proxy_target=target,
        rewire_in=rin,
        rewire_out=rout,
    )


def load_manifests(base_dir: str, globs) -> Dict[str, ActionManifest]:
    """Load every action interface file matched by ``globs`` (resolved relative to
    ``base_dir`` — the directory of the including workflow file, the ``#base``
    convention). Returns ``{token: ActionManifest}``; raises on a duplicate token."""
    import yaml

    out: Dict[str, ActionManifest] = {}
    for g in globs or ():
        pattern = g if os.path.isabs(g) else os.path.join(base_dir, g)
        for path in sorted(_glob.glob(pattern, recursive=True)):
            with open(path, encoding="utf-8") as fh:
                d = yaml.safe_load(fh) or {}
            rel = os.path.relpath(path, base_dir)
            m = manifest_from_dict(d, source=rel)
            if m.token in out:
                raise ManifestError(
                    f"duplicate token {m.token!r} (in {m.source} and {out[m.token].source})"
                )
            out[m.token] = m
    return out
