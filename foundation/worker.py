#!/usr/bin/env python3
"""foundation.worker — Workers: executors that SEAT a unit of work (an agent or a proc).

A **Worker** is the executor that decouples *running* from *what is run*. A concrete
worker seats one kind of work:

  * :class:`AgentWorker` seats a :class:`foundation.agents.BaseAgent` — an LLM agent
    session (which itself owns backend selection, logging, token extraction, and
    timeout/error handling).
  * :class:`ProcWorker` seats a **proc** — a subprocess (argv).

Because both go through the same Worker contract, we get **timeout control for both
procs and agents for free**, and both fold a timeout / error into a uniform,
never-raising outcome (failures-as-data) — the agent into an
:class:`foundation.agents.AgentOutcome`, the proc into a :class:`ProcOutcome`. Binding
code hands a worker the work it is binding and the domain text, and the worker owns the
run lifecycle, the shelf record (lifetime updates), the audit
:class:`foundation.actions.Output` (event propagation), and the timeout. Concrete
workers are created through the :func:`make_worker` factory.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence, Type, Union

from foundation import agent_sdk
from foundation import proc as _proc
from foundation.actions import Output
from foundation.agents import AgentOutcome, BaseAgent


@dataclass
class ProcOutcome:
    """A proc's uniform, never-raising outcome — the subprocess analogue of
    :class:`foundation.agents.AgentOutcome`. ``is_error`` covers a non-zero exit, a
    timeout (``timed_out=True``), or a spawn failure; ``error`` carries the message."""

    ok: bool
    is_error: bool
    timed_out: bool
    error: str
    returncode: int
    stdout: str
    stderr: str


# A summary source is either a literal string or a callable of the agent outcome.
SummarySource = Union[str, Callable[[AgentOutcome], str]]


def _resolve(src: Optional[SummarySource], outcome: AgentOutcome, default: str) -> str:
    if callable(src):
        return src(outcome)
    if isinstance(src, str) and src:
        return src
    return default


class BaseWorker(ABC):
    """Abstract executor (the worker interface). A concrete worker seats one kind of
    work and runs it with uniform timeout control and failures-as-data. Created via
    :func:`make_worker`."""

    kind: str = "worker"

    @abstractmethod
    def run(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - interface
        """Run the seated work and return its never-raising outcome."""
        ...


class AgentWorker(BaseWorker):
    """Seats a :class:`foundation.agents.BaseAgent`. :meth:`run` / :meth:`invoke` return
    the raw :class:`AgentOutcome` (for fail-safe producers that own their deliverable
    I/O); :meth:`execute` runs the full inference pattern — write the run record onto
    the shelf and return the audit :class:`Output` — so the binding writes no shelf
    state itself."""

    kind = "agent"

    def __init__(self, agent: BaseAgent) -> None:
        self.agent = agent

    def _stream_observer(self) -> Optional[Any]:
        """An env-gated streaming sink (the Observer's consumer side): logs the agent's
        LIVE progress — tool calls, rate-limit waits, stall heartbeats, the final result
        — through the agent's own tagged logger. Returns None when ``DISPATCH_AGENT_DEBUG``
        is off, so the backend takes its silent path and behaviour is unchanged.

        The consumption *policy* lives HERE, at the worker, so it applies uniformly to
        every archetype (architect, adversary, engineer) without any of them — or the
        backend — knowing about it. The backend is the sole *producer*; this only
        formats and logs what it receives, so it can never alter the run."""
        if not agent_sdk.stream_debug_enabled():
            return None
        log = self.agent._log
        glyph = {"tool_use": "▸", "text": "·", "rate_limit": "⏳",
                 "heartbeat": "…", "result": "✓", "timeout": "✗", "error": "✗"}

        def _sink(ev: "agent_sdk.AgentEvent") -> None:
            mark = glyph.get(ev.kind, "·")
            if ev.kind == "tool_use":
                body = f"tool {ev.tool}" + (f" ({ev.text})" if ev.text else "")
            elif ev.kind in ("text", "result", "rate_limit", "heartbeat", "timeout", "error"):
                body = " ".join(p for p in (ev.text, ev.detail) if p) or ev.kind
            else:
                body = ev.detail or ev.kind
            log(f"  {mark} [{ev.elapsed:.0f}s] {body}")

        return _sink

    def run(self, prompt: str, **run_kwargs: Any) -> AgentOutcome:
        run_kwargs.setdefault("on_event", self._stream_observer())
        return self.agent.invoke(prompt, **run_kwargs)

    # readable alias for the fail-safe producer call site
    invoke = run

    def execute(
        self,
        ctx: Any,
        *,
        prompt: str,
        summarize: Optional[SummarySource] = None,
        on_error: Optional[SummarySource] = None,
        shelf: Any = None,
        **run_kwargs: Any,
    ) -> Output:
        """Run the agent and record the outcome: on error, write ``{is_error, summary}``
        and return a terminal-meta Output; on success, write the run record
        (``tokens_in/out``, ``result_text``, ``summary``) and return the audit Output.
        ``summarize`` / ``on_error`` supply the binding's domain text (a string or a
        callable of the outcome); ``shelf`` defaults to ``ctx.shelves.shared``."""
        run_kwargs.setdefault("on_event", self._stream_observer())
        outcome = self.agent.invoke(prompt, **run_kwargs)
        sh = shelf if shelf is not None else ctx.shelves.shared
        if outcome.is_error:
            sh.update({
                "is_error": True,
                "result_text": "",
                "summary": _resolve(on_error, outcome, outcome.error or "agent run failed"),
            })
            return Output("", meta={"is_error": True})
        text = outcome.result_text
        sh.update({
            "tokens_in": outcome.tokens_in,
            "tokens_out": outcome.tokens_out,
            "result_text": text,
            "is_error": False,
            "summary": _resolve(summarize, outcome, text),
        })
        return Output(text, meta={
            "usage": {"input_tokens": outcome.tokens_in, "output_tokens": outcome.tokens_out},
            "model": outcome.model,
            "is_error": False,
        })


class ProcWorker(BaseWorker):
    """Seats a **proc** (subprocess). Carries the cwd/env/timeout the seated procs run
    under; :meth:`run` executes one argv with that timeout and folds a timeout, spawn
    failure, or non-zero exit into a never-raising :class:`ProcOutcome`. This is the
    same timeout discipline :class:`AgentWorker` gives agents — for free, for procs."""

    kind = "proc"

    def __init__(
        self,
        *,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> None:
        self.cwd = cwd
        self.env = env
        self.timeout = timeout

    def run(
        self,
        cmd: Sequence[str],
        *,
        input: Optional[str] = None,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
    ) -> ProcOutcome:
        """Run one argv through :func:`foundation.proc.run` with this worker's
        cwd/env/timeout, folding a timeout, launch failure, or non-zero exit into
        a never-raising :class:`ProcOutcome`."""
        t = timeout if timeout is not None else self.timeout
        try:
            p = _proc.run(list(cmd), cwd=cwd or self.cwd, env=self.env, input=input,
                          capture=True, text=True, timeout=t)
        except _proc.ProcTimeout as exc:  # wall-clock cap — handled as data
            return ProcOutcome(ok=False, is_error=True, timed_out=True, error=str(exc),
                               returncode=124, stdout="", stderr=exc.stderr)
        except _proc.ProcError as exc:  # launch failure / non-zero (check) — handled as data
            return ProcOutcome(ok=False, is_error=True, timed_out=False, error=str(exc),
                               returncode=exc.returncode or 127, stdout="", stderr=exc.stderr)
        ok = p.returncode == 0
        return ProcOutcome(
            ok=ok, is_error=not ok, timed_out=False,
            error="" if ok else (p.stderr or "").strip()[:500],
            returncode=p.returncode, stdout=p.stdout or "", stderr=p.stderr or "",
        )


# The factory registry: worker kind -> concrete Worker class. Mirrors
# ``foundation.agent_sdk._BACKENDS`` (backends) and ``agents._ARCHETYPES`` (agents).
_WORKERS: Dict[str, Type[BaseWorker]] = {
    AgentWorker.kind: AgentWorker,
    ProcWorker.kind: ProcWorker,
}


def make_worker(kind: str, **kwargs: Any) -> BaseWorker:
    """Factory: drive concrete worker creation by ``kind`` — ``"agent"`` ->
    :class:`AgentWorker` (pass ``agent=``); ``"proc"`` -> :class:`ProcWorker` (pass
    ``cwd=`` / ``env=`` / ``timeout=``). Unknown kinds raise ``KeyError`` — a worker
    kind must be registered, never guessed."""
    return _WORKERS[kind.strip().lower()](**kwargs)
