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
    GET  /api/workflows/<name>/examples
                                    -> {ok, examples}: realistic "shelf.key"->value
                                       snapshot from the offline mock, for typed probe forms
    GET  /api/sessions              -> {runs: [...], runnable: [...]}
    GET  /api/sessions/live         -> {runs: [...]}: out-of-process live runs, from
                                       sidecars an external run wrote (foundation.trace)
    POST /api/probe                 -> run ONE action in isolation; body {workflow,
                                       token, inputs, payload, live}
                                       -> {ok, result, outputs, changes, raised, trace}
    POST /api/sessions/run          -> start an observed (in-process mock) run
                                       body/query: workflow=<name>&pace=<seconds>
    POST /api/sessions/attach       -> tail an external live sidecar into a Run so it
                                       streams like a run; body {id} -> {id, workflow, status}
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


# -- single-action probe ----------------------------------------------------
def _json_safe(v: Any) -> Any:
    """Coerce anything into a JSON-encodable shape (non-serialisable → str)."""
    return json.loads(json.dumps(v, default=str))


def _mock_services() -> Any:
    """The offline service family, so a probed action that resolves a service need
    (``ctx.service``) has something to reach. Best-effort — ``None`` if unavailable
    (the action then fails its service lookup, which the probe surfaces)."""
    try:
        import importlib

        return importlib.import_module("baseworkflow.services").build_mock_services()
    except Exception:  # noqa: BLE001
        return None


# Per-workflow production factory (enforcing governors, FileShelf, and the LIVE
# inference runner that calls ``foundation.models.chat``). Mirrors ``_REGISTRIES``;
# a workflow with no entry has no live probe (the toggle reports it and stays mock).
_REAL_FACTORIES: Dict[str, str] = {
    "baseworkflow": "baseworkflow.bindings.architect:ArchitectFactory",
}


def _real_factory_for(name: str, shelf_root: str) -> Any:
    """Instantiate the workflow's production factory rooted at ``shelf_root``, or
    ``None`` if none is wired. Same sys.path discipline as ``_registry_for`` so the
    package (composition root) wins over the bare ``baseworkflow/`` dir."""
    spec = _REAL_FACTORIES.get(name)
    if not spec:
        return None
    import importlib

    bw_dir = os.path.join(REPO_ROOT, "baseworkflow")
    for p in (bw_dir, REPO_ROOT):
        if p in sys.path:
            sys.path.remove(p)
    sys.path.insert(0, bw_dir)
    sys.path.insert(0, REPO_ROOT)
    mod_name, _, cls = spec.partition(":")
    return getattr(importlib.import_module(mod_name), cls)(shelf_root=shelf_root)


def probe_action(workflow: str, token: str, inputs: Dict[str, Any],
                 payload: Any = None, live: bool = False) -> Dict[str, Any]:
    """Run ONE action in isolation: seed the given shelf values, execute the action's
    compiled body (governors and all), and report its ``Result``, what it wrote to its
    declared outputs, and any events it raised (e.g. a supersede request). ``inputs``
    maps ``"shelf.key" -> value`` (the refs the action reads).

    Two modes. **Mock** (default): ``MockActionFactory`` + ``ctx.dry_run`` — no network,
    no model, no real mutation; a ``kind: inference`` action runs its deterministic
    compiled-in oracle. **Live** (``live=True``): the workflow's production factory with
    ``ctx.dry_run`` OFF, so each ``kind: inference`` action routes to its live worker and
    calls the real model (``foundation.models.chat``) — this costs tokens and needs API
    keys / network. Services stay offline (mock) either way so a probed action never
    blocks on a TTY / gh need, and live shelves use an isolated temp root torn down after
    the run, so live inference still performs no durable external mutation."""
    prev_dry = os.environ.get("PIPELINE_DRY_RUN")
    os.environ["PIPELINE_DRY_RUN"] = "0" if live else "1"
    shelf_tmp: Optional[str] = None
    try:
        reg = _registry_for(workflow)
        if reg is None:
            return {"error": f"{workflow!r} has no token registry wired, so its actions can't be probed."}
        fn = next((f for f in _workflow_files() if _name_of(f) == workflow), None)
        if fn is None:
            return {"error": f"no workflow named {workflow!r}"}
        doc = load_workflow(os.path.join(WORKFLOWS_DIR, fn))
        manifest = (getattr(doc, "manifests", {}) or {}).get(token)
        if manifest is None:
            return {"error": f"no action {token!r} in {workflow!r}"}

        # Validate the seeded inputs against the declared contract (the tooling surface
        # of the staged enforcement). Non-blocking: a probe still runs so you can see
        # what a malformed value does, but the violations are reported to the UI.
        schemas = getattr(doc, "schemas", {}) or {}
        validation: Dict[str, List[str]] = {}
        for ref, value in (inputs or {}).items():
            schema = schemas.get(str(ref))
            if schema is not None:
                errs = schema.validate(value)
                if errs:
                    validation[str(ref)] = errs

        from foundation.actions import BudgetMeter, Context, MockActionFactory
        from foundation.workflow.nodes import ActionRefNode
        from foundation.workflow.visitor import CompileVisitor

        if live:
            import tempfile

            shelf_tmp = tempfile.mkdtemp(prefix="dispatch-probe-")
            factory = _real_factory_for(workflow, shelf_tmp)
            if factory is None:
                return {"error": f"{workflow!r} has no live factory wired; live inference is unavailable for it."}
        else:
            factory = MockActionFactory()
        cv = CompileVisitor(factory, reg)
        cv.budgets = dict(doc.budgets or {})  # so a budget-Governor cap lookup resolves
        try:
            action = cv.visit_action_ref(ActionRefNode(token=token, manifest=manifest))
        except Exception as exc:  # noqa: BLE001 — unregistered bind etc.
            return {"error": f"could not build {token!r}: {type(exc).__name__}: {exc}"}

        shelves = factory.shelves()
        ctx = Context(shelves=shelves, meter=BudgetMeter(10 ** 12, label="probe"),
                      dry_run=not live, services=_mock_services())
        seeded: List[str] = []
        for ref, value in (inputs or {}).items():
            if "." not in str(ref):
                continue
            shelf, key = str(ref).split(".", 1)
            try:
                getattr(shelves, shelf).put(key, value)
                seeded.append(ref)
            except AttributeError:
                pass

        # Snapshot the two mutable shelves AFTER seeding, so the diff below reflects only
        # what this action itself writes — the whole point of a probe is to see the change
        # it makes to ``deliverables`` and ``shared``.
        def _snap(name: str) -> Dict[str, Any]:
            sh = getattr(shelves, name, None)
            try:
                return dict(sh.snapshot()) if sh is not None else {}
            except Exception:  # noqa: BLE001
                return {}

        before = {"deliverables": _snap("deliverables"), "shared": _snap("shared")}

        result = action.run(payload, ctx)  # Action.run traps exceptions into an Error Result
        outputs: Dict[str, Any] = {}
        for r in manifest.outputs:
            try:
                outputs[r.ref] = getattr(shelves, r.shelf).get(r.key)
            except Exception:  # noqa: BLE001
                pass

        # Diff deliverables + shared to surface exactly what the action mutated (new vs
        # changed keys). This is what the inspector highlights after a probe.
        changes: List[Dict[str, Any]] = []
        for name in ("deliverables", "shared"):
            after = _snap(name)
            prev = before[name]
            for k, v in after.items():
                if k not in prev:
                    changes.append({"ref": f"{name}.{k}", "status": "new", "value": v})
                else:
                    try:
                        differs = prev[k] != v
                    except Exception:  # noqa: BLE001 — unorderable/odd types → treat as changed
                        differs = True
                    if differs:
                        changes.append({"ref": f"{name}.{k}", "status": "changed", "value": v})

        raised = []
        for e in getattr(ctx, "raised", []):
            p = getattr(e, "payload", None)
            p = p.to_dict() if hasattr(p, "to_dict") else p  # SupersedeRequest → readable dict
            name = e.name.strip("_") if str(e.name).startswith("__") else e.name  # "__supersede__" → "supersede"
            raised.append({"name": name, "payload": _json_safe(p)})
        return _json_safe({
            "ok": bool(result.ok),
            "token": token,
            "live": bool(live),
            "seeded": seeded,
            "validation": validation,  # {ref: [contract violations]} — empty when inputs conform
            "result": {
                "ok": bool(result.ok),
                "value": getattr(result, "value", None),
                "meta": getattr(result, "meta", {}) or {},
                "detail": getattr(result, "detail", "") or None,
                "error": None if result.ok else str(getattr(result, "error", "")),
            },
            "outputs": outputs,
            "changes": changes,  # [{ref, status: new|changed, value}] over deliverables + shared
            "raised": raised,
            "trace": ctx.trace,
        })
    finally:
        if prev_dry is None:
            os.environ.pop("PIPELINE_DRY_RUN", None)
        else:
            os.environ["PIPELINE_DRY_RUN"] = prev_dry
        if shelf_tmp:
            import shutil

            shutil.rmtree(shelf_tmp, ignore_errors=True)


# -- example shelf snapshots (schema-driven probe forms) --------------------
# Canonical, deterministic fixtures fed to a workflow's offline mock so we can
# read back *real* shelf values and reflect their shapes into typed form fields.
_EXAMPLE_JOB = {
    "issue": 9001,
    "title": "Add retry with backoff to fetch",
    "body": "Implement retry.\n\n## Acceptance criteria\n- retries 3x\n- exponential backoff\n- gives up after cap",
    "labels": ["enhancement"],
    "discovered": ["foundation/proc.py", "foundation/runtime.py"],
}
_EXAMPLE_TRIAGE = {"action": "implement", "scope": "m", "route": "gen-default", "confidence": 0.82}

# Workflows with a deterministic offline mock runner we can introspect (mirrors
# ``_REGISTRIES``). A draw-only workflow simply yields no examples and the probe
# UI falls back to freeform inputs.
_EXAMPLE_RUNNERS: Dict[str, str] = {"baseworkflow": "baseworkflow.baseworkflow:run_mock"}
_example_cache: Dict[str, Dict[str, Any]] = {}


def example_shelf(workflow: str) -> Dict[str, Any]:
    """A realistic ``"shelf.key" -> value`` snapshot of every ref a workflow's
    actions read/write, derived by running its offline mock once (no model, no
    network) and reading the input + deliverables shelves. The probe UI reflects
    these value shapes into typed, prefilled form controls. Best-effort and
    cached: an un-runnable workflow returns ``{}`` (UI falls back to freeform)."""
    if workflow in _example_cache:
        return _example_cache[workflow]
    spec = _EXAMPLE_RUNNERS.get(workflow)
    examples: Dict[str, Any] = {}
    if spec:
        try:
            import contextlib
            import importlib
            import io

            # Same sys.path discipline as ``_registry_for``: the package __init__
            # is the composition root, so REPO_ROOT must win, but baseworkflow/
            # is still needed for the package's bare-internal imports.
            bw_dir = os.path.join(REPO_ROOT, "baseworkflow")
            for p in (bw_dir, REPO_ROOT):
                if p in sys.path:
                    sys.path.remove(p)
            sys.path.insert(0, bw_dir)
            sys.path.insert(0, REPO_ROOT)
            os.environ["PIPELINE_DRY_RUN"] = "1"  # the mock never mutates anything real
            mod_name, _, fn = spec.partition(":")
            run_mock = getattr(importlib.import_module(mod_name), fn or "run_mock")
            with contextlib.redirect_stdout(io.StringIO()):  # swallow the mock's DRY-RUN chatter
                summary = run_mock(_EXAMPLE_JOB, _EXAMPLE_TRIAGE)
            wf = summary.get("workflow")
            shelves = getattr(wf, "shelves", None)
            for kind in ("input", "deliverables", "shared"):
                shelf = getattr(shelves, kind, None)
                snap = shelf.snapshot() if shelf is not None and hasattr(shelf, "snapshot") else {}
                for key, value in (snap or {}).items():
                    examples[f"{kind}.{key}"] = _json_safe(value)
        except Exception:  # noqa: BLE001 — best-effort; the UI falls back to freeform inputs
            examples = {}
    _example_cache[workflow] = examples
    return examples


# -- HTTP handler -----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "dispatch-debugviewer/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    # -- response helpers ---------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
        extra = extra or {}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in extra:  # caller may override (vendored assets cache long)
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
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

    def _static_gz(self, rel: str, ctype: str) -> None:
        """Serve a vendored asset, preferring its pre-gzipped sibling when the client
        accepts gzip (elk.bundled.js is ~1.5MB raw, ~0.45MB gzipped). Vendored assets
        are content-stable, so they cache long instead of the app-wide no-store."""
        accepts_gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
        base = os.path.join(STATIC_DIR, rel)
        extra = {"Cache-Control": "public, max-age=31536000, immutable"}
        try:
            if accepts_gz and os.path.exists(base + ".gz"):
                with open(base + ".gz", "rb") as fh:
                    body = fh.read()
                extra["Content-Encoding"] = "gzip"
            else:
                with open(base, "rb") as fh:
                    body = fh.read()
        except FileNotFoundError:
            self._json({"error": f"missing vendored asset {rel!r}"}, 404)
            return
        self._send(200, body, ctype, extra)

    def _query(self) -> Dict[str, str]:
        q = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in q.items()}

    def _json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routing ------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        route = unquote(urlparse(self.path).path)
        try:
            if route in ("/", "/index.html"):
                self._static("viewer.html", "text/html; charset=utf-8")
            elif route == "/vendor/elk.bundled.js":
                self._static_gz("vendor/elk.bundled.js", "application/javascript; charset=utf-8")
            elif route == "/api/sessions/live":
                self._json({"runs": BUS.list_external()})
            elif route == "/api/workflows":
                self._json(list_workflows())
            elif route.startswith("/api/workflows/") and route.endswith("/examples"):
                name = route[len("/api/workflows/"):-len("/examples")]
                ex = example_shelf(name)
                self._json({"ok": bool(ex), "examples": ex})
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
            if route == "/api/probe":
                body = self._json_body()
                res = probe_action(
                    str(body.get("workflow") or ""),
                    str(body.get("token") or ""),
                    body.get("inputs") or {},
                    body.get("payload"),
                    live=bool(body.get("live")),
                )
                self._json(res, 200 if not res.get("error") else 400)
            elif route == "/api/sessions/run":
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
            elif route == "/api/sessions/attach":
                body = self._json_body()
                rid = str(body.get("id") or "")
                run = BUS.attach_external(rid) if rid else None
                if run is None:
                    self._json({"error": f"no live sidecar {rid!r} in {BUS._trace_dir()!r}"}, 404)
                else:
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
