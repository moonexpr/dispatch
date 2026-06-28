#!/usr/bin/env python3
"""test_e2e.py — end-to-end test: BaseWorkflow against MockActionFactory.

Runs the whole spec -> work -> build lifecycle with no real side effects (in-memory
shelves, oracle-backed inference, no model, no network) and asserts the contract:
the run is green, every deliverable is produced, the budget is tracked and phased,
the machine is a serializable statechart, the orchestration script is a
deserializable statechart fragment, the Governors enforce (real) / stay permissive
(mock), and the control flow is identical across factories (inversion of control).

No pytest in this repo — this is a runnable, self-asserting module (exit 0 = pass),
matching the shell-harness convention. Run: ``python3 src/baseworkflow/test_e2e.py``.
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SRC)
for _p in (_ROOT, _SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import baseworkflow as bw  # noqa: E402
from engine.actions import (  # noqa: E402
    BudgetMeter,
    Context,
    InferenceSpec,
    MemoryShelf,
    MockActionFactory,
    OrchestrationScript,
    Program,
    RealActionFactory,
    Shelves,
    Statechart,
)

JOB = {
    "issue": 9001,
    "title": "Add retry with backoff to fetch",
    "body": "Implement retry.\n\n## Acceptance criteria\n- retries 3x\n- exponential backoff\n- gives up after cap",
    "labels": ["enhancement"],
    "discovered": ["engine/proc.py", "engine/runtime.py"],
}
TRIAGE = {"action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.82}

# Deliverables every phase must have produced by the end of a run.
EXPECTED_DELIVERABLES = {
    "purpose", "work_unit", "plan", "strategy", "bucket", "budget",
    "orchestration_script", "work_plan", "submission",
    "env", "adversarial_tests", "engineering_result", "docs", "stored",
    "intake",
}

_RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(ok), detail))


def _fresh_ctx(dry_run: bool = True) -> Context:
    return Context(
        shelves=Shelves(MemoryShelf("input"), MemoryShelf("deliverables"), MemoryShelf("shared")),
        meter=BudgetMeter(1000),
        dry_run=dry_run,
    )


def test_runs_green_with_all_deliverables() -> None:
    out = bw.run_mock(JOB, TRIAGE)
    r = out["result"]
    check("workflow runs green", r.ok, repr(getattr(r, "error", "")) if not r.ok else "")
    missing = EXPECTED_DELIVERABLES - set(out["deliverables"].keys())
    check("all deliverables produced", not missing, f"missing={sorted(missing)}")
    # work plan structure
    wp = out["deliverables"].get("work_plan") or {}
    check(
        "work plan has acceptance criteria + authorization",
        bool(wp.get("acceptance_criteria")) and "authorization" in wp,
        f"keys={sorted(wp.keys())}",
    )
    # submission is auto-approved
    sub = out["deliverables"].get("submission") or {}
    check("submission auto-approved", sub.get("approved") is True)


def test_run_live_dry_run_matches_mock() -> None:
    """The foundational LIVE runner (Phase 1): ``run_live(dry_run=True)`` runs the
    same BaseWorkflow against the ``RealActionFactory`` (enforcing governors, real
    inference runner) and must (a) run green, (b) produce the SAME deliverable key
    set as ``run_mock``, and (c) make NO model/network call — it must stay on the
    deterministic dry-run path. We prove (c) by detonating ``engine.models.chat``
    for the duration of the run: any real inference would raise, failing the run."""
    import engine.models as _models

    sentinel = {"called": False}

    def _boom(*a, **kw):  # any real model call trips this
        sentinel["called"] = True
        raise AssertionError("run_live(dry_run=True) called engine.models.chat (real model)")

    orig_chat = getattr(_models, "chat", None)
    _models.chat = _boom  # type: ignore[attr-defined]
    try:
        live = bw.run_live(JOB, TRIAGE, dry_run=True)
    finally:
        if orig_chat is not None:
            _models.chat = orig_chat  # type: ignore[attr-defined]
        # RealActionFactory uses durable FileShelf; clean up the run's sink.
        import shutil
        shutil.rmtree(os.path.join(_ROOT, "app", "actions"), ignore_errors=True)

    r = live["result"]
    check("run_live runs green (dry-run)", r.ok, repr(getattr(r, "error", "")) if not r.ok else "")
    check("run_live made no real model call", sentinel["called"] is False)
    check("run_live ran under dry_run", live["ctx"].dry_run is True)
    live_keys = set(live["deliverables"].keys())
    mock_keys = set(bw.run_mock(JOB, TRIAGE)["deliverables"].keys())
    check("run_live deliverable keys == run_mock", live_keys == mock_keys,
          f"sym_diff={sorted(live_keys ^ mock_keys)}")
    # uses the real (enforcing) factory family, not the mock
    check("run_live used RealActionFactory",
          type(live["workflow"].factory).__name__ == "RealActionFactory",
          f"factory={type(live['workflow'].factory).__name__}")


def test_budget_tracked_and_phased() -> None:
    out = bw.run_mock(JOB, TRIAGE)
    ctx = out["ctx"]
    check("global spend within 1,000,000 total", 0 < ctx.meter.spent <= bw.TOTAL_BUDGET, f"spent={ctx.meter.spent}")
    for phase in ("architect", "engineering", "admin"):
        check(f"phase meter present: {phase}", phase in ctx.budgets, f"have={sorted(ctx.budgets)}")
    eng = ctx.budgets.get("engineering")
    check("engineering bucket == ENGINEERING_BUDGET", eng is not None and eng.total == bw.ENGINEERING_BUDGET,
          f"cap={getattr(eng, 'total', None)}")
    arch = ctx.budgets.get("architect")
    check("architect bucket == ARCHITECT_BUDGET", arch is not None and arch.total == bw.ARCHITECT_BUDGET,
          f"cap={getattr(arch, 'total', None)}")
    # phase spend rolls up to the global meter (no double counting): sum == global
    rolled = sum(m.spent for m in ctx.budgets.values())
    check("phase spend rolls up to global", rolled == ctx.meter.spent, f"rolled={rolled} global={ctx.meter.spent}")


def test_no_real_side_effects() -> None:
    out = bw.run_mock(JOB, TRIAGE)
    wf = out["workflow"]
    backends = {type(wf.shelves.input).__name__, type(wf.shelves.deliverables).__name__, type(wf.shelves.shared).__name__}
    check("all shelves are in-memory (no disk)", backends == {"MemoryShelf"}, f"backends={backends}")
    check("ran under dry_run", out["ctx"].dry_run is True)


def test_machine_is_serializable_statechart() -> None:
    wf = bw.BaseWorkflow(MockActionFactory(), job=JOB, triage=TRIAGE)
    chart = wf.compile()
    check("compiles to a Statechart", isinstance(chart, Statechart))
    ids = list(chart.index.keys())
    check("every state has a stable, non-empty id", all(ids) and len(ids) == len(set(ids)), f"n={len(ids)}")
    # the three phases are addressable by stable path id
    expected_phase_ids = {"baseworkflow/0.spec", "baseworkflow/1.work", "baseworkflow/2.build"}
    check("phase states addressable by id", expected_phase_ids <= set(ids), f"have spec/work/build={expected_phase_ids <= set(ids)}")
    # first-class transitions on the lifecycle compound
    root = chart.root
    check("lifecycle has first-class transitions", len(root.transitions) > 0, f"n={len(root.transitions)}")
    # serializes to a plain dict (SCXML-aligned) and JSON-encodes
    d = chart.to_dict()
    try:
        json.dumps(d)
        serializable = True
    except TypeError:
        serializable = False
    check("statechart serializes to JSON", serializable and "root" in d)


def test_orchestration_script_is_statechart_fragment() -> None:
    out = bw.run_mock(JOB, TRIAGE)
    script_dict = out["deliverables"].get("orchestration_script")
    check("orchestration script emitted", isinstance(script_dict, dict) and bool(script_dict.get("phases")))
    # round-trips through JSON (a serialized program persisted on a shelf)
    text = json.dumps(script_dict)
    script = OrchestrationScript.from_dict(json.loads(text))
    check("script round-trips through JSON", script.permission == "deny-by-default" and "Agent" in script.allow_tools)
    # the factory deserializes it back into an executable Program (the depth operator)
    program = MockActionFactory().deserialize(script)
    check("script deserializes into a Program", isinstance(program, Program) and program.kind == "program")
    # and the rebuilt Program runs green on its own
    ctx = _fresh_ctx()
    ctx.shelves.deliverables.put("orchestration_script", script_dict)
    r = program.run(None, ctx)
    check("rebuilt Program runs green", r.ok, repr(getattr(r, "error", "")) if not r.ok else "")


def test_inversion_of_control() -> None:
    # The control structure (state ids) is identical whether mock or real builds it —
    # only the injected backend differs. That is the inversion-of-control guarantee.
    mock_ids = set(bw.BaseWorkflow(MockActionFactory(), job=JOB, triage=TRIAGE).compile().index.keys())
    real_ids = set(bw.BaseWorkflow(RealActionFactory(), job=JOB, triage=TRIAGE).compile().index.keys())
    check("control flow identical across factories", mock_ids == real_ids, f"sym_diff={mock_ids ^ real_ids}")
    # determinism: two mock runs produce the same deliverable shape
    d1 = set(bw.run_mock(JOB, TRIAGE)["deliverables"].keys())
    d2 = set(bw.run_mock(JOB, TRIAGE)["deliverables"].keys())
    check("mock runs are deterministic in shape", d1 == d2)


def test_governor_deny_by_default() -> None:
    ctx = _fresh_ctx()
    real = RealActionFactory()
    # an action requiring the Agent tool, with Agent NOT in the allowlist -> denied
    needs_agent = real.inference("needs-agent", InferenceSpec(prompt="x", tools=("Agent",)))
    denied = real.governor("permission", needs_agent, allow=("Read",)).run(None, ctx)
    check("real permission denies by default", not denied.ok, "should deny missing Agent tool")
    # same action, Agent allowlisted -> admitted (dry-run inference returns a placeholder)
    needs_agent2 = real.inference("needs-agent2", InferenceSpec(prompt="x", tools=("Agent",)))
    admitted = real.governor("permission", needs_agent2, allow=("Agent", "Read")).run(None, ctx)
    check("real permission admits allowlisted tool", admitted.ok)
    # mock permission is permissive even with an empty allowlist
    m = MockActionFactory()
    minf = m.inference("m", InferenceSpec(prompt="x", tools=("Agent",), oracle=lambda p, c: "ok"))
    permitted = m.governor("permission", minf, allow=()).run(None, ctx)
    check("mock permission is permissive", permitted.ok)


def test_governor_budget_enforced() -> None:
    ctx = _fresh_ctx()
    real = RealActionFactory()
    proc = real.procedure("spendy", lambda p, c: "done")
    # estimate (1000) exceeds the cap (500) -> pre-flight abort (real enforces)
    gov = real.governor("budget", proc, cap=500, estimate=1000, phase="tiny")
    r = gov.run(None, ctx)
    check("real budget aborts on cap breach", not r.ok, "1000 estimate over 500 cap should abort")
    # mock budget never aborts (charges but permissive)
    m = MockActionFactory()
    mgov = m.governor("budget", m.procedure("p", lambda p, c: "ok"), cap=500, estimate=1000, phase="tiny")
    check("mock budget never aborts", mgov.run(None, _fresh_ctx()).ok)


def test_program_depth_recorded() -> None:
    out = bw.run_mock(JOB, TRIAGE)
    eng = out["deliverables"].get("engineering_result") or {}
    check("engineering Program executed (nested chart)", eng.get("ok") is True, f"eng={eng}")
    # the trace recorded actions at depth > 0 (inside the nested engineering program)
    depths = {t.get("depth", 0) for t in out["ctx"].trace}
    check("nested execution reached depth > 0", any(d > 0 for d in depths), f"depths={sorted(depths)}")


def test_yaml_workflow() -> None:
    """The YAML-driven workflow: it validates clean (incl. the data-flow I/O
    contract), compiles to the pinned statechart, the validator catches mis-wired
    YAML, and the predicate grammar + advanced node types compile."""
    import bindings
    from engine.actions import MockActionFactory, Statechart
    from engine.workflow import (
        ActionRefNode,
        LoopNode,
        ParallelNode,
        PhaseNode,
        PredicateValidator,
        RenderVisitor,
        SequenceNode,
        WorkflowNode,
        compile_workflow,
        parse_predicate,
        validate,
    )
    from engine.workflow.manifest import ActionManifest, IORef

    from engine.workflow import load_workflow

    reg = bindings.build_registry()
    doc = load_workflow("workflows/baseworkflow.yml")

    # shipped YAML validates clean (structure + data-flow + tokens)
    errors = validate(doc, reg)
    check("shipped workflow YAML validates clean", not errors, f"errors={[str(e) for e in errors]}")

    # compiled-from-YAML statechart carries the pinned phase ids
    ctrl = compile_workflow(doc, registry=reg, factory=MockActionFactory())
    ids = set(ctrl.compile().index.keys())
    pinned = {"baseworkflow/0.spec", "baseworkflow/1.work", "baseworkflow/2.build"}
    check("compiled-from-YAML phase ids match pinned set", pinned <= ids, f"missing={pinned - ids}")

    # predicate grammar: composite parses; unknown ref is caught (visitor-validated)
    pv = PredicateValidator(reg)
    pv.visit(parse_predicate("engineering_succeeded and not engineering_failed"))
    check("composite predicate refs all known", not pv.errors, f"unknown={pv.errors}")
    pv2 = PredicateValidator(reg)
    pv2.visit(parse_predicate("bogus_predicate or engineering_succeeded"))
    check("validator catches unknown predicate", pv2.errors == ["bogus_predicate"], f"errors={pv2.errors}")

    # data-flow: an action reading an unproduced shelf key is flagged
    bad = ActionManifest(token="x:bad", kind="procedure", bind="store",
                         inputs=(IORef("z", "deliverables", "never_made"),))
    bad_doc = WorkflowNode(name="bad", phases=(PhaseNode("p", (ActionRefNode("x:bad", bad),)),),
                           inputs=("job", "triage"))
    check("validator catches unsatisfied input (data-flow)",
          any("never_made" in str(e) for e in validate(bad_doc, reg)), "should flag missing upstream out")

    # unknown bind is flagged
    unbound = ActionManifest(token="x:unbound", kind="procedure", bind="not_registered")
    unbound_doc = WorkflowNode(name="u", phases=(PhaseNode("p", (ActionRefNode("x:unbound", unbound),)),),
                               inputs=("job",))
    check("validator catches unregistered bind",
          any("not_registered" in str(e) for e in validate(unbound_doc, reg)), "should flag missing bind")

    # advanced node types compile: sequence + parallel + loop (with predicates)
    a_ref = doc.phases[0].steps[0]  # github:generate_work_units (in: input.job)
    syn = WorkflowNode(
        name="syn",
        phases=(PhaseNode("only", (
            SequenceNode("s", (a_ref,)),
            ParallelNode("par", (a_ref,)),
            LoopNode("l", a_ref, until="engineering_succeeded", abort_when="engineering_failed", max_iterations=2),
        )),),
        inputs=("job", "triage"), seed=doc.seed, budgets=doc.budgets,
    )
    ctrl2 = compile_workflow(syn, registry=reg, factory=MockActionFactory())
    check("synthetic seq+parallel+loop workflow compiles", isinstance(ctrl2.compile(), Statechart))

    # render visitor serializes the node tree back to a dict (round-trip)
    rendered = RenderVisitor().visit(doc)
    check("render visitor serializes workflow",
          isinstance(rendered, dict) and set(rendered.get("phases", {})) == {"spec", "work", "build"},
          f"phases={sorted(rendered.get('phases', {}))}")


def test_intake_invoice_transitions() -> None:
    """admin:intake_invoice (#137 + the engineer-invoice -> label transition ported
    from the deprecated visitors.py) — the FIRST gh-mutating workflow action. Under
    ``ctx.dry_run`` it must drive the right label transition for each invoice status
    WITHOUT any real gh mutation, and on partial/failed it must post the shared
    rescaffold directive + emit a ``fix-rescaffold`` ledger event (#137)."""
    import bindings
    import importlib

    reg = bindings.build_registry()
    binding = reg.action_binding("intake_invoice")
    check("intake_invoice registered + needs_ctx", binding.needs_ctx is True)
    intake_invoice = binding.fn

    # Detonate the real gh wrapper and capture the run-ledger so we can assert the
    # dry-run path makes NO gh mutation and emits the right ledger events.
    common = importlib.import_module("common")
    orig_mutate, orig_ledger = common.gh_mutate, common.ledger_emit
    ledger_calls: list = []

    def _boom_mutate(*a, **k):  # any real gh mutation under dry-run is a failure
        raise AssertionError(f"intake_invoice called gh_mutate under dry-run: {a}")

    common.gh_mutate = _boom_mutate  # type: ignore[assignment]
    common.ledger_emit = lambda stage, issue="", fields="": ledger_calls.append((stage, issue, fields))  # type: ignore[assignment]

    JOBP = {"issue": 9001, "pr_number": 4242, "route": "gen-default"}

    def _run(er):  # dry-run intake; returns the transition record + ledger stages
        ledger_calls.clear()
        ctx = _fresh_ctx(dry_run=True)
        ctx.shelves.input.put("job", JOBP)
        out = intake_invoice({"engineering_result": er, "job": JOBP}, ctx)
        return out["intake"], [s for s, _, _ in ledger_calls]

    try:
        # completed: arm auto-merge + relabel claimed -> done-pending-merge + closure
        rec, stages = _run({"ok": True, "value": {"done": True}, "meta": {"summary": "ok"}})
        labels = [m for m in rec["mutations"]]
        check("completed -> status completed", rec["status"] == "completed", f"rec={rec['status']}")
        check("completed arms auto-merge", any(m[:2] == ["pr", "merge"] for m in labels))
        check("completed relabels done-pending-merge",
              any("done-pending-merge" in m for m in labels))
        check("completed emits closure ledger", "closure" in stages, f"stages={stages}")

        # failed: fix-attempt-1 + rescaffold directive + fix-rescaffold ledger (#137)
        rec, stages = _run({"ok": False, "value": None, "meta": {"summary": "boom"}})
        muts = rec["mutations"]
        check("failed -> status failed", rec["status"] == "failed", f"rec={rec['status']}")
        check("failed adds fix-attempt-1", any("fix-attempt-1" in m for m in muts))
        check("failed posts rescaffold directive (#137)",
              any(any("rescaffold" in str(tok) for tok in m) for m in muts))
        check("failed emits fix-rescaffold ledger (#137)", "fix-rescaffold" in stages, f"stages={stages}")

        # partial: ok but falsy value -> same fix-attempt-1 + rescaffold path
        rec, stages = _run({"ok": True, "value": None, "meta": {"summary": "half"}})
        check("partial -> status partial", rec["status"] == "partial", f"rec={rec['status']}")
        check("partial emits fix-rescaffold ledger (#137)", "fix-rescaffold" in stages, f"stages={stages}")

        # needs-human: only reachable via an explicit meta.status
        rec, stages = _run({"ok": False, "value": None, "meta": {"status": "needs-human", "summary": "stuck"}})
        check("explicit meta.status=needs-human honored", rec["status"] == "needs-human", f"rec={rec['status']}")
        check("needs-human relabels off claimed",
              any("--remove-label" in m and "claimed" in m for m in rec["mutations"]))

        # dry-run safety: every transition recorded mutations but none hit gh
        check("dry-run recorded intended mutations without calling gh", rec["dry_run"] is True)
    finally:
        common.gh_mutate, common.ledger_emit = orig_mutate, orig_ledger  # type: ignore[assignment]


def main() -> int:
    for fn in (
        test_runs_green_with_all_deliverables,
        test_run_live_dry_run_matches_mock,
        test_budget_tracked_and_phased,
        test_no_real_side_effects,
        test_machine_is_serializable_statechart,
        test_orchestration_script_is_statechart_fragment,
        test_inversion_of_control,
        test_governor_deny_by_default,
        test_governor_budget_enforced,
        test_program_depth_recorded,
        test_yaml_workflow,
        test_intake_invoice_transitions,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — a thrown test is a failure
            check(fn.__name__, False, f"raised {type(exc).__name__}: {exc}")

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    for name, ok, detail in _RESULTS:
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {name}"
        if not ok and detail:
            line += f"  -- {detail}"
        print(line)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
