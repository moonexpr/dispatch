#!/usr/bin/env python3
"""foundation/env.py — a broad, consistent environment aggregator.

A caller's configuration (tokens, keys, settings) can live in several places: the
process environment, one or more ``.env`` files, and — on macOS — the login
Keychain. This module aggregates those SOURCES behind one interface so a lookup
resolves the same way no matter where a value sits, and so a consumer can *search*
for a value even when it does not know the exact key.

Two access methods, as asked:
  * **Direct** — :meth:`Environment.get` (first source that has the key wins).
  * **Search** — :meth:`Environment.query` / :meth:`Environment.find_token`, so an
    agent can locate an access token whose key uses an unusual name
    (``VERCEL_ACCESS_TOKEN`` vs ``VERCEL_TOKEN`` vs ``VC_PAT``) or whose value is
    simply *shaped* like a token.

And a writer:
  * **Save** — :meth:`Environment.save` dumps secrets into an accessible store
    (the Keychain by default, or a dotenv file) so they persist across runs.

OOP shape — Strategy + Composite:
    EnvSource (interface)        get / items / set, + ``name`` / ``writable``
      ├─ ProcessEnvSource        os.environ (always present; ephemeral writes)
      ├─ DotEnvSource(path)      a KEY=VALUE file (enumerable; file-backed writes)
      └─ KeychainSource          macOS `security` CLI (direct get + a managed index)
    Environment                  composes an ordered list of EnvSource and offers
                                 get / query / find_token / save across all of them.

Secrets discipline (CLAUDE.md §8): values are NEVER logged and never appear in a
``repr`` or a report — :func:`mask` previews them as ``abc…yz (len 40)``. Raw
values cross the boundary only through an explicit :meth:`Environment.get` /
``EnvMatch.value``; the search surface returns provenance + masked previews.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from foundation import proc  # capturing subprocess wrapper (ProcError on launch failure)

# A key whose NAME hints it holds a credential, and a value SHAPED like a token.
_TOKEN_KEY_HINT = re.compile(
    r"(token|secret|key|passwd|password|pat|credential|api[_-]?key|access)", re.I)
_TOKEN_VALUE = re.compile(r"^[A-Za-z0-9_\-.=+/]{16,}$")


def mask(value: Optional[str]) -> str:
    """A non-reversible preview of a secret: ``abc…yz (len 40)``; ``<unset>`` /
    ``<empty>`` for missing/blank. Safe to log."""
    if value is None:
        return "<unset>"
    if value == "":
        return "<empty>"
    n = len(value)
    if n <= 6:
        return f"{value[0]}… (len {n})"
    return f"{value[:3]}…{value[-2:]} (len {n})"


def looks_like_token(key: str, value: Optional[str]) -> bool:
    """Heuristic: does ``key=value`` look like an access token? True when the key
    name hints a credential, or the value is long and token-charactered."""
    if not value:
        return False
    if _TOKEN_KEY_HINT.search(key or ""):
        return True
    return len(value) >= 20 and bool(_TOKEN_VALUE.match(value))


@dataclass
class EnvMatch:
    """One search hit: the key, the source it came from, and its value. ``preview``
    is the masked form; ``repr`` never exposes the raw value."""
    key: str
    source: str
    value: str = field(repr=False, default="")

    @property
    def preview(self) -> str:
        return mask(self.value)

    def __repr__(self) -> str:  # never leak the value
        return f"EnvMatch(key={self.key!r}, source={self.source!r}, value={self.preview})"


# --------------------------------------------------------------------------- #
# Sources — the Strategy implementations.                                       #
# --------------------------------------------------------------------------- #
class EnvSource:
    """One place values can live. Subclasses implement direct get, enumeration
    (``items``; empty when the source is not enumerable), and optional writes."""

    name = "source"
    writable = False

    def get(self, key: str) -> Optional[str]:
        raise NotImplementedError

    def items(self) -> Dict[str, str]:
        """All ``{key: value}`` this source can enumerate (``{}`` if it cannot)."""
        return {}

    def set(self, key: str, value: str) -> bool:  # noqa: ARG002
        """Persist ``key=value``; return False when the source is read-only."""
        return False


class ProcessEnvSource(EnvSource):
    """The live process environment (``os.environ``). Always present; writes are
    in-process only (they vanish with the process)."""

    name = "env"
    writable = True

    def get(self, key: str) -> Optional[str]:
        return os.environ.get(key)

    def items(self) -> Dict[str, str]:
        return dict(os.environ)

    def set(self, key: str, value: str) -> bool:
        os.environ[key] = value
        return True


class DotEnvSource(EnvSource):
    """A ``KEY=VALUE`` dotenv file (e.g. ``.env`` / ``pipeline.env``). Parsing mirrors
    ``foundation.runtime.load_dotenv`` (``export`` prefixes, ``#`` comments and quotes
    handled) but reads into this source instead of mutating ``os.environ``. Writes
    rewrite the file in place, replacing a key's line or appending it."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)
        self.name = f"dotenv:{self.path.name}"
        self.writable = True

    @staticmethod
    def _parse(text: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            k, _, val = line.partition("=")
            out[k.strip()] = val.strip().strip('"').strip("'")
        return out

    def items(self) -> Dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            return self._parse(self.path.read_text())
        except OSError:
            return {}

    def get(self, key: str) -> Optional[str]:
        return self.items().get(key)

    def set(self, key: str, value: str) -> bool:
        try:
            lines = self.path.read_text().splitlines() if self.path.is_file() else []
        except OSError:
            return False
        prefix = f"{key}="
        replaced = False
        for i, raw in enumerate(lines):
            stripped = raw.strip()
            body = stripped[len("export "):] if stripped.startswith("export ") else stripped
            if body.startswith(prefix):
                lines[i] = f"{key}={value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{key}={value}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("\n".join(lines) + "\n")
            return True
        except OSError:
            return False


class KeychainSource(EnvSource):
    """The macOS login Keychain, via the ``security`` CLI. Direct ``get`` works for
    any key stored as a generic password whose *service* is the key name; a caller
    namespaces its own writes under one account and keeps an index entry so the keys
    it saved are enumerable (the full Keychain is deliberately NOT dumped). A no-op
    on non-macOS or when ``security`` is unavailable."""

    name = "keychain"
    _INDEX_SERVICE = "dispatch-env-index"

    def __init__(self, account: str = "dispatch") -> None:
        self.account = account
        self.available = sys.platform == "darwin" and self._have_security()
        self.writable = self.available

    @staticmethod
    def _have_security() -> bool:
        try:
            return proc.run(["/usr/bin/security", "help"], capture=True).returncode == 0
        except proc.ProcError:
            return False

    def _find(self, service: str) -> Optional[str]:
        if not self.available:
            return None
        try:
            res = proc.run(
                ["/usr/bin/security", "find-generic-password",
                 "-s", service, "-a", self.account, "-w"], capture=True)
        except proc.ProcError:
            return None
        if res.returncode != 0:
            return None
        out = (res.stdout or "").rstrip("\n")
        return out or None

    def get(self, key: str) -> Optional[str]:
        # Try the account-namespaced entry first, then a service-only entry so a
        # token stored by another tool (service=key, any account) still resolves.
        val = self._find(key)
        if val is not None or not self.available:
            return val
        try:
            res = proc.run(
                ["/usr/bin/security", "find-generic-password", "-s", key, "-w"],
                capture=True)
        except proc.ProcError:
            return None
        out = (res.stdout or "").rstrip("\n") if res.returncode == 0 else ""
        return out or None

    def _index(self) -> List[str]:
        raw = self._find(self._INDEX_SERVICE)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return [str(k) for k in data] if isinstance(data, list) else []
        except ValueError:
            return []

    def _write(self, service: str, value: str) -> bool:
        if not self.available:
            return False
        try:
            # -U updates an existing item instead of erroring on duplicate.
            res = proc.run(
                ["/usr/bin/security", "add-generic-password",
                 "-s", service, "-a", self.account, "-w", value, "-U"], capture=True)
            return res.returncode == 0
        except proc.ProcError:
            return False

    def items(self) -> Dict[str, str]:
        # Only the keys saved via this source's index — never a full dump.
        out: Dict[str, str] = {}
        for k in self._index():
            v = self._find(k)
            if v is not None:
                out[k] = v
        return out

    def set(self, key: str, value: str) -> bool:
        if not self._write(key, value):
            return False
        keys = self._index()
        if key not in keys:
            keys.append(key)
            self._write(self._INDEX_SERVICE, json.dumps(sorted(keys)))
        return True


# --------------------------------------------------------------------------- #
# The aggregator — Composite over the sources.                                  #
# --------------------------------------------------------------------------- #
class Environment:
    """A consistent view over an ordered list of :class:`EnvSource`. Earlier sources
    win on direct lookups; search spans them all with provenance."""

    def __init__(self, sources: List[EnvSource]) -> None:
        self.sources = sources

    @classmethod
    def default(cls, *, repo_root: Optional[Union[str, Path]] = None,
                dotenv_paths: Optional[Iterable[Union[str, Path]]] = None) -> "Environment":
        """A standard source stack: process env (highest priority) → ``.env`` →
        ``pipeline.env`` → Keychain (lowest). ``dotenv_paths`` overrides the file set."""
        root = Path(repo_root) if repo_root else Path(
            os.environ.get("DISPATCH_APP_DIR", "")) if os.environ.get("DISPATCH_APP_DIR") \
            else Path(__file__).resolve().parent.parent
        if dotenv_paths is None:
            dotenv_paths = [root / ".env", root / "pipeline.env"]
        sources: List[EnvSource] = [ProcessEnvSource()]
        sources += [DotEnvSource(p) for p in dotenv_paths]
        sources.append(KeychainSource())
        return cls(sources)

    # -- direct lookup ------------------------------------------------------- #
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """The value for ``key`` from the first source that has it, else ``default``."""
        for src in self.sources:
            val = src.get(key)
            if val is not None:
                return val
        return default

    def get_with_source(self, key: str) -> EnvMatch:
        """:meth:`get` plus provenance (which source supplied it)."""
        for src in self.sources:
            val = src.get(key)
            if val is not None:
                return EnvMatch(key=key, source=src.name, value=val)
        return EnvMatch(key=key, source="<none>", value="")

    # -- search -------------------------------------------------------------- #
    def query(self, pattern: Optional[str] = None, *, search_values: bool = False,
              token_like: bool = False, regex: bool = False) -> List[EnvMatch]:
        """Search every enumerable source. A hit is a key (or value, when
        ``search_values``) matching ``pattern`` (substring by default, regex when
        ``regex``), and/or a token-shaped entry when ``token_like``. Returns
        :class:`EnvMatch` results in source-priority order, de-duplicated by key
        (highest-priority source wins). ``pattern=None`` with ``token_like=True``
        lists every credential-looking value across the environment."""
        matcher = None
        if pattern is not None:
            matcher = re.compile(pattern, re.I) if regex else None
            needle = None if regex else pattern.lower()

            def _hit(text: str) -> bool:
                return bool(matcher.search(text)) if matcher else (needle in text.lower())
        else:
            def _hit(text: str) -> bool:  # noqa: ARG001
                return True

        seen: set = set()
        out: List[EnvMatch] = []
        for src in self.sources:
            for k, v in src.items().items():
                if k in seen:
                    continue
                key_hit = _hit(k) if pattern is not None else False
                val_hit = bool(search_values and v and _hit(v))
                tok_hit = bool(token_like and looks_like_token(k, v))
                if pattern is None:
                    keep = tok_hit if token_like else True
                else:
                    keep = key_hit or val_hit or (token_like and tok_hit)
                if keep:
                    seen.add(k)
                    out.append(EnvMatch(key=k, source=src.name, value=v))
        return out

    def find_token(self, *hints: str, regex: bool = False) -> Optional[EnvMatch]:
        """Best-effort access-token finder for unusual key names. Returns the
        token-shaped entry whose key matches ANY ``hint`` (e.g. ``find_token("vercel")``
        catches ``VERCEL_ACCESS_TOKEN`` / ``VERCEL_TOKEN`` / ``VC_PAT`` alike),
        preferring the longest value. With no hints, returns the longest token-shaped
        value anywhere. ``None`` if nothing qualifies."""
        candidates = self.query(token_like=True)
        if hints:
            lowered = [h.lower() for h in hints]
            if regex:
                pats = [re.compile(h, re.I) for h in hints]
                candidates = [m for m in candidates if any(p.search(m.key) for p in pats)]
            else:
                candidates = [m for m in candidates
                              if any(h in m.key.lower() for h in lowered)]
        if not candidates:
            return None
        return max(candidates, key=lambda m: len(m.value))

    # -- save ---------------------------------------------------------------- #
    def save(self, data: Optional[Dict[str, str]] = None, *, store: str = "keychain",
             path: Optional[Union[str, Path]] = None,
             keys: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """Dump secrets into an accessible store so they persist.

        What is saved: an explicit ``data`` mapping; else the named ``keys`` resolved
        via :meth:`get`; else every token-shaped value discovered by ``query``.
        Where: ``store='keychain'`` (the macOS Keychain, default), ``'dotenv'`` (a
        file at ``path``, default ``<root>/.env.dispatch``), or ``'process'`` (the
        live env, ephemeral). Returns a report with masked previews — never raw
        values. Writing a dotenv file puts secrets on disk: keep ``path`` gitignored."""
        if data is None:
            if keys is not None:
                data = {k: (self.get(k) or "") for k in keys}
                data = {k: v for k, v in data.items() if v}
            else:
                data = {m.key: m.value for m in self.query(token_like=True)}

        target = self._store_for(store, path)
        if target is None or not getattr(target, "writable", False):
            return {"store": store, "ok": False, "saved": [], "skipped": list(data),
                    "error": f"store {store!r} is unavailable or read-only"}

        saved, failed = [], []
        for k, v in data.items():
            (saved if target.set(k, v) else failed).append(k)
        report = {"store": target.name, "ok": not failed, "saved": sorted(saved),
                  "failed": sorted(failed),
                  "previews": {k: mask(data[k]) for k in saved}}
        if store == "dotenv":
            report["path"] = str(getattr(target, "path", path))
            report["warning"] = "secrets written to disk — ensure this path is gitignored"
        return report

    def _store_for(self, store: str, path: Optional[Union[str, Path]]) -> Optional[EnvSource]:
        store = (store or "").lower()
        if store == "process":
            return self._first(ProcessEnvSource)
        if store == "keychain":
            kc = self._first(KeychainSource)
            return kc if kc and getattr(kc, "available", False) else None
        if store == "dotenv":
            if path is not None:
                return DotEnvSource(path)
            existing = self._first(DotEnvSource)
            if existing is not None:
                return existing
            root = Path(__file__).resolve().parent.parent
            return DotEnvSource(root / ".env.dispatch")
        return None

    def _first(self, cls: type) -> Optional[EnvSource]:
        for src in self.sources:
            if isinstance(src, cls):
                return src
        return None


# --------------------------------------------------------------------------- #
# CLI — so an operator or agent can drive it from a shell.                       #
#   python -m foundation.env get VERCEL_ACCESS_TOKEN          (prints the value)     #
#   python -m foundation.env query vercel                     (masked matches)       #
#   python -m foundation.env find vercel supabase             (token by hint)        #
#   python -m foundation.env tokens                            (all token-shaped)     #
#   python -m foundation.env save --store keychain [KEY ...]   (persist secrets)      #
# Note: `get` prints the raw value (for piping); the others mask by design.       #
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Search and persist the broad environment.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="direct lookup (prints the raw value)")
    g.add_argument("key")
    q = sub.add_parser("query", help="search keys (and optionally values)")
    q.add_argument("pattern")
    q.add_argument("--values", action="store_true", help="also match against values")
    q.add_argument("--regex", action="store_true", help="treat pattern as a regex")
    q.add_argument("--tokens", action="store_true", help="restrict to token-shaped hits")
    f = sub.add_parser("find", help="find an access token by key hint(s)")
    f.add_argument("hints", nargs="+")
    sub.add_parser("tokens", help="list every token-shaped value (masked)")
    s = sub.add_parser("save", help="persist secrets into a store")
    s.add_argument("keys", nargs="*", help="keys to save (default: all token-shaped)")
    s.add_argument("--store", default="keychain", choices=["keychain", "dotenv", "process"])
    s.add_argument("--path", default=None, help="dotenv path (for --store dotenv)")
    args = ap.parse_args(argv)

    env = Environment.default()
    if args.cmd == "get":
        val = env.get(args.key)
        if val is None:
            print(f"{args.key}: <unset>", file=sys.stderr)
            return 1
        print(val)
        return 0
    if args.cmd == "query":
        for m in env.query(args.pattern, search_values=args.values,
                           token_like=args.tokens, regex=args.regex):
            print(f"{m.key}\t{m.source}\t{m.preview}")
        return 0
    if args.cmd == "find":
        m = env.find_token(*args.hints)
        if m is None:
            print("no token-shaped value matched", file=sys.stderr)
            return 1
        print(f"{m.key}\t{m.source}\t{m.preview}")
        return 0
    if args.cmd == "tokens":
        for m in env.query(token_like=True):
            print(f"{m.key}\t{m.source}\t{m.preview}")
        return 0
    if args.cmd == "save":
        report = env.save(keys=args.keys or None, store=args.store, path=args.path)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report.get("ok") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
