"""runtime.py — generic runtime / OS plumbing for the engine (stdlib-only leaf).

Reusable mechanisms with **no** pipeline/domain knowledge. Each is a named
pattern an engine consumer can adopt without dragging dispatch policy along:

  * **Clock** — UTC timestamps (``utc_hms`` / ``utc_iso8601``) plus epoch / ISO
    parsing (``parse_epoch``) with a test seam (``now_epoch(override)``).
  * **Logger / Die** — subsystem-tagged stderr diagnostics
    (``[HH:MM:SSZ] <subsys>: <msg>``); ``die`` emits an ERROR line then aborts
    by raising a caller-chosen ``SystemExit`` subclass.
  * **env config** — ``default()`` / ``env()`` (the shell ``: "${KEY:=…}"``
    idiom) and ``load_dotenv()`` to overlay a ``KEY=VALUE`` file while preserving
    explicit pre-launch overrides.
  * **DryRunner** — gate a side-effecting command behind a flag: print a single
    greppable line under dry-run, otherwise hand the tokens to an executor.
  * **tool checks** — ``have_tool`` / ``require_tool``: is an external tool on
    PATH (failure policy injected, never hard-wired).
  * **file_lock** — single-host advisory ``flock`` with graceful degradation to
    UNLOCKED; diagnostics are injected so the wording stays the caller's.

What the subsystem name is, which env vars carry policy, the dry-run prefix, the
lock path and its log wording — none of that is baked in; callers supply it. The
*mechanism* lives here; the *policy / state* stays with the caller (the same
split as ``structures.py`` and ``filesys.py``). Leaf module: stdlib only, no
imports from ``engine.*`` / ``src.*``, so anything may import it without cycle
risk.
"""
from __future__ import annotations

import fcntl
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Union


# --------------------------------- Clock -----------------------------------
def utc_hms() -> str:
    """Short UTC wall-clock stamp, ``HH:MM:SSZ`` (the log-line timestamp)."""
    return datetime.now(timezone.utc).strftime("%H:%M:%SZ")


def utc_iso8601() -> str:
    """UTC instant as ``YYYY-MM-DDTHH:MM:SSZ`` (record / event timestamp)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_epoch(v: str) -> Optional[int]:
    """Parse epoch-seconds | ISO-8601 to epoch seconds; ``None`` on empty/failure."""
    if not v:
        return None
    if re.match(r"^[0-9]+$", v):
        return int(v)
    s = v.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def now_epoch(override: str = "") -> int:
    """Current epoch seconds, or ``override`` (epoch|ISO) for tests.

    An unparseable override falls back to *now* rather than raising — the seam is
    for deterministic tests, not a hard input contract.
    """
    if not override:
        return int(datetime.now(timezone.utc).timestamp())
    parsed = parse_epoch(override)
    return parsed if parsed is not None else int(datetime.now(timezone.utc).timestamp())


# ----------------------------- Logger / Die --------------------------------
class Die(SystemExit):
    """Default abort type raised by :meth:`Logger.die` (exit after an ERROR line)."""


class Logger:
    """Subsystem-tagged stderr diagnostics: ``[HH:MM:SSZ] <subsys>: <msg>``.

    ``subsys`` is a string or a zero-arg callable resolved per line, so the tag
    can follow ``argv`` / an env override without rebuilding the logger.
    """

    def __init__(self, subsys: Union[str, Callable[[], str]]) -> None:
        self._subsys = subsys

    def _tag(self) -> str:
        return self._subsys() if callable(self._subsys) else self._subsys

    def log(self, msg: str) -> None:
        print(f"[{utc_hms()}] {self._tag()}: {msg}", file=sys.stderr)

    def warn(self, msg: str) -> None:
        print(f"[{utc_hms()}] {self._tag()}: WARN: {msg}", file=sys.stderr)

    def err(self, msg: str) -> None:
        print(f"[{utc_hms()}] {self._tag()}: ERROR: {msg}", file=sys.stderr)

    def die(self, msg: str, *, exc: type = Die, code: int = 1):
        """Emit an ERROR line then raise ``exc(code)`` (defaults to :class:`Die`)."""
        self.err(msg)
        raise exc(code)


# ------------------------------- env config --------------------------------
def default(key: str, value: str) -> str:
    """Shell ``: "${KEY:=default}"`` — set env default if unset/empty, return it."""
    if not os.environ.get(key):
        os.environ[key] = value
    return os.environ[key]


def env(key: str, default: str = "") -> str:  # noqa: A002 - mirrors os.environ.get
    """Read an env var with a fallback; the live source of truth at call time."""
    return os.environ.get(key, default)


def load_dotenv(path: Union[str, Path], *, preserve: Iterable[str] = ()) -> None:
    """Overlay ``KEY=VALUE`` lines from ``path`` onto ``os.environ`` (no-op if absent).

    ``export `` prefixes, blank lines and ``#`` comments are ignored; surrounding
    single/double quotes are stripped. Any key named in ``preserve`` that already
    carries a value is restored afterwards, so an explicit pre-launch override is
    never clobbered by the file's default.
    """
    p = Path(path)
    if not p.is_file():
        return
    kept = {k: os.environ[k] for k in preserve if os.environ.get(k)}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        k, _, val = line.partition("=")
        os.environ[k.strip()] = val.strip().strip('"').strip("'")
    os.environ.update(kept)


# ------------------------------- DryRunner ---------------------------------
class DryRunner:
    """Gate a side-effecting command behind a flag.

    When ``is_dry_run()`` is true, print ``<prefix><space-joined tokens>`` and
    return ``dry_rc`` without running anything; otherwise hand the token list to
    ``execute`` and return its int result. The printed form is the greppable seam
    tests assert on, so ``prefix`` is the caller's to fix.
    """

    def __init__(
        self,
        is_dry_run: Callable[[], bool],
        execute: Callable[[list], int],
        *,
        prefix: str = "DRY-RUN: ",
        dry_rc: int = 0,
    ) -> None:
        self._is_dry_run = is_dry_run
        self._execute = execute
        self._prefix = prefix
        self._dry_rc = dry_rc

    def run(self, *args) -> int:
        tokens = [str(a) for a in args]
        if self._is_dry_run():
            print(self._prefix + " ".join(tokens))
            return self._dry_rc
        return self._execute(tokens)


# ------------------------------- tool checks -------------------------------
def have_tool(name: str) -> bool:
    """Is ``name`` resolvable on PATH?"""
    return shutil.which(name) is not None


def require_tool(name: str, on_missing: Callable[[str], None]) -> None:
    """Assert ``name`` is on PATH; otherwise call ``on_missing`` (expected to raise)."""
    if shutil.which(name) is None:
        on_missing(f"required tool not found on PATH: {name}")


# ------------------------------- file_lock ---------------------------------
class LockBusy(Exception):
    """Raised when a non-blocking advisory lock is already held by another holder."""


class _Unlocked:
    """Degraded context manager: runs the body without serialization."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FileLock:
    """Holds an exclusive advisory ``flock`` for the life of a ``with`` block.

    ``wait`` <= 0 is non-blocking (``LOCK_NB``): a contended lock raises
    :class:`LockBusy`. A positive ``wait`` blocks up to that many seconds,
    emulated with ``SIGALRM`` since :mod:`fcntl` has no timeout.
    """

    def __init__(self, fd: int, wait: int, on_busy: Optional[Callable[[], None]]):
        self._fd = fd
        self._wait = wait
        self._on_busy = on_busy

    def __enter__(self):
        if self._wait <= 0:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if self._on_busy:
                    self._on_busy()
                raise LockBusy()
            return self
        import signal

        def _timeout(_signum, _frame):
            raise OSError("flock wait timed out")

        old = signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(self._wait)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError:
            if self._on_busy:
                self._on_busy()
            raise LockBusy()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
        return False


def file_lock(
    path: Union[str, Path],
    *,
    wait: int = 0,
    flock_bin: str = "flock",
    on_no_flock: Optional[Callable[[str], None]] = None,
    on_open_fail: Optional[Callable[[str], None]] = None,
    on_busy: Optional[Callable[[], None]] = None,
):
    """Return a context manager holding an exclusive single-host advisory lock.

    Degrades to an UNLOCKED context manager (after calling the matching
    diagnostic hook) rather than aborting when the lock cannot be acquired
    *infrastructurally* — the ``flock`` binary is absent (``on_no_flock``) or the
    lock file cannot be opened (``on_open_fail``). A genuinely *contended* lock is
    not degraded: it raises :class:`LockBusy` after ``on_busy``. The diagnostic
    hooks carry the caller's exact wording; this module supplies only control flow.
    """
    if not have_tool(flock_bin):
        if on_no_flock:
            on_no_flock(flock_bin)
        return _Unlocked()
    lock = str(path)
    try:
        Path(lock).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    except OSError:
        if on_open_fail:
            on_open_fail(lock)
        return _Unlocked()
    return _FileLock(fd, wait, on_busy)
