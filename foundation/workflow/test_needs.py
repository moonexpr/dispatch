#!/usr/bin/env python3
"""test_needs.py — the ADR-003 dependency formalism + controller references.

Covers the Need/Provision/NeedSet algebra, needs parsing (manifest ``needs:``
blocks + interface-derived input needs), controller registration/expansion/
compilation (one path, static and dynamic), the validator's needs-satisfaction
and cycle checks, the needs report, and the runtime ``ctx.service`` counterpart.

No pytest in this repo — a runnable, self-asserting module (exit 0 = pass).
Run: ``python3 foundation/workflow/test_needs.py``.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from foundation.actions import BudgetMeter, Context, MockActionFactory  # noqa: E402
from foundation.needs import Need, NeedError, NeedSet, Provision, parse_needs  # noqa: E402
from foundation.services import AccessService, ServiceRegistry  # noqa: E402
from foundation.workflow import (  # noqa: E402
    RenderVisitor,
    TokenRegistry,
    compile_controller,
    controller_needs,
    manifest_from_dict,
    needs_report,
    parse_document,
    validate,
)

CHECKS = {"n": 0}


def ok(label: str) -> None:
    CHECKS["n"] += 1
    print(f"  ok  {label}")


class DemoAccess(AccessService):
    domain = "demo"

    def fetch(self, key):  # a stand-in domain method
        return f"fetched:{key}"


def make_manifests():
    m1 = manifest_from_dict(
        {
            "token": "t:one",
            "kind": "procedure",
            "bind": "one",
            "needs": [{"service": "demo.access"}],
            "interface": {"in": {"job": "input.job"}, "out": {"mid": "shared.mid"}},
        },
        source="t/one.yml",
    )
    m2 = manifest_from_dict(
        {
            "token": "t:two",
            "kind": "procedure",
            "bind": "two",
            "interface": {"in": {"mid": "shared.mid"}, "out": {"final": "deliverables.final"}},
        },
        source="t/two.yml",
    )
    return {"t:one": m1, "t:two": m2}


def make_registry() -> TokenRegistry:
    reg = TokenRegistry()
    reg.register_action("one", lambda inputs: {"mid": f"mid({inputs['job']})"})
    reg.register_action("two", lambda inputs: {"final": f"final({inputs['mid']})"})
    reg.register_controller(
        "demo.ctrl",
        steps=("t:one", "t:two"),
        needs=(Need.config("mode", optional=True),),
        description="demo controller",
    )
    return reg


def make_node(manifests, phases=None, controllers=("demo.ctrl",)):
    doc = {
        "name": "demo",
        "inputs": ["job"],
        "controllers": list(controllers),
        "phases": phases or {"main": [{"controller": "demo.ctrl"}]},
    }
    return parse_document(doc, manifests, source="demo.yml")


def test_needset_algebra() -> None:
    a, b = Need.service("x.access"), Need.input("job")
    ns = NeedSet([a]) | [b, Need.service("x.access", optional=True)]
    assert len(ns) == 2 and not [n for n in ns if n.key == "service:x.access"][0].optional
    provisions = [Provision("input", "job", source="workflow.inputs")]
    unmet = ns.unmet(provisions)
    assert [n.key for n in unmet] == ["service:x.access"]
    assert not ns.satisfied_by(provisions)
    assert ns.satisfied_by(provisions + [Provision("service", "x.access", source="X")])
    ok("NeedSet: union dedups (required wins), unmet/satisfied_by respect optionality")


def test_parse_needs_forms() -> None:
    needs = parse_needs(
        [{"service": "a.access"}, {"input": "job", "optional": True}, {"kind": "config", "name": "k"}]
    )
    assert [n.key for n in needs] == ["service:a.access", "input:job", "config:k"]
    assert needs[1].optional
    for bad in ([{"service": "x", "input": "y"}], [{"kind": "nope", "name": "x"}], ["str"]):
        try:
            parse_needs(bad)
        except NeedError:
            pass
        else:
            raise AssertionError(f"parse_needs accepted {bad!r}")
    ok("parse_needs: sugar + explicit forms parse; malformed shapes raise")


def test_manifest_effective_needs() -> None:
    manifests = make_manifests()
    eff = {n.key: n for n in manifests["t:one"].effective_needs}
    assert set(eff) == {"service:demo.access", "input:job"}, eff
    assert not manifests["t:two"].effective_needs  # shared.* reads derive nothing
    ok("manifest: declared needs + interface-derived input needs, shelf-aware")


def test_controller_needs_union() -> None:
    ns = controller_needs("demo.ctrl", registry=make_registry(), manifests=make_manifests())
    assert {n.key for n in ns} == {"service:demo.access", "input:job", "config:mode"}
    assert {n.key for n in ns.required} == {"service:demo.access", "input:job"}
    ok("controller needs = union of member actions' needs + spec extras")


def test_validate_satisfaction() -> None:
    manifests, reg = make_manifests(), make_registry()
    node = make_node(manifests)
    provided = ServiceRegistry([DemoAccess()])
    assert validate(node, reg, services=provided) == []
    errors = validate(node, reg, services=ServiceRegistry())
    assert len(errors) == 1 and "service:demo.access" in str(errors[0]), errors
    assert validate(node, reg) == []  # no services -> structure/data-flow/tokens only
    ok("validate: unmet service need is an error exactly when a ServiceRegistry is given")


def test_validate_declared_only_and_unknown() -> None:
    manifests, reg = make_manifests(), make_registry()
    # 'demo.ctrl' is declared but never phase-referenced: still expanded + checked.
    node = make_node(manifests, phases={"main": ["t:one"]}, controllers=("demo.ctrl", "ghost.ctrl"))
    errors = validate(node, reg)
    assert len(errors) == 1 and "ghost.ctrl" in str(errors[0]), errors
    ok("declared-only controllers are validated; unknown names are located errors")


def test_validate_cycle() -> None:
    manifests, reg = make_manifests(), make_registry()
    reg.register_controller("loopy", steps=({"controller": "loopy"},))
    node = make_node(manifests, phases={"main": [{"controller": "loopy"}]}, controllers=())
    errors = validate(node, reg)
    assert any("cyclic" in str(e) for e in errors), errors
    ok("cyclic controller references are detected, not expanded forever")


def test_compile_controller_runs() -> None:
    factory = MockActionFactory()
    control = compile_controller(
        "demo.ctrl", registry=make_registry(), manifests=make_manifests(), factory=factory
    )
    ctx = Context(shelves=factory.shelves(), meter=BudgetMeter(10**9, label="test"))
    ctx.shelves.input.put("job", "J")
    result = control.run(None, ctx)
    assert result.ok and ctx.shelves.deliverables.get("final") == "final(mid(J))"
    ok("compile_controller materializes a runnable Control (the dynamic path)")


def test_needs_report_and_render() -> None:
    manifests, reg = make_manifests(), make_registry()
    node = make_node(manifests)
    report = needs_report(node, reg, ServiceRegistry([DemoAccess()]))
    entry = report["controllers"]["demo.ctrl"]
    assert entry["registered"] and report["satisfied"] is True, report
    by_need = {n["need"]: n for n in entry["needs"]}
    assert by_need["service:demo.access"]["satisfied_by"] == "DemoAccess"
    assert by_need["config:mode"]["optional"] and by_need["config:mode"]["satisfied_by"] is None
    report2 = needs_report(node, reg, ServiceRegistry())
    assert report2["satisfied"] is False and "service:demo.access" in report2["unmet"]
    rendered = RenderVisitor().visit(node)
    assert rendered["phases"]["main"] == [{"controller": "demo.ctrl"}]
    assert rendered["controllers"] == ["demo.ctrl"]
    ok("needs report annotates provisions/UNMET; render round-trips controller refs")


def test_ctx_service_unmet() -> None:
    ctx = Context(shelves=MockActionFactory().shelves(), meter=BudgetMeter(10, label="t"))
    try:
        ctx.service("demo.access")
    except Exception as exc:  # noqa: BLE001 — asserting the message vocabulary
        assert "unmet need service:demo.access" in str(exc)
    else:
        raise AssertionError("ctx.service must raise without a registry")
    ctx.services = ServiceRegistry([DemoAccess()])
    assert ctx.service("demo.access").fetch("k") == "fetched:k"
    ok("ctx.service: unmet needs raise in the needs vocabulary; registered ones resolve")


def main() -> int:
    test_needset_algebra()
    test_parse_needs_forms()
    test_manifest_effective_needs()
    test_controller_needs_union()
    test_validate_satisfaction()
    test_validate_declared_only_and_unknown()
    test_validate_cycle()
    test_compile_controller_runs()
    test_needs_report_and_render()
    test_ctx_service_unmet()
    print(f"\nneeds+controllers: {CHECKS['n']} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
