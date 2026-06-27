"""proc.py — subprocess invocation for pipeline clients.

Centralises *how* the pipeline shells out to a child process so client modules
(the classifier, gh reads, the engineer/prep launchers, …) describe *what* to
run, not the capture / decode / error-handling plumbing.

Design: this is a **leaf** module — it imports only the standard library, never
the app contract or any other project module. That keeps it free of import
cycles (the contract and the stage modules may import *it*) and side-effect-free
to import. Failure policy is injected by the caller via ``on_fail`` (e.g. the
orchestration ``common.die``) rather than hard-wired here.

Callers always pass a fully-formed argv list (never a shell string), so nothing
here interprets untrusted text — it only executes commands the caller assembled.
"""
from __future__ import annotations

import json
import subprocess
import sys


class ProcError(RuntimeError):
    """A child process could not be launched, timed out, or exited non-zero.

    Carries the ``cmd``, ``returncode`` and captured ``stderr`` so callers that
    want richer handling than the helpers provide can inspect them.
    """

    def __init__(self, message, *, cmd=None, returncode=None, stderr=""):
        super().__init__(message)
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr or ""


def run(cmd, *, check=False, capture=True, text=True, input=None,
        env=None, timeout=None, forward_stderr=False):
    """Robust wrapper over :func:`subprocess.run` returning a CompletedProcess.

    - ``capture`` (default True) captures stdout/stderr instead of inheriting.
    - launch failures (``OSError``/``FileNotFoundError``) and ``timeout`` are
      normalised into :class:`ProcError` rather than leaking raw exceptions.
    - ``check``: raise :class:`ProcError` (with stderr attached) on non-zero.
    - ``forward_stderr``: echo the child's captured stderr to ours (useful when
      streaming a sub-tool's diagnostics, e.g. the engineer).
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=capture, text=text,
            input=input, env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProcError(f"{cmd[0]!r} timed out after {timeout}s", cmd=cmd) from exc
    except OSError as exc:
        raise ProcError(f"could not launch {cmd[0]!r}: {exc}", cmd=cmd) from exc
    if forward_stderr and capture and proc.stderr:
        sys.stderr.write(proc.stderr)
    if check and proc.returncode != 0:
        raise ProcError(
            f"command failed (rc={proc.returncode}): {' '.join(map(str, cmd))}",
            cmd=cmd, returncode=proc.returncode,
            stderr=(proc.stderr or "") if capture else "",
        )
    return proc


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
