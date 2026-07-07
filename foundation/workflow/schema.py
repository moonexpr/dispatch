#!/usr/bin/env python3
"""schema.py — the declarative shelf-key contract (typed I/O for the workflow).

An action's ``interface.in``/``out`` names *where* data lives (``alias -> shelf.key``);
this module names *what shape* a ``shelf.key`` holds. A shelf key like ``input.job``
is read by many actions, so its type is a property of the **key**, not of any one
action — the contract is therefore a registry keyed by ``"shelf.key"``, declared
once (see ``app/config/schemas/*.yml``) and shared.

The schema is a small, self-defined type spec — deliberately *not* JSON Schema, so
``foundation.workflow`` stays a leaf (no ``jsonschema`` runtime dependency, mirroring
the note in this package's ``__init__``). It carries exactly what a debug UI needs to
render a typed form and what the engine needs to check a value:

    type         string | integer | number | boolean | array | object | any
    required     (object field) the key must be present
    description  human text (field label / tooltip)
    enum         allowed values (renders as a dropdown)
    items        element type for ``array``
    format       rendering hint (e.g. ``multiline`` for a big string)
    default      value used when absent
    example      a realistic value (prefills the form)
    minimum/maximum   numeric bounds
    fields       nested field schemas for ``object``

``validate(value)`` returns a list of human-readable error strings (empty = ok) and
is shared by both the tooling surface (the debug viewer's probe validation) and the
opt-in engine enforcement (``DISPATCH_ENFORCE_SCHEMA``); ``to_dict()`` emits the
JSON-only shape a UI consumes.

Leaf module: stdlib only (an in-function PyYAML import lives in the loader, not here).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

SCALAR_TYPES = ("string", "integer", "number", "boolean")
VALID_TYPES = SCALAR_TYPES + ("array", "object", "any")


class SchemaError(Exception):
    """A malformed schema declaration."""


class SchemaContractError(Exception):
    """A runtime shelf value violated its declared contract (raised only when
    ``DISPATCH_ENFORCE_SCHEMA`` is set; off by default). Trapped by ``Action.run``
    into a failed Result, so a violation fails the step cleanly rather than
    crashing the engine."""


def _pytype(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (list, tuple)):
        return "array"
    if isinstance(v, dict):
        return "object"
    if v is None:
        return "null"
    return type(v).__name__


def _type_ok(value: Any, t: str) -> bool:
    if t in ("any", ""):
        return True
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "array":
        return isinstance(value, (list, tuple))
    if t == "object":
        return isinstance(value, dict)
    return True


@dataclass(frozen=True)
class FieldSchema:
    """One field of an ``object`` shelf schema (or one array element's contract)."""

    name: str
    type: str = "any"
    required: bool = False
    description: str = ""
    enum: Tuple[Any, ...] = ()
    items: str = ""
    format: str = ""
    default: Any = None
    example: Any = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    fields: Tuple["FieldSchema", ...] = ()

    def validate(self, value: Any) -> List[str]:
        """Errors for ``value`` against this field (no key prefix; the caller adds it)."""
        if value is None:
            return []  # presence is the parent object's ``required`` concern
        errs: List[str] = []
        if not _type_ok(value, self.type):
            return [f"expected {self.type}, got {_pytype(value)}"]
        if self.enum and value not in self.enum:
            errs.append(f"{value!r} not in {list(self.enum)}")
        if self.type in ("number", "integer"):
            if self.minimum is not None and value < self.minimum:
                errs.append(f"{value} < minimum {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                errs.append(f"{value} > maximum {self.maximum}")
        if self.type == "array" and self.items:
            for i, item in enumerate(value):
                if not _type_ok(item, self.items):
                    errs.append(f"[{i}] expected {self.items}, got {_pytype(item)}")
        if self.type == "object" and self.fields:
            errs.extend(_validate_object(self.fields, value))
        return errs

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name, "type": self.type}
        if self.required:
            out["required"] = True
        if self.description:
            out["description"] = self.description
        if self.enum:
            out["enum"] = list(self.enum)
        if self.items:
            out["items"] = self.items
        if self.format:
            out["format"] = self.format
        if self.default is not None:
            out["default"] = self.default
        if self.example is not None:
            out["example"] = self.example
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.fields:
            out["fields"] = [f.to_dict() for f in self.fields]
        return out


def _validate_object(fields: Tuple[FieldSchema, ...], value: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    for f in fields:
        present = f.name in value
        if f.required and not present:
            errs.append(f".{f.name}: required")
            continue
        if present:
            errs.extend(f".{f.name}{'' if e[:1] in ('.', '[') else ': '}{e}" for e in f.validate(value[f.name]))
    return errs


@dataclass(frozen=True)
class ShelfSchema:
    """The declared contract for one ``shelf.key`` (the registry value)."""

    ref: str
    type: str = "object"
    description: str = ""
    fields: Tuple[FieldSchema, ...] = ()
    items: str = ""
    enum: Tuple[Any, ...] = ()
    example: Any = None

    def validate(self, value: Any) -> List[str]:
        """Human-readable errors for ``value`` against this contract (empty = ok)."""
        if value is None:
            return []
        if not _type_ok(value, self.type):
            return [f"{self.ref}: expected {self.type}, got {_pytype(value)}"]
        errs: List[str] = []
        if self.type == "object":
            errs.extend(f"{self.ref}{e}" for e in _validate_object(self.fields, value))
        elif self.type == "array" and self.items:
            for i, item in enumerate(value):
                if not _type_ok(item, self.items):
                    errs.append(f"{self.ref}[{i}]: expected {self.items}, got {_pytype(item)}")
        if self.enum and value not in self.enum:
            errs.append(f"{self.ref}: {value!r} not in {list(self.enum)}")
        return errs

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ref": self.ref, "type": self.type}
        if self.description:
            out["description"] = self.description
        if self.fields:
            out["fields"] = [f.to_dict() for f in self.fields]
        if self.items:
            out["items"] = self.items
        if self.enum:
            out["enum"] = list(self.enum)
        if self.example is not None:
            out["example"] = self.example
        return out


# -- parsing ----------------------------------------------------------------
def _as_field(name: str, spec: Any, where: str) -> FieldSchema:
    # Shorthand: ``title: string`` (a bare type name) or ``title: {type: string, ...}``.
    if isinstance(spec, str):
        spec = {"type": spec}
    if not isinstance(spec, dict):
        raise SchemaError(f"{where}.{name}: field must be a type string or a map, got {type(spec).__name__}")
    t = str(spec.get("type", "any"))
    if t not in VALID_TYPES:
        raise SchemaError(f"{where}.{name}: unknown type {t!r} (one of {VALID_TYPES})")
    nested = spec.get("fields") or {}
    return FieldSchema(
        name=name,
        type=t,
        required=bool(spec.get("required", False)),
        description=str(spec.get("description", "")),
        enum=tuple(spec.get("enum") or ()),
        items=str(spec.get("items", "")),
        format=str(spec.get("format", "")),
        default=spec.get("default"),
        example=spec.get("example"),
        minimum=spec.get("minimum"),
        maximum=spec.get("maximum"),
        fields=tuple(_as_field(k, v, f"{where}.{name}") for k, v in nested.items()),
    )


def parse_schema(ref: str, spec: Any) -> ShelfSchema:
    """Parse one ``"shelf.key" -> spec`` declaration into a :class:`ShelfSchema`."""
    if isinstance(spec, str):
        spec = {"type": spec}
    if not isinstance(spec, dict):
        raise SchemaError(f"schema {ref!r}: must be a map, got {type(spec).__name__}")
    t = str(spec.get("type", "object"))
    if t not in VALID_TYPES:
        raise SchemaError(f"schema {ref!r}: unknown type {t!r} (one of {VALID_TYPES})")
    fields = spec.get("fields") or {}
    if not isinstance(fields, dict):
        raise SchemaError(f"schema {ref!r}: 'fields' must be a map of name -> field spec")
    return ShelfSchema(
        ref=ref,
        type=t,
        description=str(spec.get("description", "")),
        fields=tuple(_as_field(k, v, ref) for k, v in fields.items()),
        items=str(spec.get("items", "")),
        enum=tuple(spec.get("enum") or ()),
        example=spec.get("example"),
    )


def parse_schemas(mapping: Any, *, where: str = "schemas") -> Dict[str, ShelfSchema]:
    """Parse a ``{"shelf.key": spec, ...}`` mapping into a schema registry."""
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        raise SchemaError(f"{where}: must be a map of 'shelf.key' -> schema, got {type(mapping).__name__}")
    out: Dict[str, ShelfSchema] = {}
    for ref, spec in mapping.items():
        if "." not in str(ref):
            raise SchemaError(f"{where}: key {ref!r} must be 'shelf.key'")
        out[str(ref)] = parse_schema(str(ref), spec)
    return out
