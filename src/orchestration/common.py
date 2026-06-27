"""common.py — the Unattended Engineering Pipeline contract (orchestration layer).

Python port of ``scripts/lib/common.sh``, imported by every orchestration entry
point. This is the SINGLE source of truth for dispatch *policy*: the env-var
defaults, the scope->route map, the fix-ladder, dry-run semantics, the run-ledger
and the per-tick run record, the crash-reaper clock, and the thin wrappers around
``gh`` / ``claude`` / the Engineer that make those engines honour
PIPELINE_DRY_RUN.

It is deliberately **application** code, not engine code: it carries dispatch
business knowledge (which env vars exist, how a scope maps to a model tier, what a
"tick" records). The *generic mechanisms* it stands on — subsystem logging, the
dry-run runner, env-default / dotenv loading, the UTC clock + epoch parsing, the
single-host advisory lock, tool-presence checks — were factored out to
``engine.runtime`` (and subprocess invocation to ``engine.proc``) so they can be
reused without dragging this policy along. The label vocabulary + lifecycle state
machine are likewise DATA in ``app/config/state_machine.yml`` — assembled by
``engine.structures`` and exposed by ``statemachine.py``; this module no longer
holds the label table.

Design rules (HANDOFF-pipeline-v0.md §2), preserved from the shell:
  - We do NOT build engines. These helpers only *shape calls* to existing
    engines (gh, claude, python).
  - Dry-run defaults ON (§8). Nothing mutates unless PIPELINE_DRY_RUN=0.
  - All durable state lives in GitHub (labels/comments/PRs), never on disk.

Fidelity contract: smoke.sh greps byte-exact substrings out of these helpers
(``DRY-RUN: ...``, ``--add-label claimed``, the ``[time] subsys: msg`` log
shape, the ``event=started ...`` run-record line). Changing an output string
breaks the acceptance suite — keep them identical to the shell.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from engine import proc, runtime


# ---------------------------------------------------------------------------
# Repo-root resolution. This file lives at <root>/src/orchestration/.
# ---------------------------------------------------------------------------
PIPELINE_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("PIPELINE_ROOT", str(PIPELINE_ROOT))


# ---------------------------------------------------------------------------
# pipeline.env loading (never overrides already-set process vars for the four
# explicit CLI overrides). Operators copy pipeline.env.example -> pipeline.env
# (gitignored) and fill it. Absent at test time, so smoke.sh never hits this.
# The dotenv mechanism (overlay + preserve-explicit-overrides) lives in
# engine.runtime; the policy — *which* four vars are protected — stays here.
# ---------------------------------------------------------------------------
runtime.load_dotenv(
    PIPELINE_ROOT / "pipeline.env",
    preserve=("PIPELINE_DRY_RUN", "PIPELINE_CONCURRENCY", "PIPELINE_REPO", "ENGINEER_BIN"),
)


# Local alias keeps the dense env-default block below readable; the mechanism is
# the engine's shell ``: "${KEY:=default}"`` helper.
_default = runtime.default


# --------------------------- Env-var defaults ------------------------------
# Safety-critical: dry-run is ON unless explicitly disabled by the operator.
_default("PIPELINE_DRY_RUN", "1")
_default("PIPELINE_REPO", "")
_default("PIPELINE_CONFIDENCE_THRESHOLD", "0.55")
_default("PIPELINE_CONCURRENCY", "1")

# Tool binaries (overridable so tests / odd PATHs work).
_default("GH_BIN", "gh")
_default("CLAUDE_BIN", "claude")
_default("PYTHON_BIN", "python3")
_default("FLOCK_BIN", "flock")

# Tick concurrency lock (E1-1). Repo-scoped so distinct repos don't block.
_default(
    "DISPATCH_LOCK_FILE",
    f"{os.environ.get('TMPDIR', '/tmp').rstrip('/')}/dispatch-{os.environ['PIPELINE_REPO'].replace('/', '-')}.lock",
)
_default("DISPATCH_LOCK_WAIT", "0")

# Per-tick run record (E1-2).
_default(
    "DISPATCH_RUN_RECORD",
    f"{os.environ.get('DISPATCH_ARTIFACTS_DIR', '.dispatch')}/run-record.log",
)

# Crash reaper (E1-3).
_default("DISPATCH_REAPER_ENABLED", "1")
_default("DISPATCH_CLAIM_TIMEOUT_HOURS", "")
_default("DISPATCH_NOW_OVERRIDE", "")
_default("DISPATCH_REAPER_FIXTURE", "")

# How `claude` workflow args are passed.
_default("CLAUDE_ARGS_MODE", "prompt")

# Worktree isolation root for worker sessions (§5.1 / §5.4 step 3).
_default("PIPELINE_WORKTREE_ROOT", str(PIPELINE_ROOT / ".worktrees"))

# Test seams (only consulted when set).
_default("PIPELINE_FIXTURE_ISSUES", "")
_default("PIPELINE_FIXTURE_PR", "")
_default("PIPELINE_FIX_ATTEMPT", "")
_default("CLASSIFIER_OFFLINE", "")

# Engineer backend (live-path requirement; empty in tests/dry-run).
_default("ENGINEER_BIN", "")

# Authoring engine for the workorder stage (Phase 3: BaseWorkflow is now the
# default). ``baseworkflow`` (default) runs a BaseWorkflow over the visitor-built
# Job Request and folds the authored orchestration_script / work_plan into the
# request; ``visitor`` is the explicit fallback that keeps the historical
# visitor-built Job Request untouched. Read live at call time.
_default("DISPATCH_ENGINE", "baseworkflow")

LEDGER_PY = os.environ.get("LEDGER_PY", str(PIPELINE_ROOT / "src" / "ledger" / "ledger.py"))
PREP_PY = os.environ.get("PREP_PY", str(PIPELINE_ROOT / "src" / "architect" / "prep.py"))


# Read an env var with a fallback; the live source of truth at call time.
env = runtime.env


# ------------------------------- Logging -----------------------------------
# Every line is tagged with the SUBSYSTEM issuing it (mirrors common.sh). The
# subsystem defaults to the invoking program's basename (minus .sh/.py) and is
# overridable with DISPATCH_SUBSYS. The ``[time] subsys: msg`` format mechanism
# lives in engine.runtime.Logger; the subsystem-resolution policy stays here.
def _subsys() -> str:
    s = os.environ.get("DISPATCH_SUBSYS") or os.path.basename(sys.argv[0] or "orchestration")
    for ext in (".sh", ".py"):
        if s.endswith(ext):
            s = s[: -len(ext)]
    return s


_LOG = runtime.Logger(_subsys)


def log(msg: str) -> None:
    _LOG.log(msg)


def warn(msg: str) -> None:
    _LOG.warn(msg)


def err(msg: str) -> None:
    _LOG.err(msg)


class PipelineExit(SystemExit):
    """die() — raise to exit 1 with an ERROR line already emitted."""


def die(msg: str) -> "PipelineExit":
    _LOG.die(msg, exc=PipelineExit)


# --------------------------- Dry-run plumbing ------------------------------
def is_dry_run() -> bool:
    return os.environ.get("PIPELINE_DRY_RUN", "1") != "0"


# A mutating command runs for real only when PIPELINE_DRY_RUN=0; otherwise the
# DryRunner prints the greppable ``DRY-RUN: <tokens>`` line smoke.sh asserts on.
_RUNNER = runtime.DryRunner(
    is_dry_run,
    lambda tokens: proc.run(tokens, capture=False).returncode,
)


def run(*args: str) -> int:
    """Execute a mutating command, or print it (greppable) under dry-run.

    Tokens are joined by a single space so smoke.sh can assert on substrings,
    matching the shell ``echo "DRY-RUN: $*"``. Returns the child exit code
    (0 on the dry-run branch).
    """
    return _RUNNER.run(*args)


def require_tool(name: str) -> None:
    runtime.require_tool(name, die)


def have_tool(name: str) -> bool:
    return runtime.have_tool(name)


# --------------------------- Tick concurrency lock (E1-1) ------------------
# The advisory single-host flock mechanism (presence check, non-blocking vs
# timed acquire, graceful degradation) lives in engine.runtime.file_lock; the
# env-var policy and the exact degraded/contended log wording stay here.
_LockBusy = runtime.LockBusy


def tick_lock():
    """Return a context manager holding an exclusive single-host flock.

    DISPATCH_LOCK_WAIT=0 (default) is non-blocking — a second concurrent tick
    raises _LockBusy so the caller exits 0. A positive N waits up to N seconds.
    If flock/the lock dir is unavailable, degrade to UNLOCKED with a WARN rather
    than aborting the tick.
    """
    flock_bin = os.environ.get("FLOCK_BIN", "flock")
    waitsec = os.environ.get("DISPATCH_LOCK_WAIT", "0")
    wait = int(waitsec) if re.match(r"^[1-9][0-9]*$", waitsec) else 0
    return runtime.file_lock(
        os.environ["DISPATCH_LOCK_FILE"],
        wait=wait,
        flock_bin=flock_bin,
        on_no_flock=lambda b: warn(
            f"{b} not on PATH — running tick UNLOCKED "
            f"(overlapping ticks not serialized)"
        ),
        on_open_fail=lambda p: warn(f"cannot open lock file {p} — running tick UNLOCKED"),
        on_busy=lambda: log("pipeline: another tick holds the lock; exiting 0"),
    )


# --------------------------- Tick run-record helpers (E1-2) ----------------
def _artifacts_dir() -> str:
    return os.environ.get("DISPATCH_ARTIFACTS_DIR", ".dispatch")


def _tick_id() -> str:
    return os.environ.get("DISPATCH_TICK_ID", "tick-unknown")


def _tick_claimed_file() -> str:
    return os.environ.get(
        "DISPATCH_CLAIMED_FILE", f"{_artifacts_dir()}/{_tick_id()}.claimed"
    )


def _tick_reaped_file() -> str:
    return os.environ.get(
        "DISPATCH_REAPED_FILE", f"{_artifacts_dir()}/{_tick_id()}.reaped"
    )


def tick_record_start() -> None:
    """Write the `started` record and (re)initialise this tick's sinks."""
    if not os.environ.get("DISPATCH_TICK_ID"):
        os.environ["DISPATCH_TICK_ID"] = "tick-" + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
    os.environ["DISPATCH_CLAIMED_FILE"] = _tick_claimed_file()
    os.environ["DISPATCH_REAPED_FILE"] = _tick_reaped_file()
    record = os.environ["DISPATCH_RUN_RECORD"]
    for path in (record, os.environ["DISPATCH_CLAIMED_FILE"], os.environ["DISPATCH_REAPED_FILE"]):
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    for sink in (os.environ["DISPATCH_CLAIMED_FILE"], os.environ["DISPATCH_REAPED_FILE"]):
        try:
            Path(sink).write_text("")  # fresh sinks per tick
        except OSError:
            pass
    line = (
        f"event=started tick_id={os.environ['DISPATCH_TICK_ID']} ts={runtime.utc_iso8601()} "
        f"repo={os.environ.get('PIPELINE_REPO', '')} dry_run={os.environ['PIPELINE_DRY_RUN']}\n"
    )
    try:
        with open(record, "a") as fh:
            fh.write(line)
    except OSError:
        pass


def tick_record_claim(num) -> None:
    f = os.environ.get("DISPATCH_CLAIMED_FILE") or _tick_claimed_file()
    if not f:
        return
    try:
        with open(f, "a") as fh:
            fh.write(f"{num}\n")
    except OSError:
        pass


def tick_record_reap(num) -> None:
    f = os.environ.get("DISPATCH_REAPED_FILE") or _tick_reaped_file()
    if not f:
        return
    try:
        with open(f, "a") as fh:
            fh.write(f"{num}\n")
    except OSError:
        pass


def _sink_summary(path: str):
    nums = []
    try:
        if path and os.path.getsize(path) > 0:
            nums = [ln for ln in Path(path).read_text().splitlines() if ln.strip()]
    except OSError:
        pass
    return len(nums), ",".join(nums)


def tick_record_end(status=0) -> None:
    claimed_count, claimed_csv = _sink_summary(
        os.environ.get("DISPATCH_CLAIMED_FILE") or _tick_claimed_file()
    )
    reaped_count, reaped_csv = _sink_summary(
        os.environ.get("DISPATCH_REAPED_FILE") or _tick_reaped_file()
    )
    record = os.environ["DISPATCH_RUN_RECORD"]
    try:
        Path(record).parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    line = (
        f"event=ended tick_id={os.environ.get('DISPATCH_TICK_ID', 'tick-unknown')} "
        f"ts={runtime.utc_iso8601()} status={status} claimed_count={claimed_count} "
        f"claimed={claimed_csv} reaped_count={reaped_count} reaped={reaped_csv}\n"
    )
    try:
        with open(record, "a") as fh:
            fh.write(line)
    except OSError:
        pass


# --------------------------- Crash-reaper helpers (E1-3) -------------------
# Time/epoch parsing is the engine's Clock; what a reaper is and which env var
# overrides "now" is dispatch policy.
def reaper_now_epoch() -> int:
    return runtime.now_epoch(os.environ.get("DISPATCH_NOW_OVERRIDE", ""))


def reaper_epoch_of(v: str):
    """Parse epoch|ISO-8601 to epoch seconds; None on parse failure."""
    return runtime.parse_epoch(v)


def reaper_timeout_hours():
    """env override -> tuning.json recovery.reaper_timeout_hours -> 4."""
    override = os.environ.get("DISPATCH_CLAIM_TIMEOUT_HOURS", "")
    if override:
        return override
    v = proc.run_text(
        [
            os.environ["PYTHON_BIN"],
            "-c",
            "import tuning; v=tuning.RECOVERY_REAPER_TIMEOUT_HOURS; "
            "print(int(v) if float(v).is_integer() else v)",
        ],
        default="",
        env={**os.environ, "PYTHONPATH": str(PIPELINE_ROOT / "src")},
    )
    return v if v else "4"


# ------------------------------ Run-ledger (E4 / #36) ----------------------
def ledger_emit(stage: str, issue: str = "", fields: str = "") -> None:
    """Append one stage-transition line to the JSONL run-ledger via ledger.py.

    NOT gated by is_dry_run — the ledger always records (stamping dry_run:
    true|false), never calls gh, and never fails the tick (errors swallowed).
    """
    if not fields:
        fields = "{}"
    dry = "true" if is_dry_run() else "false"
    # Captured (not inherited) so the ledger stays quiet; never fails the tick —
    # a launch failure surfaces as ProcError, which we swallow.
    try:
        proc.run(
            [
                os.environ["PYTHON_BIN"],
                LEDGER_PY,
                "--stage",
                stage,
                "--issue",
                str(issue),
                "--tick-id",
                os.environ.get("DISPATCH_TICK_ID", "tick-unknown"),
                "--dry-run",
                dry,
                "--fields",
                fields,
                "--quiet",
            ],
        )
    except proc.ProcError:
        pass


# ------------------------- Engine call wrappers ----------------------------
def gh_repo_args() -> list[str]:
    """``--repo owner/repo`` (or empty) for splicing into gh calls."""
    repo = os.environ.get("PIPELINE_REPO", "")
    return ["--repo", repo] if repo else []


def gh_mutate(*args: str) -> int:
    """A gh call that changes GitHub state -> always via run() (dry-run aware).

    REPO_ARGS are appended AFTER the supplied args, matching common.sh so the
    greppable dry-run line has the same token order.
    """
    return run(os.environ["GH_BIN"], *[str(a) for a in args], *gh_repo_args())


# ------------------------- Issue / PR comment formatting -------------------
_INVOICE_HEADER = {
    "completed": "✅ Engineer invoice — completed",
    "partial": "⚠️ Engineer invoice — partial",
    "failed": "❌ Engineer invoice — failed",
    "needs-human": "🔴 Engineer returned `needs-human`",
}


def format_invoice_comment(status: str, summary: str, *, route_used: str = "") -> str:
    """A readable Markdown issue/PR comment for an engineer Invoice.

    The engineer ``summary`` is the model's own (multi-paragraph) rationale; it is
    rendered verbatim on its own lines below a status heading so its structure
    survives — instead of being jammed onto the header line as one newline-less
    wall of text (the old ``**…** {summary}`` form)."""
    header = _INVOICE_HEADER.get(status, f"Engineer invoice — {status}")
    lines = [f"### {header}", ""]
    if route_used:
        lines.append(f"**Route:** `{route_used}`")
        lines.append("")
    lines.append((summary or "").strip() or "_(no summary provided)_")
    return "\n".join(lines)


def format_route_comment(result_json: str) -> str:
    """A readable routing-decision comment. Keeps the machine-findable
    ``<!-- pipeline:route -->`` marker (so the decision stays greppable) but renders
    the payload as a one-line summary instead of a raw JSON dump."""
    marker = "<!-- pipeline:route -->"
    try:
        d = json.loads(result_json)
    except (ValueError, TypeError):
        return f"{marker}\nRouting decision: {result_json}"
    if not isinstance(d, dict):
        return f"{marker}\nRouting decision: {result_json}"
    parts = []
    if d.get("action"):
        parts.append(f"`{d['action']}`")
    if d.get("scope"):
        parts.append(f"scope `{d['scope']}`")
    if d.get("route"):
        parts.append(f"route `{d['route']}`")
    conf = d.get("confidence")
    if conf is not None:
        try:
            parts.append(f"confidence {float(conf):.2f}")
        except (TypeError, ValueError):
            pass
    body = " · ".join(parts) if parts else result_json
    return f"{marker}\n**🧭 Routing decision** — {body}"


def claude_invoke(workflow: str, args_json: str) -> int:
    """Launch a saved dynamic workflow headlessly (dry-run aware)."""
    mode = os.environ.get("CLAUDE_ARGS_MODE", "prompt")
    claude = os.environ["CLAUDE_BIN"]
    if mode == "flag":
        return run(claude, "-p", f"/{workflow}", "--args", args_json)
    return run(claude, "-p", f"Run /{workflow} with args {args_json}")


# ------------------------- Routing / ladder logic --------------------------
def route_for_scope(scope: str) -> str:
    """Classifier scope -> generation model group (§5.2 / §5.3)."""
    if scope in ("xs", "s"):
        return "gen-local"
    if scope == "m":
        return "gen-default"
    if scope == "l":
        return "gen-frontier"
    return "gen-default"


def tier_for_attempt(attempt) -> str:
    """Fix-ladder (§5.5). attempt>3 (or 0/invalid) => needs-human sentinel."""
    return {
        "1": "gen-local",
        "2": "gen-default",
        "3": "gen-frontier",
    }.get(str(attempt), "needs-human")


# ------------------------- Engineer interface -------------------------------
def engineer_dispatch(job_request_json: str) -> int:
    """Submit a Job Request JSON to the configured Engineer (ENGINEER_BIN).

    Dry-run prints the intended call; live calls ENGINEER_BIN (whose stdout is
    the Invoice). The Architect is blind to which Engineer backs the call.
    """
    if is_dry_run():
        return run(os.environ.get("ENGINEER_BIN") or "<ENGINEER_BIN>", job_request_json)
    if not os.environ.get("ENGINEER_BIN"):
        die("ENGINEER_BIN is not set; configure it in pipeline.env")
    return run(os.environ["ENGINEER_BIN"], job_request_json)
