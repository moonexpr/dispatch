#!/usr/bin/env python3
"""test_slice_dispatch.py — issue #171: per-slice parallel agent dispatch.

Self-asserting module (exit 0 = pass), matching the repo's shell-harness convention
(no pytest). Run: ``python3 src/baseworkflow/test_slice_dispatch.py``.

Covers the offline-verifiable acceptance criteria of #171:
  * the architect staffs each FEATURE unit with TWO agents (prototyper + tester),
    sharing a ``slice_id`` (N feature units -> 2N agent phases);
  * the wave executor fans a wave's unit-agents out CONCURRENTLY, bounded to a max,
    honours dependency wave ordering (foundation -> features -> verify; tester after
    its prototyper), retries burst-limited units with backoff, and CONTAINS a single
    unit's failure (it never aborts the wave);
  * consolidation applies each slice's commits onto ONE branch in order and skips a
    conflicting slice (fail-safe), preserving admin:consolidate_pr's single clone;
  * ``slice_id`` round-trips through the serialized script;
  * the dry-run / mock in-process Program still deserializes + runs green unchanged.

The live model/clone path is NOT exercised here (no network/model in CI); the policy
that drives it — dispatch_waves / _consolidate_clones — is tested with injected
runners/git/sleep so it is fully deterministic and side-effect-free.
"""
from __future__ import annotations

import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))   # src/baseworkflow
_SRC = os.path.dirname(_HERE)                         # src
_ROOT = os.path.dirname(_SRC)                         # repo root
for _p in (_ROOT, _SRC, _HERE, os.path.join(_HERE, "subsystems"), os.path.join(_HERE, "bindings")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import baseworkflow  # noqa: E402  (sets up package paths + registers bindings)
from engine.actions import AgentSpec, MockActionFactory, OrchestrationScript, PhaseSpec, Program  # noqa: E402
from bindings import architect, engineer  # noqa: E402

_RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(ok), detail))


# --- a 1-foundation / 3-feature / 1-verify plan, shaped like decompose.plan ----------
def _plan():
    def feat(uid, deps):
        return {"id": uid, "phase": "feature", "depends_on": deps,
                "specialization": {"label": "backend feature"},
                "deliverable": f"feature {uid}", "files": [f"{uid.lower()}.py"]}
    units = [
        {"id": "A", "phase": "foundation", "depends_on": [],
         "specialization": {"label": "backend foundation"}, "deliverable": "scaffold", "files": ["a.py"]},
        feat("B", ["A"]), feat("C", ["A"]), feat("D", ["A"]),
        {"id": "E", "phase": "verify", "depends_on": ["A", "B", "C", "D"],
         "specialization": {"label": "qa"}, "deliverable": "verify", "files": []},
    ]
    staffing = {"parallel": ["A"], "waves": [["A"], ["B", "C", "D"], ["E"]], "agent_count": 5}
    return {"units": units, "staffing": staffing, "criteria": ["works"]}


def _authored_phases():
    out = architect.author_orchestration({"plan": _plan()})
    return OrchestrationScript.from_dict(out["orchestration_script"]).phases


# ===========================================================================
def test_slice_id_round_trips() -> None:
    p = PhaseSpec(id="B", agent=AgentSpec(description="x"), slice_id="B")
    rt = PhaseSpec.from_dict(p.to_dict())
    check("slice_id survives to_dict/from_dict", rt.slice_id == "B", f"got={rt.slice_id!r}")
    # older scripts (no slice_id key) default to "" — backward compatible
    legacy = PhaseSpec.from_dict({"id": "Z", "agent": {"description": "x"}})
    check("missing slice_id defaults to ''", legacy.slice_id == "", f"got={legacy.slice_id!r}")


def test_two_agents_per_feature() -> None:
    phases = _authored_phases()
    by_id = {p.id: p for p in phases}
    # N feature units (B,C,D) -> 2N feature phases + foundation + verify = 8
    check("N feature units -> 2N+structural phases (8)", len(phases) == 8, f"n={len(phases)}")
    # each feature slice: a prototyper {uid}p and a tester {uid} sharing slice_id, the
    # tester depending on the prototyper.
    for uid in ("B", "C", "D"):
        proto, tester = by_id.get(f"{uid}p"), by_id.get(uid)
        ok = (proto is not None and tester is not None
              and proto.slice_id == uid and tester.slice_id == uid
              and tester.depends_on == (f"{uid}p",)
              and proto.depends_on == ("A",)
              and proto.agent.description.startswith("prototyper:")
              and tester.agent.description.startswith("tester:"))
        check(f"slice {uid}: prototyper + tester wired", ok,
              f"proto={proto} tester={tester}")
    # foundation + verify stay single-agent; verify waits on the testers (slice exits)
    check("foundation A is a single engineer phase", "Ap" not in by_id and by_id["A"].slice_id == "A")
    check("verify E single + depends on tester exits",
          "Ep" not in by_id and set(by_id["E"].depends_on) == {"A", "B", "C", "D"},
          f"deps={by_id['E'].depends_on}")
    # sibling feature slices share a wave -> parallel flag set so the executor fans out
    check("feature phases marked parallel", all(by_id[i].parallel for i in ("Bp", "Cp", "Dp", "B", "C", "D")))


def test_degenerate_single_feature_still_pairs() -> None:
    # one implicit feature unit (empty plan) still yields prototyper + tester (>1 phase
    # -> the live path chooses orchestrated dispatch, not the single-engineer fallback)
    phases = OrchestrationScript.from_dict(
        architect.author_orchestration({"plan": {}})["orchestration_script"]).phases
    check("empty plan -> 2 agents (proto+tester)", len(phases) == 2, f"n={len(phases)}")


# --- wave executor (pure policy; injected runner/sleep) ------------------------------
class _Recorder:
    """A fake run_unit that records call order + observed concurrency, optionally
    rendezvousing on a Barrier to *prove* concurrency, and optionally failing chosen
    phases."""

    def __init__(self, *, barrier: "threading.Barrier | None" = None, fail: "set[str] | None" = None,
                 burst_once: "set[str] | None" = None):
        self.lock = threading.Lock()
        self.starts: list = []
        self.calls: list = []
        self.inflight = 0
        self.max_inflight = 0
        self.barrier = barrier
        self.fail = fail or set()
        self.burst_once = dict.fromkeys(burst_once or set(), True)

    def __call__(self, phase):
        with self.lock:
            self.starts.append(phase.id)
            self.calls.append(phase.id)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.barrier is not None:
                try:
                    self.barrier.wait(timeout=3)
                except threading.BrokenBarrierError:
                    # Barrier rendezvous is only a best-effort concurrency proof in tests;
                    # continue so dispatch behavior remains testable even if it breaks.
                    pass
            if self.burst_once.get(phase.id):
                self.burst_once[phase.id] = False
                raise RuntimeError("rate limit exceeded (429)")
            if phase.id in self.fail:
                raise RuntimeError("boom")
            return {"clone_dir": f"/tmp/{phase.id}", "branch": "pipeline/issue-1", "status": "completed"}
        finally:
            with self.lock:
                self.inflight -= 1


def test_dispatch_invokes_2n_agents_in_wave_order() -> None:
    phases = _authored_phases()  # 8 phases: [A],[Bp,Cp,Dp],[B,C,D],[E]
    rec = _Recorder()
    outcomes = engineer.dispatch_waves(phases, rec, max_parallel=4, stagger_s=0, sleep=lambda _s: None)
    check("every authored agent dispatched (2N+structural)", len(rec.calls) == 8, f"n={len(rec.calls)}")
    check("all outcomes ok", all(o.ok for o in outcomes), f"bad={[o.phase_id for o in outcomes if not o.ok]}")
    order = rec.starts
    # ThreadPoolExecutor per wave joins before the next wave -> strict cross-wave order
    check("foundation dispatched first", order[0] == "A", f"order={order}")
    check("prototypers form wave 2", set(order[1:4]) == {"Bp", "Cp", "Dp"}, f"order={order}")
    check("testers form wave 3 (after their prototypers)", set(order[4:7]) == {"B", "C", "D"}, f"order={order}")
    check("verify dispatched last", order[7] == "E", f"order={order}")


def test_true_concurrency_within_wave() -> None:
    # 4 independent phases, max_parallel=4: a Barrier(4) releases ONLY if all four run
    # at once. Sequential dispatch would time out the barrier -> max_inflight < 4.
    phases = [PhaseSpec(id=f"P{i}", agent=AgentSpec(description="x"), slice_id=f"P{i}") for i in range(4)]
    rec = _Recorder(barrier=threading.Barrier(4))
    engineer.dispatch_waves(phases, rec, max_parallel=4, stagger_s=0, sleep=lambda _s: None)
    check("all 4 wave members ran concurrently", rec.max_inflight == 4, f"max_inflight={rec.max_inflight}")


def test_bounded_concurrency() -> None:
    # 4 independent phases, max_parallel=2: pool caps in-flight at 2; a Barrier(2)
    # forces pairs to overlap so we observe exactly 2 (deterministic, not flaky).
    phases = [PhaseSpec(id=f"P{i}", agent=AgentSpec(description="x"), slice_id=f"P{i}") for i in range(4)]
    rec = _Recorder(barrier=threading.Barrier(2))
    engineer.dispatch_waves(phases, rec, max_parallel=2, stagger_s=0, sleep=lambda _s: None)
    check("concurrency bounded to max_parallel=2", rec.max_inflight == 2, f"max_inflight={rec.max_inflight}")


def test_one_unit_failure_is_contained() -> None:
    phases = _authored_phases()
    rec = _Recorder(fail={"Cp"})  # one prototyper hard-fails
    outcomes = engineer.dispatch_waves(phases, rec, max_parallel=4, stagger_s=0, sleep=lambda _s: None)
    by = {o.phase_id: o for o in outcomes}
    check("failed unit recorded not-ok", by["Cp"].ok is False, f"Cp={by['Cp']}")
    check("sibling prototypers still ran ok", by["Bp"].ok and by["Dp"].ok)
    check("downstream waves still dispatched (fail-safe)", "E" in by and len(rec.calls) == 8, f"n={len(rec.calls)}")
    check("non-burst failure is NOT retried", by["Cp"].attempts == 1, f"attempts={by['Cp'].attempts}")


def test_burst_limit_retried_with_backoff() -> None:
    phases = [PhaseSpec(id="X", agent=AgentSpec(description="x"), slice_id="X")]
    rec = _Recorder(burst_once={"X"})  # first attempt looks rate-limited, then succeeds
    slept: list = []
    outcomes = engineer.dispatch_waves(phases, rec, max_parallel=1, stagger_s=0,
                                       max_retries=2, backoff_base_s=2.0, sleep=lambda s: slept.append(s))
    o = outcomes[0]
    check("burst-limited unit retried then succeeded", o.ok and o.attempts == 2, f"o={o}")
    check("backoff sleep applied on retry", any(s >= 2.0 for s in slept), f"slept={slept}")


def test_wave_levels_breaks_cycles() -> None:
    # a dependency cycle must not spin forever — the remainder flushes as one wave
    a = PhaseSpec(id="a", agent=AgentSpec(description="x"), depends_on=("b",))
    b = PhaseSpec(id="b", agent=AgentSpec(description="x"), depends_on=("a",))
    waves = engineer._wave_levels([a, b])
    flat = [p.id for w in waves for p in w]
    check("cyclic phases still all scheduled once", sorted(flat) == ["a", "b"], f"flat={flat}")


# --- consolidation (pure ordering + fail-safe; injected git) ------------------------
class _FakeGit:
    """Records git calls (all targeting the primary clone). A cherry-pick whose range
    names a ref in ``conflict_refs`` returns rc=1 (simulating a merge conflict)."""

    def __init__(self, conflict_refs: "set[str] | None" = None):
        self.calls: list = []
        self.conflict_refs = conflict_refs or set()

    def __call__(self, clone, *args):
        self.calls.append((clone, args))

        class _C:
            returncode = 0
            stdout = "basesha\n"
        if args and args[0] == "cherry-pick" and args[-1] != "--abort":
            rng = args[-1]  # "<base>..refs/dispatch/uN"
            if any(ref in rng for ref in self.conflict_refs):
                _C.returncode = 1
        return _C()


def test_consolidate_applies_each_slice_in_order() -> None:
    git = _FakeGit()
    units = [("B", "/tmp/B", "pipeline/issue-1"), ("D", "/tmp/D", "pipeline/issue-1")]
    applied, conflicts = engineer._consolidate_clones("/tmp/A", units, git=git, log=lambda _m: None)
    check("every non-primary slice applied", applied == ["B", "D"], f"applied={applied}")
    check("no conflicts on clean apply", conflicts == [])
    ops = [a[1][0] for a in git.calls]
    check("each slice fetched then cherry-picked", ops.count("fetch") == 2 and ops.count("cherry-pick") == 2, f"ops={ops}")


def test_consolidate_skips_conflict_fail_safe() -> None:
    git = _FakeGit(conflict_refs={"refs/dispatch/u1"})  # the 2nd non-primary slice (D)
    units = [("B", "/tmp/B", "pipeline/issue-1"), ("D", "/tmp/D", "pipeline/issue-1")]
    applied, conflicts = engineer._consolidate_clones("/tmp/A", units, git=git, log=lambda _m: None)
    check("clean slice applied, conflicting slice skipped", applied == ["B"] and conflicts == ["D"],
          f"applied={applied} conflicts={conflicts}")
    aborted = any(a[1] == ("cherry-pick", "--abort") for a in git.calls)
    check("conflicting cherry-pick was aborted (not fatal)", aborted)


# --- orchestration-phase deserialize helper -----------------------------------------
def test_orchestration_phases_helper() -> None:
    script = architect.author_orchestration({"plan": _plan()})["orchestration_script"]
    check("dict script -> phase list", len(engineer._orchestration_phases(script)) == 8)
    check("empty/None script -> [] (single-engineer fallback)", engineer._orchestration_phases(None) == [])
    check("malformed script -> [] (fail-safe)", engineer._orchestration_phases({"phases": "nope"}) == [])


# --- dry-run / mock unchanged --------------------------------------------------------
def test_dryrun_program_still_runs_green() -> None:
    # the authored multi-agent (8-phase) script still deserializes into a runnable
    # in-process Program and runs green under the mock factory (offline path unchanged).
    script = OrchestrationScript.from_dict(
        architect.author_orchestration({"plan": _plan()})["orchestration_script"])
    program = MockActionFactory().deserialize(script)
    check("multi-agent script deserializes to a Program", isinstance(program, Program) and program.kind == "program")
    from engine.actions import BudgetMeter, Context, MemoryShelf, Shelves
    ctx = Context(shelves=Shelves(MemoryShelf("input"), MemoryShelf("deliverables"), MemoryShelf("shared")),
                  meter=BudgetMeter(1_000_000), dry_run=True)
    r = program.run(None, ctx)
    check("8-phase Program runs green in-process", r.ok, repr(getattr(r, "error", "")) if not r.ok else "")


def main() -> int:
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(name, False, f"raised {type(exc).__name__}: {exc}")
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    for n, ok, d in _RESULTS:
        sys.stderr.write(f"{'PASS' if ok else 'FAIL'}  {n}{(' — ' + d) if d and not ok else ''}\n")
    sys.stderr.write(f"\n{len(_RESULTS) - len(failed)}/{len(_RESULTS)} checks passed\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
