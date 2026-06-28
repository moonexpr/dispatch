#!/usr/bin/env python3
"""agent_sdk.py — a generic, agent-agnostic Claude Agent runner.

This is the *agentic, tool-using* layer, distinct from ``engine.models`` (the
request/response LLM API abstraction). Where ``models.chat`` sends one prompt and
reads one completion, an :class:`AgentRunner` drives a **headless agent session** —
the ``claude`` CLI or ``claude_agent_sdk`` — inside a working directory, with a tool
set, subscription-auth isolation, and optional sub-agents (multi-agent fan-out).

The runner is agent-agnostic: the engineer, the architect, or any future agent
binds a runner via the :func:`make_runner` **factory** and feeds it an
:class:`AgentRunSpec`. The agent-specific contract — what prompt to send, how to
read the result (an engineer Invoice, an architect work plan, …), and the git/gh
lifecycle around the run — stays in the caller, not here.

Subscription auth: every backend copies ``os.environ`` with ``ANTHROPIC_API_KEY``
removed, so the spawned ``claude`` uses the logged-in subscription rather than API
billing, and (CLI path) repoints ``CLAUDE_CONFIG_DIR`` to a throwaway dir seeded
with ONLY the login + credential (see :func:`seed_config_auth`) so the operator's
``~/.claude`` (CLAUDE.md / memory / hooks) never poisons the session (issue #159).

Leaf-module discipline: stdlib only at import time; ``claude_agent_sdk`` is imported
lazily inside the SDK runner so the OFFLINE / CLI path never needs it installed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# TODO(John): Refine the agent_sdk and runners with a better abstraction later to
# make this machinery less cumbersome. Today each caller (EngineerFactory's
# _engineer_runner, ArchitectFactory's _architect_runner) hand-rolls the same
# shape — read structured inputs off the shelf, build a prompt, make_runner().run(),
# parse the result, write outputs, fail-safe — around this runner. A cleaner
# abstraction (e.g. a declarative "agent action" that owns prompt-build + parse +
# shelf I/O, with the factory only injecting the backend) would let a new agent be
# added with a prompt + a parser instead of a bespoke factory + runner pair.

# The default agent tool set (the SDK path passes this explicitly; the CLI path
# runs under --permission-mode bypassPermissions and lets the lead use every tool).
DEFAULT_TOOLS: List[str] = ["Read", "Edit", "Write", "Bash", "Agent", "Task"]


@dataclass
class AgentResult:
    """A backend-uniform result shim so callers read one shape regardless of which
    runner produced it (mirrors the SDK ``ResultMessage`` surface)."""

    result: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    total_cost_usd: Optional[float] = None
    is_error: bool = False
    num_turns: Optional[int] = None


@dataclass
class AgentRunSpec:
    """One agent session's parameters — everything a runner needs, nothing
    agent-specific. ``subagents`` is a plain ``{name: {description, prompt, tools}}``
    map (the SDK runner builds ``AgentDefinition`` objects from it lazily), so callers
    never import ``claude_agent_sdk`` to request multi-agent fan-out."""

    cwd: str
    prompt: str
    model: str
    timeout: int
    allowed_tools: List[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    permission_mode: str = "bypassPermissions"
    subagents: Optional[Dict[str, Dict[str, Any]]] = None


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
    hooks (the issue-#159 isolation goal).

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
            cred = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials",
                 "-a", os.environ.get("USER", ""), "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if cred.returncode == 0 and cred.stdout.strip():
                with open(dst_cred, "w", encoding="utf-8") as fh:
                    fh.write(cred.stdout)
        except Exception:  # noqa: BLE001 — best-effort; auth failure surfaces downstream
            pass


class AgentRunner(ABC):
    """Strategy interface: run one agent session and return an :class:`AgentResult`.
    Timeouts/errors propagate as exceptions (``subprocess.TimeoutExpired`` for the CLI
    backend, ``asyncio.TimeoutError`` for the SDK backend) so the caller decides how to
    record them — the runner never invents a verdict."""

    name: str = "agent"

    @abstractmethod
    def run(self, spec: AgentRunSpec) -> AgentResult:  # pragma: no cover - interface
        ...


class CliAgentRunner(AgentRunner):
    """Backend via the ``claude`` CLI headless on the subscription. Used because
    ``claude_agent_sdk`` 0.2.x hangs under this env (CLI 2.1.x / Python 3.14: anyio
    stream stalls after the system-init messages) while the CLI runs fine. The lead
    agent may still fan out to sub-agents via its own Task/Agent tool — under
    ``bypassPermissions`` it has the full tool set, so ``allowed_tools``/``subagents``
    on the spec are advisory here and consumed by the SDK backend instead."""

    name = "cli"

    def run(self, spec: AgentRunSpec) -> AgentResult:
        import shutil as _shutil

        sub_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        # Context isolation (issue #159): repoint the config home to a temp dir so the
        # operator's ~/.claude does NOT load into the session. Seeded with the
        # subscription auth state ONLY (a non-default home is "Not logged in" by
        # default). The target cwd's own CLAUDE.md still loads (cwd-based) — wanted.
        iso_cfg = tempfile.mkdtemp(prefix="agent-cfg-")
        seed_config_auth(iso_cfg)
        sub_env["CLAUDE_CONFIG_DIR"] = iso_cfg
        claude = os.environ.get("CLAUDE_BIN") or "claude"
        cmd = [
            claude, "-p", spec.prompt,
            "--output-format", "json",
            "--permission-mode", spec.permission_mode,
            "--model", spec.model,
            "--add-dir", spec.cwd,
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=spec.cwd, env=sub_env, capture_output=True, text=True,
                timeout=spec.timeout,
            )
        finally:
            _shutil.rmtree(iso_cfg, ignore_errors=True)
        res = AgentResult()
        res.is_error = proc.returncode != 0
        out = (proc.stdout or "").strip()
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
            res.result = (proc.stderr or "").strip()[:500]
        return res


class SdkAgentRunner(AgentRunner):
    """Backend via ``claude_agent_sdk.query`` over asyncio. Subscription auth:
    ``options.env`` is a copy of ``os.environ`` with ``ANTHROPIC_API_KEY`` removed so
    the spawned CLI uses the logged-in subscription, not API billing. ``subagents``
    become ``AgentDefinition`` objects (multi-agent fan-out)."""

    name = "sdk"

    def run(self, spec: AgentRunSpec) -> AgentResult:
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

        rm = asyncio.run(asyncio.wait_for(_drive(), timeout=spec.timeout))
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


# The factory registry: backend name -> runner class. Extend by registering a new
# AgentRunner subclass here; every agent reaches it through make_runner().
_RUNNERS: Dict[str, type] = {
    CliAgentRunner.name: CliAgentRunner,
    SdkAgentRunner.name: SdkAgentRunner,
}


def make_runner(backend: Optional[str] = None) -> AgentRunner:
    """Factory: return the :class:`AgentRunner` for ``backend`` (``"cli"`` |
    ``"sdk"``). Resolution: explicit arg -> ``$AGENT_BACKEND`` -> ``"cli"`` (the
    default, since the SDK hangs under the current env). Unknown names fall back to
    the CLI runner."""
    name = (backend or os.environ.get("AGENT_BACKEND") or "cli").strip().lower()
    return _RUNNERS.get(name, CliAgentRunner)()
