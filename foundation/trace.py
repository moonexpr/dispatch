#!/usr/bin/env python3
"""trace.py — a file-writing run Observer + shared live-trace-dir resolution.

The debug viewer (``tools/debugviewer``) can only observe runs in its own process
(its in-process ``bus.py``). A real live run — e.g. ``app/scripts/oneshot_feed.py``
driving ``run_live`` — happens in a *separate* process, so the viewer cannot see it.
This module is the one cross-process channel between the two:

  * A live run attaches a :class:`FileObserver` via ``ctx.observer``. The engine's
    ``Interpreter`` drives its ``on_enter`` / ``on_leave`` / ``on_event`` hooks (the
    same hooks the viewer's ``BusObserver`` uses), and each becomes a JSON line in a
    per-run sidecar file under :func:`trace_dir`.
  * The viewer tails that sidecar and replays the lines onto a ``Run``, so an
    out-of-process live tick lights up the statechart exactly like an in-process one.

Because both sides emit the Interpreter's ``state.id``, the viewer's node highlight
map keys line up with zero translation — the reason to carry structured events
instead of parsing the human ``→``/``←`` trace text.

Sidecar format: JSONL — a ``meta`` header line, then ``enter`` / ``leave`` / ``event``
lines, then a terminal ``status`` line. Passive telemetry only (Observer contract):
every write is best-effort and never raises into the run.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

_ENV_DIR = "DISPATCH_LIVE_TRACE_DIR"
_ENV_ON = "DISPATCH_LIVE_TRACE"


def trace_dir() -> str:
    """Directory holding live-run sidecars. Override with ``$DISPATCH_LIVE_TRACE_DIR``;
    otherwise ``<repo>/.dispatch/live-runs`` (repo root is one level above ``foundation/``)."""
    d = os.environ.get(_ENV_DIR)
    if not d:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = os.path.join(repo, ".dispatch", "live-runs")
    return d


def tracing_enabled() -> bool:
    """Live tracing is opt-in: on when a watcher has set ``$DISPATCH_LIVE_TRACE`` (any
    truthy value) or pointed ``$DISPATCH_LIVE_TRACE_DIR`` somewhere. Off by default, so a
    normal run writes no sidecar."""
    return bool(os.environ.get(_ENV_DIR) or os.environ.get(_ENV_ON))


class FileObserver:
    """Writes each Interpreter callback as a JSON line to a per-run sidecar. One per
    run; best-effort and non-raising (Observer contract — telemetry must never break
    the run it watches)."""

    def __init__(self, path: str, *, workflow: str) -> None:
        self.path = path
        self._started = time.time()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        self._write({"type": "meta", "workflow": workflow, "started": self._started}, mode="w")

    def _write(self, event: Dict[str, Any], *, mode: str = "a") -> None:
        try:
            rec = {"t": round(time.time() - self._started, 3), **event}
            with open(self.path, mode, encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
                fh.flush()
        except Exception:  # noqa: BLE001 — a watcher must never break the run
            pass

    def on_enter(self, state_id: str, kind: str) -> None:
        self._write({"type": "enter", "state": state_id, "kind": kind})

    def on_leave(self, state_id: str, ok: bool) -> None:
        self._write({"type": "leave", "state": state_id, "ok": bool(ok)})

    def on_event(self, event: Dict[str, Any]) -> None:
        self._write({"type": "event", **event})

    def finish(self, status: str) -> None:
        """Write the terminal status line. Idempotent enough for best-effort use."""
        self._write({"type": "status", "status": status})


def new_observer(workflow: str, *, run_id: Optional[str] = None) -> "FileObserver":
    """A :class:`FileObserver` writing to ``trace_dir()/<run_id>.jsonl``. When ``run_id``
    is not given it is ``<workflow>-<ms-timestamp>`` — unique per run within a dir."""
    rid = run_id or f"{workflow}-{int(time.time() * 1000)}"
    return FileObserver(os.path.join(trace_dir(), rid + ".jsonl"), workflow=workflow)
