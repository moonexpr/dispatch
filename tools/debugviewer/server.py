#!/usr/bin/env python3
"""server.py — the debug-viewer HTTP server (stdlib only).

A tiny ``http.server`` that discovers every workflow YAML under
``app/workflows/``, compiles each to a drawable graph via
``foundation.workflow.workflow_to_graph``, and serves a single-page SVG viewer that
draws the states as a graph and lets you switch between workflows.

Endpoints
---------
    GET  /                          -> the viewer (static/viewer.html)
    GET  /api/workflows             -> [{name, file, ok, error, phases, node_count}]
    GET  /api/workflows/<name>      -> the graph {name, phases, budgets, nodes, edges}
    GET  /api/sessions              -> {runs: [...], runnable: [...]}
    POST /api/sessions/run          -> start an observed run; {id, workflow, status}
                                       body/query: workflow=<name>&pace=<seconds>
    GET  /api/sessions/<id>/stream  -> SSE: live enter/leave/event/status stream
    POST /api/sessions/<id>/stop    -> request the run stop

Graphs are recompiled on every request, so editing a workflow YAML and hitting
refresh in the browser shows the new statechart. A run streams the engine's own
``Interpreter`` enter/leave/event callbacks (via ``bus.BusObserver``) keyed by the
same ``state_id`` the graph nodes carry, so the viewer lights the active state
live. Only ``baseworkflow`` has a mock runner wired today (see ``bus.py``); other
workflows are draw-only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

# Repo root = three levels up from this file (tools/debugviewer/server.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import queue  # noqa: E402

from foundation.workflow import load_workflow, workflow_to_graph  # noqa: E402

from .bus import BUS  # noqa: E402

WORKFLOWS_DIR = os.path.join(REPO_ROOT, "app", "workflows")
STATIC_DIR = os.path.join(_HERE, "static")

# Optional token registries, so ``controller:`` references (and dynamic supersede
# targets, ADR-003) expand into their action subgraphs instead of drawing as
# single unresolved boundary nodes. Best-effort: a workflow with no mapped (or
# un-importable) registry still draws — its controllers just stay collapsed.
_REGISTRIES: Dict[str, str] = {
    "baseworkflow": "baseworkflow.bindings:build_registry",
    "websitewf": "websitewf.bindings:build_registry",
}
_registry_cache: Dict[str, Any] = {}


def _registry_for(name: str) -> Any:
    """Resolve (and cache) the token registry for a workflow, or ``None``. Mirrors
    ``bus.py``'s sys.path discipline: ``import baseworkflow`` must hit the PACKAGE
    (its __init__ is the composition root), so REPO_ROOT wins over baseworkflow/,
    which is still needed for the package's bare-internal imports."""
    if name in _registry_cache:
        return _registry_cache[name]
    spec = _REGISTRIES.get(name)
    reg = None
    if spec:
        try:
            import importlib

            bw_dir = os.path.join(REPO_ROOT, "baseworkflow")
            for p in (bw_dir, REPO_ROOT):
                if p in sys.path:
                    sys.path.remove(p)
            sys.path.insert(0, bw_dir)
            sys.path.insert(0, REPO_ROOT)
            mod_name, _, fn = spec.partition(":")
            reg = getattr(importlib.import_module(mod_name), fn or "build_registry")()
        except Exception:  # noqa: BLE001 — draw-only fallback; a bad registry never breaks the viewer
            reg = None
    _registry_cache[name] = reg
    return reg


# -- workflow discovery / compilation --------------------------------------
def _workflow_files() -> List[str]:
    if not os.path.isdir(WORKFLOWS_DIR):
        return []
    return sorted(
        f for f in os.listdir(WORKFLOWS_DIR)
        if f.endswith((".yml", ".yaml")) and not f.startswith(".")
    )


def _name_of(filename: str) -> str:
    return os.path.splitext(filename)[0]


def _compile_graph(filename: str) -> Dict[str, Any]:
    path = os.path.join(WORKFLOWS_DIR, filename)
    return workflow_to_graph(load_workflow(path), registry=_registry_for(_name_of(filename)))


def list_workflows() -> List[Dict[str, Any]]:
    """One summary row per workflow file — ``ok=False`` with ``error`` if the YAML
    fails to load, so the viewer can show the failure instead of an empty list."""
    out: List[Dict[str, Any]] = []
    for fn in _workflow_files():
        row: Dict[str, Any] = {"name": _name_of(fn), "file": fn}
        try:
            g = _compile_graph(fn)
            row.update(ok=True, phases=g["phases"], node_count=len(g["nodes"]))
        except Exception as exc:  # noqa: BLE001 — surface load errors to the UI
            row.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        out.append(row)
    return out


def graph_for(name: str) -> Optional[Dict[str, Any]]:
    for fn in _workflow_files():
        if _name_of(fn) == name:
            return _compile_graph(fn)
    return None


# -- live sessions ----------------------------------------------------------
def list_sessions() -> Dict[str, Any]:
    return {"runs": BUS.list(), "runnable": BUS.runnable()}


# -- HTTP handler -----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "dispatch-debugviewer/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    # -- response helpers ---------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, indent=2).encode("utf-8"), "application/json; charset=utf-8")

    def _static(self, rel: str, ctype: str) -> None:
        path = os.path.join(STATIC_DIR, rel)
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            self._json({"error": f"missing static asset {rel!r}"}, 404)
            return
        self._send(200, body, ctype)

    def _query(self) -> Dict[str, str]:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    # -- routing ------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        route = unquote(urlparse(self.path).path)
        try:
            if route in ("/", "/index.html"):
                self._static("viewer.html", "text/html; charset=utf-8")
            elif route == "/api/workflows":
                self._json(list_workflows())
            elif route.startswith("/api/workflows/"):
                name = route[len("/api/workflows/"):]
                graph = graph_for(name)
                if graph is None:
                    self._json({"error": f"no workflow named {name!r}"}, 404)
                else:
                    self._json(graph)
            elif route == "/api/sessions":
                self._json(list_sessions())
            elif route.startswith("/api/sessions/") and route.endswith("/stream"):
                self._sessions_stream(route[len("/api/sessions/"):-len("/stream")])
            else:
                self._json({"error": f"not found: {route}"}, 404)
        except BrokenPipeError:
            # Client disconnected while we were writing the response; ignore.
            sys.stderr.write("  client disconnected (BrokenPipeError)\n")
            return
        except Exception as exc:  # noqa: BLE001 — never 500-crash the whole server
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        route = unquote(urlparse(self.path).path)
        try:
            if route == "/api/sessions/run":
                q = self._query()
                workflow = q.get("workflow", "baseworkflow")
                try:
                    pace = float(q.get("pace", "0.3"))
                except ValueError:
                    pace = 0.3
                try:
                    run = BUS.start(workflow, pace=max(0.0, min(2.0, pace)))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                self._json(run.summary(), 201)
            elif route.startswith("/api/sessions/") and route.endswith("/stop"):
                rid = route[len("/api/sessions/"):-len("/stop")]
                run = BUS.get(rid)
                if run is None:
                    self._json({"error": f"no run {rid!r}"}, 404)
                else:
                    run.request_stop()
                    self._json(run.summary())
            else:
                self._json({"error": f"not found: {route}"}, 404)
        except BrokenPipeError:
            # Client disconnected before the response finished writing.
            # This is expected for aborted HTTP requests; ignore quietly.
            pass
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _sessions_stream(self, run_id: str) -> None:
        """Long-lived SSE stream of one run's events: backlog first (so a late
        subscriber sees the whole run), then live events until a terminal status.
        A ``: ping`` comment every couple seconds keeps the socket warm."""
        run = BUS.get(run_id)
        if run is None:
            self._json({"error": f"no run {run_id!r}"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q, backlog = run.subscribe()
        try:
            for ev in backlog:
                self._sse(ev)
            terminal = {"succeeded", "failed", "errored", "stopped"}
            # If the run already finished, the backlog carried its final status.
            done = any(e.get("type") == "status" and e.get("status") in terminal for e in backlog)
            while not done:
                try:
                    ev = q.get(timeout=2.0)
                    self._sse(ev)
                    if ev.get("type") == "status" and ev.get("status") in terminal:
                        done = True
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    if run.status in terminal:
                        done = True
        except (BrokenPipeError, ConnectionResetError):
            # Client disconnected mid-stream; this is expected for SSE and can be ignored.
            pass
        finally:
            run.unsubscribe(q)

    def _sse(self, ev: Dict[str, Any]) -> None:
        self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode("utf-8"))
        self.wfile.flush()


def serve(host: str, port: int, open_browser: bool) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    n = len([r for r in list_workflows() if r.get("ok")])
    print(f"dispatch debug viewer → {url}")
    print(f"  serving {n} workflow(s) from {os.path.relpath(WORKFLOWS_DIR, REPO_ROOT)}/")
    print(f"  live runs: {', '.join(BUS.runnable())} (mock, no network) — 'Run & watch' in the UI")
    print("  Ctrl-C to stop")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="tools.debugviewer", description="Realtime workflow debug viewer.")
    p.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8787, help="bind port (default 8787)")
    p.add_argument("--open", action="store_true", help="open the viewer in a browser on start")
    args = p.parse_args(argv)
    serve(args.host, args.port, args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
