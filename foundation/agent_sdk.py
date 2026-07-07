#!/usr/bin/env python3
"""agent_sdk.py — a generic, agent-agnostic Claude Agent backend.

This is the *agentic, tool-using* layer, distinct from ``foundation.models`` (the
request/response LLM API abstraction). Where ``models.chat`` sends one prompt and
reads one completion, an :class:`AgentBackend` drives a **headless agent session** —
the ``claude`` CLI or ``claude_agent_sdk`` — inside a working directory, with a tool
set, subscription-auth isolation, and optional sub-agents (multi-agent fan-out).

The backend is agent-agnostic: any caller binds a backend via the :func:`make_backend`
**factory** and feeds it an :class:`AgentRunSpec`. The caller-specific contract —
what prompt to send and how to interpret the result — stays in the caller, not here.

This is the **backend** (implementation) side of a Bridge: the *abstraction* side is
any layer that composes :class:`AgentBackend` and owns the role identity (system
prompt, tool set, default model). That layer is backend-agnostic; it reaches one of
these backends through :func:`make_backend`, so the same session definition runs on
the ``claude`` CLI, the Agent SDK, or — via :class:`ChatBackend` over
``foundation.models.chat`` — a single-shot model. The ``system_prompt`` carried on the
spec is the caller's persona, sent distinctly from the per-session ``prompt``.

Subscription auth: every backend copies ``os.environ`` with ``ANTHROPIC_API_KEY``
removed, so the spawned ``claude`` uses the logged-in subscription rather than API
billing, and (CLI path) repoints ``CLAUDE_CONFIG_DIR`` to a throwaway dir seeded
with ONLY the login + credential (see :func:`seed_config_auth`) so the operator's
``~/.claude`` (CLAUDE.md / memory / hooks) never poisons the session (issue #159).

Leaf-module discipline: stdlib only at import time; ``claude_agent_sdk`` is imported
lazily inside the SDK backend so the OFFLINE / CLI path never needs it installed.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from foundation import proc  # capturing subprocess wrapper (ProcError/ProcTimeout on failure)

# The default agent tool set (the SDK path passes this explicitly; the CLI path
# runs under --permission-mode bypassPermissions and lets the lead use every tool).
DEFAULT_TOOLS: List[str] = ["Read", "Edit", "Write", "Bash", "Agent", "Task"]


@dataclass
class AgentResult:
    """A backend-uniform result shim so callers read one shape regardless of which
    backend produced it (mirrors the SDK ``ResultMessage`` surface)."""

    result: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    total_cost_usd: Optional[float] = None
    is_error: bool = False
    num_turns: Optional[int] = None
    # True when the session was killed by the wall-clock cap. A backend sets this (not
    # the caller) so timeout handling lives in ONE place — the backend — and never
    # scaffolds back up into the agents/bindings. On ``is_error`` the message is in
    # ``result``.
    timed_out: bool = False


@dataclass
class AgentEvent:
    """One normalized event from a *streaming* agent session — the unit an ``on_event``
    sink receives (Observer pattern). The backend is the only layer that holds the live
    process stream, so it is the sole producer: it normalizes each transport line into
    one of these and ALSO emits synthetic ``heartbeat`` / ``timeout`` events (which have
    no transport line) so a consumer can see liveness and stalls. Emitting an event is
    observation only — it never changes the run's control flow (timeout/kill stays in
    the backend). ``kind`` is one of: ``system``, ``text``, ``tool_use``,
    ``tool_result``, ``rate_limit``, ``result``, ``heartbeat``, ``timeout``, ``error``."""

    kind: str
    elapsed: float = 0.0      # seconds since the session started
    tool: str = ""            # tool name (kind == "tool_use")
    text: str = ""            # short snippet: assistant text / result summary
    detail: str = ""          # extra: heartbeat gap, system subtype, error message
    raw_type: str = ""        # the underlying transport event type (debugging)


# An ``on_event`` sink: a callable handed one :class:`AgentEvent` at a time, or None
# (the default — no observer, so the backend takes its silent, non-streaming path).
EventSink = Optional[Callable[["AgentEvent"], None]]


def stream_debug_enabled() -> bool:
    """True when agent sessions should run in streaming/observable mode. Gated on
    ``DISPATCH_AGENT_DEBUG`` (truthy). Off by default → backends take the silent
    ``--output-format json`` path and behave exactly as before."""
    return os.environ.get("DISPATCH_AGENT_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _stream_heartbeat_seconds() -> int:
    """Seconds without a *meaningful* event (tool call / text / result) before the
    backend emits a synthetic ``heartbeat`` — the stall / throttle signal. Override
    with ``DISPATCH_AGENT_HEARTBEAT_SECONDS`` (default 30)."""
    try:
        return max(5, int(os.environ.get("DISPATCH_AGENT_HEARTBEAT_SECONDS", "30")))
    except ValueError:
        return 30


def _short_tool_input(inp: Any) -> str:
    """A compact one-line hint of a tool_use input (never the full payload — that can be
    a whole file's contents)."""
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "description", "query"):
        if inp.get(key):
            return f"{key}={str(inp[key])[:60]}"
    return ",".join(list(inp.keys())[:4])


def _normalize_stream_line(line: str, res: "AgentResult", state: Dict[str, Any]) -> Optional["AgentEvent"]:
    """Map one ``stream-json`` transport line to an :class:`AgentEvent`, folding the
    terminal ``result`` event's fields into ``res`` as a side effect. Returns None for
    pure transport noise (bare ``user`` / ``system`` lines) so the sink isn't spammed —
    liveness during such gaps is covered by the heartbeat instead. ``state`` carries the
    cross-line counters the heartbeat reads (``rate_limits``, ``tool_calls``,
    ``last_meaningful``, ``saw_result``)."""
    line = (line or "").strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return None
    t = ev.get("type")
    if t == "assistant":
        for blk in (ev.get("message", {}).get("content") or []):
            bt = blk.get("type")
            if bt == "tool_use":
                state["tool_calls"] = state.get("tool_calls", 0) + 1
                state["last_meaningful"] = time.monotonic()
                return AgentEvent(kind="tool_use", tool=str(blk.get("name") or "?"),
                                  text=_short_tool_input(blk.get("input")), raw_type=t)
            if bt == "text" and str(blk.get("text") or "").strip():
                state["last_meaningful"] = time.monotonic()
                return AgentEvent(kind="text", text=str(blk.get("text"))[:160], raw_type=t)
        return None
    if t in ("rate_limit_event", "rate_limit"):
        state["rate_limits"] = state.get("rate_limits", 0) + 1
        return AgentEvent(kind="rate_limit",
                          detail=str(ev.get("rate_limit") or ev.get("subtype") or "")[:80], raw_type=t)
    if t == "result":
        res.result = str(ev.get("result") or "")
        res.usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
        res.total_cost_usd = ev.get("total_cost_usd")
        res.is_error = bool(ev.get("is_error", False))
        res.num_turns = ev.get("num_turns")
        state["saw_result"] = True
        state["last_meaningful"] = time.monotonic()
        return AgentEvent(kind="result", text=res.result[:120],
                          detail=f"is_error={res.is_error} turns={res.num_turns}", raw_type=t)
    return None  # bare system / user transport lines — heartbeat covers liveness


@dataclass
class AgentRunSpec:
    """One agent session's parameters — everything a backend needs, nothing
    agent-specific. ``subagents`` is a plain ``{name: {description, prompt, tools}}``
    map (the SDK backend builds ``AgentDefinition`` objects from it lazily), so callers
    never import ``claude_agent_sdk`` to request multi-agent fan-out."""

    cwd: str
    prompt: str
    model: str
    timeout: int
    allowed_tools: List[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    permission_mode: str = "bypassPermissions"
    subagents: Optional[Dict[str, Dict[str, Any]]] = None
    # The agent archetype's persona, sent distinctly from the per-session ``prompt``
    # (CLI: ``--append-system-prompt``; SDK: ``ClaudeAgentOptions.system_prompt``;
    # chat: the ``system`` message). Empty -> no custom system prompt (the caller put
    # everything in ``prompt``, the pre-archetype behaviour).
    system_prompt: str = ""


def usage_tokens(usage: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    """(input, output) token counts from a usage block. Field names vary across SDK /
    CLI versions, so probe several (parity with the shell's defensive jq)."""
    if not isinstance(usage, dict):
        return 0, 0

    def _pick(*keys: str) -> int:
        for k in keys:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    tin = _pick("input_tokens", "inputTokens", "prompt_tokens")
    if tin == 0:
        tin = _pick("cache_read_input_tokens") + _pick("cache_creation_input_tokens")
    tout = _pick("output_tokens", "outputTokens", "completion_tokens")
    return tin, tout


def seed_config_auth(cfg_dir: str) -> None:
    """Seed an isolated ``CLAUDE_CONFIG_DIR`` with ONLY the subscription auth state —
    login/onboarding (``~/.claude.json``) + the OAuth credential — so the headless
    ``claude`` is logged in WITHOUT inheriting the operator's ``CLAUDE.md`` / memory /
    hooks (the isolation goal).

    Subscription auth is NOT keychain-transparent across config homes: once
    ``CLAUDE_CONFIG_DIR`` is non-default, the CLI reads its credential from the config
    dir, not the login keychain, so a *fresh* dir is "Not logged in · Please run
    /login" and every turn errors. The credential is therefore placed into the dir
    explicitly — from ``~/.claude/.credentials.json`` (Linux) or, on macOS, extracted
    from the login keychain (service ``Claude Code-credentials``). Best effort: on
    failure the run still proceeds and surfaces the auth error downstream rather than
    silently mutating."""
    import shutil as _shutil

    home = os.path.expanduser("~")
    src_json = os.path.join(home, ".claude.json")
    if os.path.isfile(src_json):
        try:
            _shutil.copyfile(src_json, os.path.join(cfg_dir, ".claude.json"))
        except OSError:
            pass
    dst_cred = os.path.join(cfg_dir, ".credentials.json")
    src_cred = os.path.join(home, ".claude", ".credentials.json")
    if os.path.isfile(src_cred):
        try:
            _shutil.copyfile(src_cred, dst_cred)
            return
        except OSError:
            pass
    if sys.platform == "darwin":  # credential lives in the login keychain, not a file
        try:
            cred = proc.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials",
                 "-a", os.environ.get("USER", ""), "-w"],
                capture=True, timeout=10,
            )
            if cred.ok and cred.stdout.strip():
                with open(dst_cred, "w", encoding="utf-8") as fh:
                    fh.write(cred.stdout)
        except Exception:  # noqa: BLE001 — best-effort; auth failure surfaces downstream
            pass


class AgentBackend(ABC):
    """Strategy interface: run one agent session and ALWAYS return an
    :class:`AgentResult` — never raise for a handled failure. A wall-clock timeout or a
    backend error is recorded as ``is_error=True`` (``timed_out=True`` for the former)
    with the message in ``result``, so timeout/error handling lives HERE, in the
    backend, and never scaffolds up into the agents or bindings (failures-as-data). Only
    genuinely unexpected exceptions (programmer error) propagate, to surface as bugs."""

    name: str = "agent"

    @abstractmethod
    def run(self, spec: AgentRunSpec, *, on_event: EventSink = None) -> AgentResult:  # pragma: no cover - interface
        """Run one session. When ``on_event`` is given, the backend SHOULD stream
        normalized :class:`AgentEvent`s to it (observation only — the sink never alters
        control flow); when None, the backend takes its silent, non-streaming path and
        behaves exactly as before. A sink raising is swallowed, never propagated."""
        ...

    @staticmethod
    def _timeout_result(seconds: int) -> AgentResult:
        return AgentResult(is_error=True, timed_out=True,
                           result=f"agent session timed out after {seconds}s")

    @staticmethod
    def _error_result(exc: Exception) -> AgentResult:
        return AgentResult(is_error=True, result=f"agent backend error: {exc}")


class CliBackend(AgentBackend):
    """Backend via the ``claude`` CLI headless on the subscription. Used because
    ``claude_agent_sdk`` 0.2.x hangs under this env (CLI 2.1.x / Python 3.14: anyio
    stream stalls after the system-init messages) while the CLI runs fine. The lead
    agent may still fan out to sub-agents via its own Task/Agent tool — under
    ``bypassPermissions`` it has the full tool set, so ``allowed_tools``/``subagents``
    on the spec are advisory here and consumed by the SDK backend instead."""

    name = "cli"

    def run(self, spec: AgentRunSpec, *, on_event: EventSink = None) -> AgentResult:
        import shutil as _shutil

        sub_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        # Context isolation: repoint the config home to a temp dir so the operator's
        # ~/.claude does NOT load into the session. Seeded with the subscription auth
        # state ONLY (a non-default home is "Not logged in" by default). The target
        # cwd's own CLAUDE.md still loads (cwd-based) — wanted.
        iso_cfg = tempfile.mkdtemp(prefix="agent-cfg-")
        seed_config_auth(iso_cfg)
        sub_env["CLAUDE_CONFIG_DIR"] = iso_cfg
        claude = os.environ.get("CLAUDE_BIN") or "claude"
        # Shared flags; the output-format differs per path (json vs stream-json).
        base_cmd = [
            claude, "-p", spec.prompt,
            "--permission-mode", spec.permission_mode,
            "--model", spec.model,
            "--add-dir", spec.cwd,
            # The seeded .claude.json carries the operator's user-scoped MCP
            # servers; booting them in a headless agent is unwanted surface and
            # a hang source (an MCP server that blocks on interactive input —
            # e.g. a password manager with no session token — stalls session
            # init until the wall-clock timeout). Isolation means NO inherited
            # MCP servers.
            "--strict-mcp-config",
        ]
        if spec.system_prompt:
            # Append (not replace) so the archetype persona layers onto the CLI's
            # default system prompt rather than discarding it.
            base_cmd += ["--append-system-prompt", spec.system_prompt]
        try:
            if on_event is not None:
                # Observable path: stream events to the sink (Observer). Used when a
                # consumer (the worker, under DISPATCH_AGENT_DEBUG) wants liveness.
                return self._run_streamed(base_cmd, spec, sub_env, on_event)
            # Default silent path — single JSON blob at the end, exactly as before.
            cmd = base_cmd + ["--output-format", "json"]
            try:
                cp = proc.run(cmd, cwd=spec.cwd, env=sub_env, capture=True, timeout=spec.timeout)
            except proc.ProcTimeout:
                return self._timeout_result(spec.timeout)
            except proc.ProcError as exc:  # launch failure (CLI missing, etc.) — handled as data
                return self._error_result(exc)
            return self._parse_json_result(cp)
        finally:
            _shutil.rmtree(iso_cfg, ignore_errors=True)

    @staticmethod
    def _parse_json_result(cp: "proc.Completed") -> AgentResult:
        """Build an :class:`AgentResult` from a completed ``--output-format json`` run."""
        res = AgentResult()
        res.is_error = not cp.ok
        out = (cp.stdout or "").strip()
        try:
            obj = json.loads(out)
            if isinstance(obj, dict):
                res.result = str(obj.get("result") or "")
                res.usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
                res.total_cost_usd = obj.get("total_cost_usd")
                res.is_error = bool(obj.get("is_error", res.is_error))
                res.num_turns = obj.get("num_turns")
        except (ValueError, TypeError):
            res.result = out  # non-JSON stdout: treat as the result text
        if res.is_error and not res.result:
            res.result = (cp.stderr or "").strip()[:500]
        return res

    def _run_streamed(self, base_cmd: List[str], spec: AgentRunSpec, sub_env: Dict[str, str],
                      on_event: Callable[["AgentEvent"], None]) -> AgentResult:
        """Stream-json path: relay normalized events to ``on_event`` as the session runs,
        emit synthetic ``heartbeat`` events when meaningful progress stalls (the throttle
        signal), enforce the wall-clock cap with a watchdog, and reconstruct the
        :class:`AgentResult` from the terminal ``result`` event. Same return contract as
        the silent path; visibility is the only difference."""
        cmd = base_cmd + ["--output-format", "stream-json", "--verbose"]
        res = AgentResult()
        start = time.monotonic()
        state: Dict[str, Any] = {"last_meaningful": start, "saw_result": False,
                                 "timed_out": False, "rate_limits": 0, "tool_calls": 0}
        hb = _stream_heartbeat_seconds()

        def _emit(ev: AgentEvent) -> None:
            ev.elapsed = time.monotonic() - start
            try:
                on_event(ev)
            except Exception:  # noqa: BLE001 — a sink must never break the run (observation only)
                pass

        try:
            # Merge stderr into stdout so the single stream read drains EVERYTHING —
            # a separate undrained stderr pipe could fill and deadlock the child. Non-JSON
            # stderr lines simply normalize to None and are skipped.
            with proc.popen(cmd, cwd=spec.cwd, env=sub_env, stderr=subprocess.STDOUT,
                            start_new_session=True) as handle:
                def _kill() -> None:
                    state["timed_out"] = True
                    try:  # whole process group — the agent may have spawned children
                        os.killpg(os.getpgid(handle.pid), signal.SIGTERM)
                    except Exception:  # noqa: BLE001
                        try:
                            handle.send_signal(signal.SIGTERM)
                        except Exception:  # noqa: BLE001
                            pass

                watchdog = threading.Timer(spec.timeout, _kill)
                watchdog.daemon = True
                watchdog.start()

                stop = threading.Event()

                def _heartbeat() -> None:
                    # Fire on lack of *meaningful* progress (tool/text/result), not on raw
                    # transport silence — so a storm of rate-limit/system lines still reads
                    # as "stalled", which is exactly the throttle case we want surfaced.
                    while not stop.wait(min(hb, 5)):
                        gap = time.monotonic() - state["last_meaningful"]
                        if gap >= hb:
                            _emit(AgentEvent(
                                kind="heartbeat",
                                detail=(f"no progress for {gap:.0f}s "
                                        f"({state['tool_calls']} tool calls, "
                                        f"{state['rate_limits']} rate-limit waits so far)")))
                            state["last_meaningful"] = time.monotonic()  # throttle the heartbeat itself

                hbt = threading.Thread(target=_heartbeat, daemon=True)
                hbt.start()
                try:
                    for line in handle.stream():
                        ev = _normalize_stream_line(line, res, state)
                        if ev is not None:
                            _emit(ev)
                finally:
                    stop.set()
                    watchdog.cancel()
        except proc.ProcError as exc:  # launch failure — handled as data
            return self._error_result(exc)

        if state["timed_out"] and not state["saw_result"]:
            _emit(AgentEvent(kind="timeout",
                             detail=f"killed at {spec.timeout}s wall-clock "
                                    f"({state['tool_calls']} tool calls, "
                                    f"{state['rate_limits']} rate-limit waits)"))
            return self._timeout_result(spec.timeout)
        return res


class SdkBackend(AgentBackend):
    """Backend via ``claude_agent_sdk.query`` over asyncio. Subscription auth:
    ``options.env`` is a copy of ``os.environ`` with ``ANTHROPIC_API_KEY`` removed so
    the spawned CLI uses the logged-in subscription, not API billing. ``subagents``
    become ``AgentDefinition`` objects (multi-agent fan-out)."""

    name = "sdk"

    def run(self, spec: AgentRunSpec, *, on_event: EventSink = None) -> AgentResult:
        # Streaming observability is implemented for the CLI backend only; the SDK path
        # accepts ``on_event`` for interface parity and ignores it (it is opt-in and
        # known to stall under the current env — see the class docstring).
        import asyncio

        from claude_agent_sdk import (  # imported lazily so OFFLINE never needs it
            AgentDefinition,
            ClaudeAgentOptions,
            ResultMessage,
            query,
        )

        sub_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        opts_kwargs: Dict[str, Any] = dict(
            cwd=spec.cwd,
            add_dirs=[spec.cwd],
            permission_mode=spec.permission_mode,  # disposable target; §skip-perms posture
            model=spec.model,
            allowed_tools=list(spec.allowed_tools),
            env=sub_env,
        )
        if spec.system_prompt:
            opts_kwargs["system_prompt"] = spec.system_prompt
        if spec.subagents:
            opts_kwargs["agents"] = {
                name: AgentDefinition(**cfg) for name, cfg in spec.subagents.items()
            }
        options = ClaudeAgentOptions(**opts_kwargs)

        async def _drive():
            result = None
            async for message in query(prompt=spec.prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
            return result

        try:
            rm = asyncio.run(asyncio.wait_for(_drive(), timeout=spec.timeout))
        except asyncio.TimeoutError:
            return self._timeout_result(spec.timeout)
        except Exception as exc:  # noqa: BLE001 — SDK/transport errors handled as data
            return self._error_result(exc)
        if rm is None:
            return AgentResult()
        usage = getattr(rm, "usage", None)
        return AgentResult(
            result=str(getattr(rm, "result", None) or "").strip(),
            usage=usage if isinstance(usage, dict) else {},
            total_cost_usd=getattr(rm, "total_cost_usd", None),
            is_error=bool(getattr(rm, "is_error", False)),
            num_turns=getattr(rm, "num_turns", None),
        )


class ChatBackend(AgentBackend):
    """Backend via ``foundation.models.chat`` — a SINGLE-SHOT, non-tool-using
    completion. This is how a backend-agnostic caller reaches a plain chat model
    (OpenRouter / HuggingFace / litellm / the Anthropic API), selected by
    ``foundation.models`` from the resolved model id / ``MODELS_BACKEND``. There is no
    agent session and no tool use, so ``allowed_tools``/``subagents``/``cwd`` are
    ignored; it suits single-shot callers whose whole job is to emit one structured
    (usually JSON) answer. The caller's ``system_prompt`` on the spec becomes the
    ``system`` message; ``prompt`` the ``user`` message. A transport error is recorded
    as ``is_error`` (failures-as-data), like the other backends — never raised."""

    name = "chat"

    def run(self, spec: AgentRunSpec, *, on_event: EventSink = None) -> AgentResult:
        # Single-shot, no tool loop and no stream — ``on_event`` is accepted for
        # interface parity and ignored (there is nothing incremental to relay).
        from foundation import models  # local import: keep stdlib-only at module import

        messages: List[Dict[str, str]] = []
        if spec.system_prompt:
            messages.append({"role": "system", "content": spec.system_prompt})
        messages.append({"role": "user", "content": spec.prompt})
        try:
            text = models.chat(spec.model, messages)
        except Exception as exc:  # noqa: BLE001 — transport/model errors handled as data
            return self._error_result(exc)
        return AgentResult(result=str(text or "").strip())


# The factory registry: backend name -> backend class. Extend by registering a new
# AgentBackend subclass here; every agent reaches it through make_backend().
_BACKENDS: Dict[str, type] = {
    CliBackend.name: CliBackend,
    SdkBackend.name: SdkBackend,
    ChatBackend.name: ChatBackend,
}


def make_backend(backend: Optional[str] = None) -> AgentBackend:
    """Factory: return the :class:`AgentBackend` for ``backend`` (``"cli"`` |
    ``"sdk"``). Resolution: explicit arg -> ``$AGENT_BACKEND`` -> ``"cli"`` (the
    default, since the SDK hangs under the current env). Unknown names fall back to
    the CLI backend."""
    name = (backend or os.environ.get("AGENT_BACKEND") or "cli").strip().lower()
    return _BACKENDS.get(name, CliBackend)()
