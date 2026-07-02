#!/usr/bin/env python3
"""bus.py — an in-process event bus + observer + runner for live session watching.

A run is one observed execution of a workflow. The engine's ``Interpreter`` calls
a :class:`~engine.actions.Observer` on every state enter/leave and completion
event; :class:`BusObserver` turns those callbacks into JSON events on a
:class:`Run`, which fans them out to any number of SSE subscribers *and* keeps a
replayable backlog. The server exposes runs over ``/api/sessions*``.

Today only ``baseworkflow`` is runnable here, via the deterministic
``MockActionFactory`` (real subsystem logic, no model, no network) — the same
engine the roundabout demo drives. The run is *paced* (a small per-state delay in
the observer) so the statechart visibly steps through its states in the viewer;
pacing is watch-only telemetry and never changes the run's outcome.

Stdlib only; imports the engine + ``src/baseworkflow`` lazily inside the runner.
"""
from __future__ import annotations

import itertools
import os
import queue
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))

TERMINAL = ("succeeded", "failed", "errored", "stopped")

# A canned issue so a watch run needs no network. The mock architect/admin oracles
# read these fields; the values only shape the deterministic deliverables.
DEMO_JOB: Dict[str, Any] = {
    "issue": 0,
    "title": "Demo: add a realtime debug viewer",
    "body": "Draw workflow states as a graph and watch a tick run live.",
    "labels": ["enhancement"],
}
DEMO_TRIAGE: Dict[str, Any] = {
    "action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.82,
}


class Run:
    """One observed execution. Thread-safe: the engine thread emits while HTTP
    threads subscribe/read. ``events`` is the full replayable backlog; ``subs`` are
    live SSE queues."""

    def __init__(self, run_id: str, workflow: str) -> None:
        self.id = run_id
        self.workflow = workflow
        self.status = "running"
        self.started = time.time()
        self.events: List[Dict[str, Any]] = []
        self._subs: "set[queue.Queue]" = set()
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def emit(self, event: Dict[str, Any]) -> None:
        with self._lock:
            event = {"seq": next(self._seq), "t": round(time.time() - self.started, 3), **event}
            self.events.append(event)
            for q in list(self._subs):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

    def subscribe(self) -> "Tuple[queue.Queue, List[Dict[str, Any]]]":
        """Atomically snapshot the backlog and register a live queue, so no event is
        dropped or duplicated across the handoff."""
        q: "queue.Queue" = queue.Queue(maxsize=4096)
        with self._lock:
            backlog = list(self.events)
            self._subs.add(q)
        return q, backlog

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            self._subs.discard(q)

    def finish(self, status: str) -> None:
        self.status = status
        self.emit({"type": "status", "status": status})

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id, "workflow": self.workflow, "status": self.status,
            "events": len(self.events), "elapsed": round(time.time() - self.started, 2),
        }


class BusObserver:
    """Adapts the engine's ``Observer`` protocol onto a :class:`Run`. Passive: it
    only emits telemetry (and optionally paces the run so it is watchable)."""

    def __init__(self, run: Run, pace: float = 0.3) -> None:
        self.run = run
        self.pace = pace

    def on_enter(self, state_id: str, kind: str) -> None:
        self.run.emit({"type": "enter", "state": state_id, "kind": kind})
        if self.pace and kind == "leaf":
            time.sleep(self.pace)  # inline in the engine thread → visible stepping

    def on_leave(self, state_id: str, ok: bool) -> None:
        self.run.emit({"type": "leave", "state": state_id, "ok": bool(ok)})

    def on_event(self, event: Dict[str, Any]) -> None:
        self.run.emit({"type": "event", **event})


# -- registry ---------------------------------------------------------------
class Bus:
    def __init__(self) -> None:
        self._runs: "Dict[str, Run]" = {}
        self._n = itertools.count(1)
        self._lock = threading.Lock()

    def runnable(self) -> List[str]:
        # Only baseworkflow has a wired mock runner today; others are draw-only.
        return ["baseworkflow"]

    def create(self, workflow: str) -> Run:
        with self._lock:
            run = Run(f"run-{next(self._n)}", workflow)
            self._runs[run.id] = run
            return run

    def get(self, run_id: str) -> Optional[Run]:
        return self._runs.get(run_id)

    def list(self) -> List[Dict[str, Any]]:
        return [r.summary() for r in reversed(list(self._runs.values()))]

    def start(self, workflow: str, *, pace: float = 0.3) -> Run:
        """Create a run and drive it on a daemon thread. Raises ValueError if the
        workflow has no runner wired yet."""
        if workflow not in self.runnable():
            raise ValueError(
                f"{workflow!r} is draw-only; a live mock runner is wired for: {self.runnable()}"
            )
        run = self.create(workflow)
        threading.Thread(target=self._drive, args=(run, pace), daemon=True).start()
        return run

    def _drive(self, run: Run, pace: float) -> None:
        try:
            if run.workflow == "baseworkflow":
                self._run_baseworkflow(run, pace)
            else:  # pragma: no cover — guarded by runnable()
                run.emit({"type": "error", "message": f"no runner for {run.workflow!r}"})
                run.finish("errored")
        except Exception as exc:  # noqa: BLE001 — surface any run failure to the UI
            run.emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            run.finish("errored")

    def _run_baseworkflow(self, run: Run, pace: float) -> None:
        if REPO_ROOT not in sys.path:
            sys.path.insert(0, REPO_ROOT)
        bw_dir = os.path.join(REPO_ROOT, "src", "baseworkflow")
        if bw_dir not in sys.path:
            sys.path.insert(0, bw_dir)
        import baseworkflow as bw  # noqa: E402
        from engine.actions import MockActionFactory  # noqa: E402

        run.emit({"type": "run-start", "workflow": run.workflow})
        wf = bw.BaseWorkflow(MockActionFactory(), job=DEMO_JOB, triage=DEMO_TRIAGE)
        ctx = wf.context(dry_run=True)
        ctx.observer = BusObserver(run, pace=pace)  # rides ctx into nested Programs
        result = wf.run(ctx=ctx)
        run.finish("succeeded" if result.ok else "failed")


BUS = Bus()
