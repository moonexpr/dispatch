#!/usr/bin/env python3
"""foundation.agents.base_agent — the abstract agent archetype (abstraction side of a Bridge).

This is the *foundation* layer: an abstract base that any concrete role definition
subclasses. An *archetype* is a reusable, backend-agnostic **agent definition** — a
durable role identity (its ``SYSTEM_PROMPT`` persona), its **tool access**, and its
default model route — everything about *who the agent is*, decoupled from *how a
session is executed*. You **puppet** an archetype by handing it a per-session
``session_prompt`` (the task for this run); the same profile drives many sessions. The
execution backend — the ``claude`` CLI, the Agent SDK, or a single-shot chat model
(OpenRouter / HuggingFace / litellm) — is the *implementation* side, reached through
:func:`foundation.agent_sdk.make_backend`. Swap the backend without touching the
definition.

Concrete archetypes are supplied by higher layers, subclassing this base.
Leaf-module discipline: stdlib + sibling ``foundation`` modules only; never import
from callers.
"""
from __future__ import annotations

import os
from abc import ABC
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from foundation import agent_sdk, env as _env, models, runtime as _runtime

# Cached multi-source environment (process env → .env → pipeline.env → Keychain). The
# process source reads ``os.environ`` live, so a single cached instance still sees
# per-tick overrides; the dotenv files are read once. Agent backend/timeout overrides
# resolve through this, NOT raw ``os.environ.get``, so pipeline.env / .env are honored.
_ENVIRONMENT: Optional["_env.Environment"] = None


def _environment() -> "_env.Environment":
    global _ENVIRONMENT
    if _ENVIRONMENT is None:
        _ENVIRONMENT = _env.Environment.default()
    return _ENVIRONMENT


@dataclass
class AgentOutcome:
    """The result of :meth:`BaseAgent.invoke` — a uniform, NEVER-raising outcome shape
    so a caller handles a misrun with a branch, not a ``try/except``. ``is_error`` is
    true for a handled timeout or backend error (``timed_out`` distinguishes the
    former) OR when the backend reported ``is_error``; ``error`` carries the message.
    On success ``result_text`` is the agent's output and the token counts are filled.
    The agent has already logged the run line and any cost/turns — the caller need not."""

    ok: bool
    is_error: bool
    timed_out: bool
    error: str
    result_text: str
    tokens_in: int
    tokens_out: int
    model: str
    total_cost_usd: Optional[float] = None
    num_turns: Optional[int] = None


class BaseAgent(ABC):
    """Abstract base archetype. A concrete agent is defined purely by overriding the
    class-level identity below (persona, route, tools, backend, timeouts, log tag) — no
    method override is required. Instances may override any knob at construction, and a
    single :meth:`run` / :meth:`invoke` call may override per-session.

    Resolution order for each knob: explicit call arg -> instance value (constructor)
    -> environment override -> class default.
    """

    #: The role persona — written ONCE on the archetype, sent as the session's system
    #: prompt distinctly from the per-run task. Subclasses MUST set this.
    SYSTEM_PROMPT: str = ""
    #: Generation route (gen-* tier), resolved to a concrete model id via
    #: ``foundation.models.model_id_for_route``. A call may pass an explicit ``model`` or a
    #: different ``route`` to override.
    DEFAULT_ROUTE: str = "gen-default"
    #: The archetype's tool access — what the agent is permitted to use.
    DEFAULT_TOOLS: Tuple[str, ...] = ("Read",)
    #: Backend resolution: explicit -> instance -> $BACKEND_ENV -> DEFAULT_BACKEND.
    DEFAULT_BACKEND: str = "cli"
    BACKEND_ENV: Optional[str] = None
    #: Session wall-clock cap (seconds); a $TIMEOUT_ENV override is honored if set.
    DEFAULT_TIMEOUT: int = 600
    TIMEOUT_ENV: Optional[str] = None
    DEFAULT_PERMISSION: str = "bypassPermissions"
    #: stderr log prefix for this archetype's runs (stdout is reserved for results).
    LOG_TAG: str = "agent(base_agent)"

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        route: Optional[str] = None,
        backend: Optional[str] = None,
        tools: Optional[Tuple[str, ...]] = None,
        timeout: Optional[int] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        self.model = model
        self.route = route
        self.backend = backend
        self.tools = tools
        self.timeout = timeout
        self.system_prompt = system_prompt if system_prompt is not None else self.SYSTEM_PROMPT

    # -- resolution helpers -------------------------------------------------
    def resolve_model(self, *, model: Optional[str] = None, route: Optional[str] = None) -> str:
        """A concrete model id. An explicit ``model`` wins; else a ``route`` (per-call
        or instance or class default) is resolved through the single source of truth
        (``foundation.models``). Never returns empty — falls back to the sonnet tier."""
        if model:
            return model
        if self.model:
            return self.model
        r = route or self.route or self.DEFAULT_ROUTE
        try:
            return models.model_id_for_route(r) or "claude-sonnet-4-6"
        except Exception:  # noqa: BLE001 — resolution is best-effort; never run model-less
            return "claude-sonnet-4-6"

    def resolve_backend(self, backend: Optional[str] = None) -> str:
        if backend:
            return backend.strip().lower()
        if self.backend:
            return self.backend.strip().lower()
        if self.BACKEND_ENV:
            env_val = _environment().get(self.BACKEND_ENV)
            if env_val:
                return env_val.strip().lower()
        return self.DEFAULT_BACKEND.strip().lower()

    def resolve_timeout(self, timeout: Optional[int] = None) -> int:
        if timeout is not None:
            return int(timeout)
        if self.timeout is not None:
            return int(self.timeout)
        if self.TIMEOUT_ENV:
            env_val = _environment().get(self.TIMEOUT_ENV)
            if env_val:
                try:
                    return int(env_val)
                except ValueError:
                    pass
        return int(self.DEFAULT_TIMEOUT)

    def resolve_tools(self, tools: Optional[Tuple[str, ...]] = None) -> List[str]:
        # TODO(John): Investigate if we can deterministically attach tools for the
        # agents to use here — today tool access is an advisory allowlist (the CLI
        # backend under bypassPermissions still grants the full set); a deterministic
        # binding would let an archetype guarantee exactly its declared tool surface
        # regardless of backend.
        return list(tools or self.tools or self.DEFAULT_TOOLS)

    # -- the low-level verb: build a spec and run on the resolved backend ----
    def run(
        self,
        session_prompt: str,
        *,
        cwd: Optional[str] = None,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        route: Optional[str] = None,
        tools: Optional[Tuple[str, ...]] = None,
        timeout: Optional[int] = None,
        permission_mode: Optional[str] = None,
        subagents: Optional[Dict[str, Dict[str, Any]]] = None,
        on_event: "agent_sdk.EventSink" = None,
    ) -> agent_sdk.AgentResult:
        """Run one session: this archetype's persona (``SYSTEM_PROMPT``) + the caller's
        ``session_prompt`` (the task), on the resolved backend. Returns the uniform
        :class:`foundation.agent_sdk.AgentResult`. The backend records a handled timeout /
        error as ``is_error`` rather than raising — see :meth:`invoke` for the
        higher-level, token-extracted, self-logging form a binding should prefer.
        ``on_event`` (when given) is forwarded to the backend as a streaming
        observability sink; the archetype neither produces nor consumes events."""
        spec = agent_sdk.AgentRunSpec(
            cwd=cwd or os.getcwd(),
            prompt=session_prompt,
            model=self.resolve_model(model=model, route=route),
            timeout=self.resolve_timeout(timeout),
            allowed_tools=self.resolve_tools(tools),
            permission_mode=permission_mode or self.DEFAULT_PERMISSION,
            subagents=subagents,
            system_prompt=self.system_prompt,
        )
        return agent_sdk.make_backend(self.resolve_backend(backend)).run(spec, on_event=on_event)

    # -- logging ------------------------------------------------------------
    def _log(self, msg: str) -> None:
        """Subsystem-tagged stderr line via the standard runtime logger (atomic flush;
        stdout is reserved for the result)."""
        _runtime.Logger(self.LOG_TAG).log(msg)

    # -- the high-level verb a binding should use ---------------------------
    def invoke(
        self,
        session_prompt: str,
        *,
        cwd: Optional[str] = None,
        backend: Optional[str] = None,
        model: Optional[str] = None,
        route: Optional[str] = None,
        tools: Optional[Tuple[str, ...]] = None,
        timeout: Optional[int] = None,
        permission_mode: Optional[str] = None,
        subagents: Optional[Dict[str, Dict[str, Any]]] = None,
        on_event: "agent_sdk.EventSink" = None,
    ) -> AgentOutcome:
        """Run a session and return a uniform, NEVER-raising :class:`AgentOutcome`. This
        is the higher abstraction the bindings target: it resolves the backend/model/
        timeout, logs the run line and any cost/turns, extracts token counts, and folds
        a handled timeout/backend error into ``is_error`` — so a binding only finds the
        agent, hands it the prompt, and reads the output or branches on a misrun. No run
        machinery, no timeout scaffolding, no logging in the binding. ``on_event`` (when
        given) is forwarded to the backend as a streaming observability sink — the worker
        injects one under ``DISPATCH_AGENT_DEBUG``."""
        b = self.resolve_backend(backend)
        m = self.resolve_model(model=model, route=route)
        t = self.resolve_timeout(timeout)
        self._log(f"running backend={b} model={m} timeout={t}s (subscription auth)")
        result = self.run(
            session_prompt, cwd=cwd, backend=b, model=m, route=route, tools=tools,
            timeout=t, permission_mode=permission_mode, subagents=subagents, on_event=on_event,
        )
        tokens_in, tokens_out = agent_sdk.usage_tokens(result.usage)
        if result.total_cost_usd is not None:
            self._log(f"total_cost_usd={result.total_cost_usd} num_turns={result.num_turns}")
        if result.is_error:
            self._log("session timed out" if result.timed_out
                      else f"backend error: {(result.result or '')[:200]}")
        return AgentOutcome(
            ok=not result.is_error,
            is_error=result.is_error,
            timed_out=result.timed_out,
            error=result.result if result.is_error else "",
            result_text="" if result.is_error else result.result,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model=m,
            total_cost_usd=result.total_cost_usd,
            num_turns=result.num_turns,
        )
