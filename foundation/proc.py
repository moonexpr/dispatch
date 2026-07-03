"""proc.py — subprocess invocation & resource management.

Centralises *how* callers shell out to a child process so consumer modules
describe *what* to run, not the capture / decode / timeout / cleanup plumbing.
Every module that spawns a process may go through here rather than touching
:mod:`subprocess` directly.

The surface has two halves:

* **One-shot runs** — :func:`run` (and the convenience skins :func:`run_text`,
  :func:`run_json`, :func:`run_lines`, :func:`run_checked`) execute a command to
  completion and hand back a :class:`Completed` (a ``subprocess.CompletedProcess``
  enriched with ``.ok`` / ``.check()`` / ``.out``). Launch failures and timeouts
  are normalised into :class:`ProcError` / :class:`ProcTimeout` instead of leaking
  raw ``OSError`` / ``subprocess.TimeoutExpired``.

* **Managed long-running processes** — :func:`popen` (a context manager that
  *guarantees* the child is reaped — terminate→kill on the way out) and
  :func:`stream` (iterate a child's output line-by-line, with the same cleanup
  guarantee). These are the "resource management" half: a process opened here is
  never leaked even if the caller raises mid-stream.

Design: this is a **leaf** module — it imports only the standard library, never
any other ``foundation.*`` module. That keeps it free of import cycles (any
higher-level module may import *it*) and side-effect-free to import. Failure
policy is injected by the caller via ``on_fail`` rather than hard-wired here.

Callers always pass a fully-formed argv list (never a shell string), so nothing
here interprets untrusted text — it only executes commands the caller assembled.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from contextlib import contextmanager
from typing import Iterator, List, Optional

__all__ = [
    "ProcError",
    "ProcTimeout",
    "Completed",
    "Proc",
    "run",
    "run_text",
    "run_json",
    "run_lines",
    "run_checked",
    "popen",
    "stream",
    "terminate",
    "which",
    "DEFAULT_KILL_TIMEOUT",
]

# Grace period (seconds) given to a child between ``terminate()`` and the harder
# ``kill()`` when a managed process is being torn down.
DEFAULT_KILL_TIMEOUT = 5.0


class ProcError(RuntimeError):
    """A child process could not be launched, timed out, or exited non-zero.

    Carries the ``cmd``, ``returncode`` and captured ``stderr`` so callers that
    want richer handling than the helpers provide can inspect them.
    """

    def __init__(self, message, *, cmd=None, returncode=None, stderr="", timed_out=False):
        super().__init__(message)
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr or ""
        self.timed_out = timed_out


class ProcTimeout(ProcError):
    """A child process exceeded its ``timeout``.

    A :class:`ProcError` subclass so existing ``except ProcError`` handlers still
    catch it, while callers that distinguish a timeout from a launch failure or a
    non-zero exit (e.g. the engineer step, which records a timeout differently)
    can catch :class:`ProcTimeout` specifically. ``timeout`` is the limit that was
    exceeded, in seconds.
    """

    def __init__(self, message, *, cmd=None, timeout=None):
        super().__init__(message, cmd=cmd)
        self.timeout = timeout


class Completed(subprocess.CompletedProcess):
    """A finished process: ``subprocess.CompletedProcess`` + foundation-native helpers.

    Subclassing keeps every attribute callers already use (``args``,
    ``returncode``, ``stdout``, ``stderr``) while adding the ``.ok`` / ``.check()``
    / ``.out`` / ``.errtext`` shape higher layers rely on (mirrored by
    ``foundation.actions.result``).
    """

    @property
    def ok(self) -> bool:
        """True iff the process exited zero."""
        return self.returncode == 0

    @property
    def out(self) -> str:
        """Stripped stdout (``""`` when not captured)."""
        return (self.stdout or "").strip()

    @property
    def errtext(self) -> str:
        """Stripped stderr (``""`` when not captured)."""
        return (self.stderr or "").strip()

    def check(self, fail_msg: Optional[str] = None) -> "Completed":
        """Return ``self`` if the process succeeded, else raise :class:`ProcError`.

        Lets a caller defer the success check past :func:`run`'s own ``check=``
        gate — ``proc.run(cmd).check()`` reads naturally at a call site that first
        wants to inspect ``returncode`` for itself.
        """
        if not self.ok:
            raise ProcError(
                fail_msg or f"command failed (rc={self.returncode}): {_fmt(self.args)}",
                cmd=self.args, returncode=self.returncode, stderr=self.errtext,
            )
        return self


class Proc:
    """A managed running process — the handle :func:`popen` yields.

    Wraps a live :class:`subprocess.Popen` with foundation's vocabulary so a
    long-running child reads the same as the one-shot helpers::

        with proc.popen(cmd) as p:
            p.wait()                 # block until it exits (raises ProcTimeout)
            text = p.output()        # captured stdout, stripped

    or, to relay progress as it happens::

        with proc.popen(cmd) as p:
            for line in p.stream():  # stdout line-by-line, live
                log(line)

    Either way the child is guaranteed to be reaped when the ``with`` block
    exits (see :func:`popen`). Note the method is ``wait()`` not ``await()`` —
    ``await`` is a reserved word in Python.

    ``output()`` reads through :meth:`subprocess.Popen.communicate`, so it is
    safe to call even after :meth:`wait`, and does not deadlock on output larger
    than the OS pipe buffer (the failure mode of a bare ``wait()`` + ``read()``).
    """

    def __init__(self, popen_obj: "subprocess.Popen", cmd) -> None:
        self._p = popen_obj
        self.cmd = cmd
        self._captured: Optional[tuple] = None  # (stdout, stderr) once communicated

    # -- proxy the Popen surface callers commonly reach for --------------------
    @property
    def pid(self) -> int:
        return self._p.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._p.returncode

    @property
    def stdin(self):
        return self._p.stdin

    @property
    def stdout(self):
        return self._p.stdout

    @property
    def stderr(self):
        return self._p.stderr

    @property
    def ok(self) -> bool:
        """True iff the process has exited zero (False while still running)."""
        return self._p.returncode == 0

    def poll(self) -> Optional[int]:
        """Return the exit code if the child has finished, else ``None``."""
        return self._p.poll()

    def send_signal(self, sig) -> None:
        self._p.send_signal(sig)

    # -- blocking completion --------------------------------------------------
    def wait(self, timeout: Optional[float] = None) -> int:
        """Block until the child exits and return its code; ``ProcTimeout`` on timeout."""
        try:
            return self._p.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ProcTimeout(
                f"{_name(self.cmd)!r} timed out after {timeout}s",
                cmd=self.cmd, timeout=timeout,
            ) from exc

    def communicate(self, input=None, timeout: Optional[float] = None) -> tuple:
        """Send ``input``, read remaining stdout/stderr, wait. ``ProcTimeout`` on timeout."""
        if self._captured is not None:
            return self._captured
        try:
            self._captured = self._p.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ProcTimeout(
                f"{_name(self.cmd)!r} timed out after {timeout}s",
                cmd=self.cmd, timeout=timeout,
            ) from exc
        return self._captured

    def output(self, *, timeout: Optional[float] = None) -> str:
        """Full stdout, stripped — waits for exit. Safe after :meth:`wait`."""
        out, _ = self.communicate(timeout=timeout)
        return (out or "").strip()

    def stream(self) -> Iterator[str]:
        """Yield stdout line-by-line (newline-stripped) as the child produces it.

        Requires the child to have been opened with a piped stdout (the default).
        """
        if self._p.stdout is None:
            raise ProcError("process was not opened with a stdout pipe", cmd=self.cmd)
        for line in self._p.stdout:
            yield line.rstrip("\n")
        self._p.wait()


def _name(cmd) -> str:
    """Best-effort program name for diagnostics, for list *or* string ``cmd``."""
    if isinstance(cmd, (list, tuple)) and cmd:
        return str(cmd[0])
    return str(cmd)


def _fmt(cmd) -> str:
    """Render ``cmd`` for an error message."""
    if isinstance(cmd, (list, tuple)):
        return " ".join(map(str, cmd))
    return str(cmd)


def run(cmd, *, check=False, capture=True, text=True, input=None,
        env=None, cwd=None, timeout=None, forward_stderr=False) -> Completed:
    """Robust wrapper over :func:`subprocess.run` returning a :class:`Completed`.

    - ``capture`` (default True) captures stdout/stderr instead of inheriting.
    - ``cwd`` runs the child in that working directory.
    - launch failures (``OSError``/``FileNotFoundError``) raise :class:`ProcError`
      and ``timeout`` raises :class:`ProcTimeout`, rather than leaking raw
      exceptions.
    - ``check``: raise :class:`ProcError` (with stderr attached) on non-zero.
    - ``forward_stderr``: echo the child's captured stderr to ours (useful when
      relaying a long-running child's diagnostics live).
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=capture, text=text,
            input=input, env=env, cwd=cwd, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcTimeout(
            f"{_name(cmd)!r} timed out after {timeout}s", cmd=cmd, timeout=timeout,
        ) from exc
    except OSError as exc:
        raise ProcError(f"could not launch {_name(cmd)!r}: {exc}", cmd=cmd) from exc
    completed = Completed(proc.args, proc.returncode, proc.stdout, proc.stderr)
    if forward_stderr and capture and completed.stderr:
        sys.stderr.write(completed.stderr)
    if check and not completed.ok:
        raise ProcError(
            f"command failed (rc={completed.returncode}): {_fmt(cmd)}",
            cmd=cmd, returncode=completed.returncode,
            stderr=(completed.stderr or "") if capture else "",
        )
    return completed


def run_text(cmd, *, default=None, **kw):
    """Stripped stdout on success; ``default`` if the process fails in any way.

    Swallows :class:`ProcError` (launch/timeout/non-zero). Use when a failure is
    non-fatal and a fallback value is appropriate.
    """
    try:
        return run(cmd, check=True, **kw).stdout.strip()
    except ProcError:
        return default


def run_json(cmd, *, default=None, **kw):
    """Parsed-JSON stdout on success; ``default`` on launch/non-zero/parse error."""
    try:
        return json.loads(run(cmd, check=True, **kw).stdout)
    except (ProcError, ValueError, TypeError):
        return default


def run_lines(cmd, *, default=None, **kw) -> Optional[List[str]]:
    """Non-empty stripped stdout lines on success; ``default`` (or ``[]``) on failure.

    Convenience for the common "list of ids / names, one per line" shell idiom.
    """
    try:
        out = run(cmd, check=True, **kw).stdout or ""
    except ProcError:
        return [] if default is None else default
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def run_checked(cmd, *, fail_msg=None, on_fail=None, forward_stderr=True, **kw):
    """Raw stdout on success; escalate on failure.

    On launch/timeout/non-zero the child's stderr is forwarded to ours, then:
      * if ``on_fail`` is given it is called with ``fail_msg`` (expected to
        raise — e.g. ``common.die`` → ``PipelineExit``), otherwise
      * a :class:`ProcError` carrying ``fail_msg`` is raised.
    Returns stdout **unstripped** so JSON/whitespace-sensitive callers decide.
    """
    try:
        return run(cmd, check=True, forward_stderr=forward_stderr, **kw).stdout
    except ProcError as exc:
        msg = fail_msg or str(exc)
        if on_fail is not None:
            on_fail(msg)  # expected to raise
        raise ProcError(msg, cmd=exc.cmd, returncode=exc.returncode,
                        stderr=exc.stderr) from exc


def terminate(proc: "subprocess.Popen", kill_timeout: float = DEFAULT_KILL_TIMEOUT) -> None:
    """Best-effort reap of a still-running child: ``terminate()`` then ``kill()``.

    Idempotent and exception-safe — safe to call on an already-exited process.
    Gives the child ``kill_timeout`` seconds to honour SIGTERM before SIGKILL.
    """
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=kill_timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return
    try:
        proc.kill()
        proc.wait(timeout=kill_timeout)
    except (OSError, subprocess.TimeoutExpired):
        pass


@contextmanager
def popen(cmd, *, env=None, cwd=None, text=True,
          stdin=None, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
          kill_timeout: float = DEFAULT_KILL_TIMEOUT,
          start_new_session: bool = False) -> Iterator[Proc]:
    """Context manager around :class:`subprocess.Popen` with guaranteed cleanup.

    Yields a :class:`Proc` handle (``.wait()`` / ``.output()`` / ``.stream()``) over
    a live child. However the block exits — normally, ``break``, or an exception —
    the child is reaped via :func:`terminate` (terminate→kill), so a process opened
    here is never leaked::

        with proc.popen([tool, *args], cwd=workdir) as p:
            for line in p.stream():
                relay(line)
            rc = p.wait()

    Launch failures surface as :class:`ProcError` rather than raw ``OSError``.
    ``start_new_session`` puts the child in its own process group, so cleanup of a
    pipeline/shell-tree does not also signal the parent.
    """
    try:
        raw = subprocess.Popen(
            cmd, env=env, cwd=cwd, text=text,
            stdin=stdin, stdout=stdout, stderr=stderr,
            start_new_session=start_new_session,
        )
    except OSError as exc:
        raise ProcError(f"could not launch {_name(cmd)!r}: {exc}", cmd=cmd) from exc
    handle = Proc(raw, cmd)
    try:
        yield handle
    finally:
        terminate(raw, kill_timeout)


def stream(cmd, *, env=None, cwd=None, merge_stderr=True,
           kill_timeout: float = DEFAULT_KILL_TIMEOUT,
           start_new_session: bool = False) -> Iterator[str]:
    """Iterate a child's stdout line-by-line (newline-stripped) as it arrives.

    A thin generator over :func:`popen`: the child is launched, its output is
    yielded incrementally (so a long-running tool's progress can be relayed live),
    and — because it delegates to :func:`popen` — the process is always reaped when
    the generator is exhausted or closed early. With ``merge_stderr`` (default),
    stderr is folded into the same stream; set it False to drop stderr.
    """
    err = subprocess.STDOUT if merge_stderr else subprocess.DEVNULL
    with popen(cmd, env=env, cwd=cwd, text=True, stdout=subprocess.PIPE,
               stderr=err, kill_timeout=kill_timeout,
               start_new_session=start_new_session) as handle:
        yield from handle.stream()


def which(name: str, *, default: Optional[str] = None) -> Optional[str]:
    """Resolve an executable on ``PATH`` (``shutil.which``), or ``default`` if absent.

    Lets a caller probe for a tool (``proc.which("gh")``) without spawning it.
    """
    return shutil.which(name) or default
