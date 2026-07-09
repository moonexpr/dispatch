#!/usr/bin/env python3
"""bus.py — an in-process event bus + observer + runner for live session watching.

A run is one observed execution of a workflow. The engine's ``Interpreter`` calls
a :class:`~foundation.actions.Observer` on every state enter/leave and completion
event; :class:`BusObserver` turns those callbacks into JSON events on a
:class:`Run`, which fans them out to any number of SSE subscribers *and* keeps a
replayable backlog. The server exposes runs over ``/api/sessions*``.

Today only ``baseworkflow`` is runnable here, via the deterministic
``MockActionFactory`` (real subsystem logic, no model, no network) — the same
engine the roundabout demo drives. The run is *paced* (a small per-state delay in
the observer) so the statechart visibly steps through its states in the viewer;
pacing is watch-only telemetry and never changes the run's outcome.

Stdlib only; imports ``foundation`` + ``baseworkflow`` lazily inside the runner.
"""
from __future__ import annotations

import glob
import itertools
import json
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
                    # Slow subscriber: drop this live event for that queue so one
                    # backpressured client cannot block emission to others.
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

    # -- external live runs: tail sidecars written by out-of-process runs --------
    @staticmethod
    def _trace_dir() -> str:
        try:
            from foundation import trace
            return trace.trace_dir()
        except Exception:  # noqa: BLE001
            return os.path.join(REPO_ROOT, ".dispatch", "live-runs")

    @staticmethod
    def _read_sidecar(path: str) -> Dict[str, Any]:
        """Cheap scan of a sidecar for its list-view summary (workflow, status, count).
        Reads only whole lines; a partial final line (writer mid-flush) is ignored."""
        wf, status, started, n = None, "running", None, 0
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.endswith("\n"):
                        break  # partial trailing line — not yet complete
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    n += 1
                    if ev.get("type") == "meta":
                        wf, started = ev.get("workflow"), ev.get("started")
                    elif ev.get("type") == "status":
                        status = ev.get("status") or status
        except OSError:
            pass
        return {"workflow": wf, "status": status, "started": started, "events": n}

    def list_external(self) -> List[Dict[str, Any]]:
        """Discover live-run sidecars (newest first) written by out-of-process runs."""
        try:
            files = sorted(glob.glob(os.path.join(self._trace_dir(), "*.jsonl")),
                           key=os.path.getmtime, reverse=True)
        except OSError:
            files = []
        out: List[Dict[str, Any]] = []
        for p in files[:50]:
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                mtime = 0.0
            # mtime lets the UI tell a genuinely-active run from a sidecar whose writer
            # died without a terminal status line (which otherwise reads "running" forever).
            out.append({"id": os.path.splitext(os.path.basename(p))[0],
                        "path": p, "mtime": mtime, **self._read_sidecar(p)})
        return out

    def attach_external(self, run_id: str) -> Optional[Run]:
        """Tail the sidecar named ``<run_id>.jsonl`` into a Run so the browser can
        stream it exactly like an in-process run. Reuses a still-live attachment."""
        path = os.path.join(self._trace_dir(), run_id + ".jsonl")
        if not os.path.isfile(path):
            return None
        rid = "ext-" + run_id
        with self._lock:
            existing = self._runs.get(rid)
            if existing is not None and existing.status == "running":
                return existing
            run = Run(rid, self._read_sidecar(path).get("workflow") or "external")
            self._runs[rid] = run
        threading.Thread(target=self._tail_external, args=(run, path), daemon=True).start()
        return run

    def _tail_external(self, run: Run, path: str) -> None:
        """Follow a sidecar, replaying complete JSON lines onto ``run`` as they land.
        Finishes on the terminal ``status`` line, or after a long idle gap (writer gone)."""
        POLL, MAX_IDLE = 0.4, 180.0
        consumed, idle = 0, 0.0
        try:
            while not run.stopping:
                try:
                    with open(path, encoding="utf-8") as fh:
                        data = fh.read()
                except OSError:
                    data = ""
                lines = data.split("\n")
                complete = lines[:-1]  # last element is "" (trailing \n) or a partial line
                fresh = complete[consumed:]
                if fresh:
                    idle = 0.0
                    for line in fresh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        kind = ev.get("type")
                        if kind == "meta":
                            run.emit({"type": "run-start", "workflow": ev.get("workflow")})
                        elif kind == "status":
                            run.finish(ev.get("status") or "succeeded")
                            return
                        else:
                            run.emit(ev)  # enter/leave/event/error pass straight through
                    consumed = len(complete)
                else:
                    idle += POLL
                    if idle >= MAX_IDLE:
                        run.finish("stopped")  # writer gone / stalled — stop tailing
                        return
                time.sleep(POLL)
        except Exception as exc:  # noqa: BLE001 — surface any tail failure to the UI
            run.emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            run.finish("errored")

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
        import importlib

        # `import baseworkflow` MUST resolve to the PACKAGE (its __init__ is the
        # composition root that registers the Tuning service before any subsystem
        # resolves it) — so REPO_ROOT must win over the baseworkflow/ dir on the
        # path. The dir is still needed on the path for the package's bare-internal
        # imports (`import bindings`, `import decompose`). Force order: REPO_ROOT
        # first, baseworkflow/ second, regardless of prior sys.path state.
        bw_dir = os.path.join(REPO_ROOT, "baseworkflow")
        for p in (bw_dir, REPO_ROOT):
            if p in sys.path:
                sys.path.remove(p)
        sys.path.insert(0, bw_dir)
        sys.path.insert(0, REPO_ROOT)
        bw = importlib.import_module("baseworkflow")
        from foundation.actions import MockActionFactory  # noqa: E402

        run.emit({"type": "run-start", "workflow": run.workflow})
        wf = bw.BaseWorkflow(MockActionFactory(), job=DEMO_JOB, triage=DEMO_TRIAGE)
        ctx = wf.context(dry_run=True)
        ctx.observer = BusObserver(run, pace=pace)  # rides ctx into nested Programs
        result = wf.run(ctx=ctx)
        run.finish("succeeded" if result.ok else "failed")


BUS = Bus()
