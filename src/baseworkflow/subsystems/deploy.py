#!/usr/bin/env python3
"""deploy.py — provider deploy STRATEGIES, composed by ``admin:publish``.

``admin:publish`` ships a *completed* unit to its hosting providers. Rather than
hard-code vercel/supabase specifics into the action, each provider is a **Strategy**
behind one small interface (:class:`DeployProvider`): it declares whether it is
usable in this environment and the ordered shell steps to *create/link* its project
and *ship/migrate* the app. :func:`ship` is the **composition root** — it composes
the ordered provider list and runs every step through ONE dry-run-gated, fail-safe
loop. Adding a provider is adding a subclass + one entry in :func:`providers`; it is
never an edit to ``publish()``. (This is the repo's interface-before-implementation
paradigm: ``publish`` depends on the ``DeployProvider`` interface, not on any CLI.)

Secrets discipline (CLAUDE.md §8 — secrets only in env, never in logs/records):
  * Auth tokens are bridged into the ENVIRONMENT the CLI reads (``bridge_env``),
    never passed as CLI args.
  * A step may still need a secret arg the CLI has no env for (``supabase projects
    create --db-password``); such flags are declared in ``secret_flags`` and their
    value is masked (`***`) in every RECORDED / logged form. The executed form is
    resolved from the live environment but never stored.
  * ``@ENV:NAME`` arg tokens are late-bound: resolved from ``os.environ`` at
    execution time (so a value captured from an earlier step — e.g. a freshly
    created Supabase project ref — flows into a later step), and shown as
    ``$NAME`` (never the value) in the recorded form.

Fail-safe: deploy is the last build-phase step but runs BEFORE ``consolidate_pr``,
so a crash here would abort the tick before the engineer's branch is pushed. A
provider whose CLI/credentials are absent is SKIPPED with a recorded reason; a step
that cannot launch (ProcError) or returns non-zero ends that provider with a
recorded failure — never an exception that breaks the pipeline. Pushing the work
is not contingent on a successful deploy.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import common  # src/baseworkflow/subsystems/common.py — dry-run-aware runner + log
from engine import proc  # capturing subprocess wrapper (raises ProcError on launch fail)
from engine.env import Environment  # broad env: process + .env + Keychain, one view

# Flags whose FOLLOWING value is a secret and must be masked in any recorded form.
_SECRET_FLAGS = {"--db-password", "--password", "--token"}

# One Environment for the whole publish (process env + .env files + macOS Keychain),
# so a provider's token resolves wherever it lives — even under an unusual key name.
_ENV: Optional[Environment] = None


def _environment() -> Environment:
    global _ENV
    if _ENV is None:
        _ENV = Environment.default()
    return _ENV


def _lookup(name: str, *hints: str) -> Optional[str]:
    """Resolve a credential by canonical ``name`` across all sources; failing that,
    fall back to :meth:`Environment.find_token` over ``hints`` so an oddly named key
    (``VC_PAT`` for vercel) still resolves."""
    env = _environment()
    val = env.get(name)
    if val:
        return val
    if hints:
        match = env.find_token(*hints)
        if match:
            return match.value
    return None


def _redact(args: List[Any], secret_flags: Optional[List[str]] = None) -> List[str]:
    """The safe-to-log / safe-to-store form of an arg vector: ``@ENV:NAME`` tokens
    become ``$NAME`` (never the value) and the value after any secret flag is
    masked. Used for the greppable DRY-RUN line AND the durable ``mutations``."""
    secret = set(secret_flags or ()) | _SECRET_FLAGS
    out: List[str] = []
    mask_next = False
    for a in args:
        s = str(a)
        if mask_next:
            out.append("***")
            mask_next = False
            continue
        if s.startswith("@ENV:"):
            out.append("$" + s[5:])
        else:
            out.append(s)
        if s in secret:
            mask_next = True
    return out


def _resolve(args: List[Any]) -> List[str]:
    """The executed form: ``@ENV:NAME`` → its live ``os.environ`` value (late-bound,
    so a value captured from an earlier step is visible here). Never stored."""
    return [os.environ.get(s[5:], "") if (s := str(a)).startswith("@ENV:") else str(a)
            for a in args]


# --------------------------------------------------------------------------- #
# The Strategy interface + concrete providers.                                  #
# --------------------------------------------------------------------------- #
class DeployProvider:
    """One hosting provider, as a deploy Strategy. Subclasses declare usability and
    the ordered steps; :func:`ship` owns execution, dry-run gating, and fail-safe."""

    name = "provider"

    def usable(self) -> Tuple[bool, str]:
        """``(usable, reason)`` — CLI on PATH and credentials present in env."""
        raise NotImplementedError

    def prepare(self) -> None:
        """Bring this provider's credentials from the broad environment (process env,
        .env files, Keychain) into ``os.environ`` under the names its CLI and the
        ``@ENV:`` step tokens read — so a token stored only in the Keychain still
        reaches the subprocess, and no secret is ever passed as a CLI arg. Called
        once on the live path only, BEFORE :meth:`usable` and :meth:`steps`."""

    def steps(self, project: str, project_dir: Optional[str]) -> List[Dict[str, Any]]:
        """Ordered ``{label, args[, secret_flags, capture]}`` step descriptors:
        create/link first, then ship/migrate. ``args`` may carry ``@ENV:NAME``
        late-bound tokens; ``capture`` = ``{"env": NAME, "pattern": regex}`` extracts
        a value from a step's stdout into the env for later steps."""
        raise NotImplementedError


class SupabaseProvider(DeployProvider):
    """Supabase: create (or link an existing) project, then push DB migrations.

    Auth is the ``SUPABASE_ACCESS_TOKEN`` env var (read by the CLI natively). With
    ``SUPABASE_PROJECT_REF`` set we link that project; otherwise we create one (needs
    ``SUPABASE_ORG_ID`` + ``SUPABASE_DB_PASSWORD``; region from ``SUPABASE_REGION``,
    default ``us-east-1``) and capture the new ref for the link + push steps."""

    name = "supabase"

    def __init__(self) -> None:
        self.bin = os.environ.get("SUPABASE_BIN", "supabase")

    def prepare(self) -> None:
        # Bridge credentials from the broad env (process / .env / Keychain) into
        # os.environ so the CLI and the @ENV: step tokens resolve them consistently.
        token = _lookup("SUPABASE_ACCESS_TOKEN", "supabase")
        if token:
            os.environ["SUPABASE_ACCESS_TOKEN"] = token
        for var in ("SUPABASE_ORG_ID", "SUPABASE_DB_PASSWORD",
                    "SUPABASE_PROJECT_REF", "SUPABASE_REGION"):
            if not os.environ.get(var):
                val = _environment().get(var)
                if val:
                    os.environ[var] = val

    def usable(self) -> Tuple[bool, str]:
        if not common.have_tool(self.bin):
            return (False, f"{self.bin} CLI not installed")
        if not _lookup("SUPABASE_ACCESS_TOKEN", "supabase"):
            return (False, "no Supabase token found (set SUPABASE_ACCESS_TOKEN in env/.env/Keychain)")
        if not (os.environ.get("SUPABASE_PROJECT_REF")
                or (os.environ.get("SUPABASE_ORG_ID") and os.environ.get("SUPABASE_DB_PASSWORD"))):
            return (False, "set SUPABASE_PROJECT_REF, or SUPABASE_ORG_ID + SUPABASE_DB_PASSWORD to create")
        return (True, "")

    def steps(self, project: str, project_dir: Optional[str]) -> List[Dict[str, Any]]:
        workdir = ["--workdir", project_dir] if project_dir else []
        steps: List[Dict[str, Any]] = []
        if not os.environ.get("SUPABASE_PROJECT_REF"):
            region = os.environ.get("SUPABASE_REGION", "us-east-1")
            steps.append({
                "label": "create-project",
                "args": [self.bin, "projects", "create", project,
                         "--org-id", "@ENV:SUPABASE_ORG_ID",
                         "--db-password", "@ENV:SUPABASE_DB_PASSWORD",
                         "--region", region],
                "secret_flags": ["--db-password"],
                # Supabase project refs are 20 lowercase letters; capture into the env
                # so the link/push steps below target the just-created project.
                "capture": {"env": "SUPABASE_PROJECT_REF", "pattern": r"[a-z]{20}"},
            })
        steps.append({"label": "link",
                      "args": [self.bin, "link", "--project-ref", "@ENV:SUPABASE_PROJECT_REF", *workdir]})
        steps.append({"label": "db-push",
                      "args": [self.bin, "db", "push", *workdir]})
        return steps


class VercelProvider(DeployProvider):
    """Vercel: create/link the project, then deploy the frontend to production.

    Auth is ``VERCEL_ACCESS_TOKEN``, bridged to the ``VERCEL_TOKEN`` env var the CLI
    reads (so the token never appears in an arg vector). ``vercel link --yes
    --project <slug>`` creates the project on first run and links on later ticks
    (idempotent by the deterministic repo-derived slug)."""

    name = "vercel"

    def __init__(self) -> None:
        self.bin = os.environ.get("VERCEL_BIN", "vercel")

    def usable(self) -> Tuple[bool, str]:
        if not common.have_tool(self.bin):
            return (False, f"{self.bin} CLI not installed")
        if not _lookup("VERCEL_ACCESS_TOKEN", "vercel"):
            return (False, "no Vercel token found (set VERCEL_ACCESS_TOKEN in env/.env/Keychain)")
        return (True, "")

    def prepare(self) -> None:
        # Resolve the token from anywhere (env / .env / Keychain / unusual key) and
        # bridge it to VERCEL_TOKEN — the var the CLI reads — so no --token arg (and
        # thus no secret) is ever recorded. setdefault: an explicit VERCEL_TOKEN wins.
        token = _lookup("VERCEL_ACCESS_TOKEN", "vercel")
        if token:
            os.environ.setdefault("VERCEL_TOKEN", token)

    def steps(self, project: str, project_dir: Optional[str]) -> List[Dict[str, Any]]:
        cwd = ["--cwd", project_dir] if project_dir else []
        return [
            {"label": "link", "args": [self.bin, "link", "--yes", "--project", project, *cwd]},
            {"label": "deploy", "args": [self.bin, "deploy", "--prod", "--yes", *cwd]},
        ]


def providers() -> List[DeployProvider]:
    """The composition root: the ordered provider strategies publish ships through.
    DB first (Supabase) so the deployed frontend (Vercel) meets an up-to-date backend.
    Add a provider here + a subclass above — publish() does not change."""
    return [SupabaseProvider(), VercelProvider()]


# --------------------------------------------------------------------------- #
# Execution: one dry-run-gated, fail-safe loop over the composed strategies.     #
# --------------------------------------------------------------------------- #
def _run_provider(prov: DeployProvider, project: str, project_dir: Optional[str],
                  dry: bool) -> Dict[str, Any]:
    # Live: bridge credentials into os.environ BEFORE building steps, so a provider
    # whose step set depends on a resolved value (e.g. Supabase's SUPABASE_PROJECT_REF
    # branch) sees the same env the CLI will. Dry-run records intent only, no env read.
    if not dry:
        prov.prepare()
    steps = prov.steps(project, project_dir)
    mutations = [_redact(s["args"], s.get("secret_flags")) for s in steps]
    record: Dict[str, Any] = {"provider": prov.name, "mutations": mutations}

    if dry:
        # Record-only: print the greppable, REDACTED DRY-RUN line per step, no exec.
        for rec_args in mutations:
            common.run(*rec_args)
        record.update(ok=True, skipped=False, dry_run=True,
                      steps=[{"label": s["label"], "ok": True, "dry_run": True} for s in steps])
        return record

    usable, reason = prov.usable()
    if not usable:
        common.log(f"publish: {prov.name} skipped — {reason}")
        record.update(ok=False, skipped=True, reason=reason)
        return record

    # prepare() already bridged credentials into os.environ (top of this function).
    timeout = int(os.environ.get("PUBLISH_STEP_TIMEOUT_SECONDS", "600"))
    results: List[Dict[str, Any]] = []
    ok_all = True
    for s in steps:
        label = s["label"]
        try:
            res = proc.run(_resolve(s["args"]), capture=True, timeout=timeout)
        except proc.ProcError as exc:  # launch failure / timeout — recorded, never raised
            common.log(f"publish: {prov.name}:{label} could not launch ({exc})")
            results.append({"label": label, "ok": False, "error": str(exc)})
            ok_all = False
            break  # later steps depend on this one
        rc = res.returncode
        results.append({"label": label, "ok": rc == 0, "returncode": rc})
        if rc != 0:
            common.log(f"publish: {prov.name}:{label} failed (rc={rc})")
            ok_all = False
            break
        cap = s.get("capture")
        if cap and (res.stdout or ""):
            m = re.search(cap["pattern"], res.stdout)
            if m:
                os.environ[cap["env"]] = m.group(0)
    record.update(ok=ok_all, skipped=False, steps=results)
    return record


def ship(project: str, project_dir: Optional[str], *, dry_run: bool) -> Dict[str, Any]:
    """Compose the provider strategies and run each fail-safe. Returns
    ``{"deployments": [...per provider...], "mutations": [...redacted vectors...]}``.
    Secrets never appear in either field."""
    deployments: List[Dict[str, Any]] = []
    mutations: List[List[str]] = []
    for prov in providers():
        rec = _run_provider(prov, project, project_dir, dry_run)
        deployments.append(rec)
        mutations.extend(rec.get("mutations", []))
    return {"deployments": deployments, "mutations": mutations}
