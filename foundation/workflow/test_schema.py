#!/usr/bin/env python3
"""test_schema.py — the declarative shelf-key contract (parse + validate + to_dict)."""
from __future__ import annotations

import pytest

from foundation.workflow.schema import (
    SchemaError,
    ShelfSchema,
    parse_schema,
    parse_schemas,
)

JOB_SPEC = {
    "type": "object",
    "description": "a job",
    "fields": {
        "issue": {"type": "integer", "required": True},
        "title": {"type": "string", "required": True},
        "labels": {"type": "array", "items": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "action": {"type": "string", "enum": ["implement", "fix", "docs"]},
    },
}


def test_parse_shape():
    s = parse_schema("input.job", JOB_SPEC)
    assert s.ref == "input.job"
    assert s.type == "object"
    names = {f.name: f for f in s.fields}
    assert names["issue"].required and names["issue"].type == "integer"
    assert names["labels"].items == "string"
    assert names["action"].enum == ("implement", "fix", "docs")
    assert names["confidence"].minimum == 0 and names["confidence"].maximum == 1


def test_validate_ok():
    s = parse_schema("input.job", JOB_SPEC)
    assert s.validate({"issue": 1, "title": "x", "labels": ["a"], "confidence": 0.5, "action": "fix"}) == []
    # absent optional fields are fine; only declared-required keys must be present
    assert s.validate({"issue": 1, "title": "x"}) == []
    # None short-circuits (a missing shelf value is not a violation on its own)
    assert s.validate(None) == []


def test_validate_required_and_type():
    s = parse_schema("input.job", JOB_SPEC)
    assert any("issue" in e and "required" in e for e in s.validate({"title": "x"}))
    assert any("expected integer" in e for e in s.validate({"issue": "nine", "title": "x"}))


def test_validate_enum_range_and_items():
    s = parse_schema("input.job", JOB_SPEC)
    assert any("not in" in e for e in s.validate({"issue": 1, "title": "x", "action": "destroy"}))
    assert any("maximum" in e for e in s.validate({"issue": 1, "title": "x", "confidence": 9}))
    errs = s.validate({"issue": 1, "title": "x", "labels": [1, 2]})
    assert sum("expected string" in e for e in errs) == 2


def test_shorthand_type_string():
    # ``title: string`` shorthand and a scalar top-level schema.
    reg = parse_schemas({"shared.name": "string"})
    assert reg["shared.name"].type == "string"
    assert reg["shared.name"].validate("hi") == []
    assert reg["shared.name"].validate(3) == ["shared.name: expected string, got integer"]


def test_to_dict_roundtrips_key_fields():
    d = parse_schema("input.job", JOB_SPEC).to_dict()
    assert d["ref"] == "input.job" and d["type"] == "object"
    issue = next(f for f in d["fields"] if f["name"] == "issue")
    assert issue["required"] is True and issue["type"] == "integer"


def test_bad_declarations_raise():
    with pytest.raises(SchemaError):
        parse_schema("input.job", {"type": "widget"})           # unknown type
    with pytest.raises(SchemaError):
        parse_schemas({"noshelf": {"type": "object"}})           # key not "shelf.key"
    with pytest.raises(SchemaError):
        parse_schema("input.job", {"fields": {"x": {"type": "nope"}}})  # unknown field type


def test_empty_and_none():
    assert parse_schemas(None) == {}
    assert ShelfSchema(ref="x.y").validate({}) == []
