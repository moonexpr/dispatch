"""pipeline.py — kernel entrypoint for one full pipeline tick.

Python port of scripts/pipeline.sh. Wires
[bootstrap] -> dispatch (select + claim) -> engineer -> intake, each component
still independently runnable. The driver walks the canonical stage list,
honouring the --until halt-gate and the --from/--artifact replay; the per-issue
stage work lives in the shared ExecutionVisitor.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from . import architect_intake, bootstrap_labels, dispatch
from engine import proc

from . import common
from .stages import STAGE_NAMES, stage_ord
from .visitors import ExecutionVisitor, TickContext

SCRIPTS_DIR = common.PIPELINE_ROOT / "scripts"

# Stage ordinals, looked up ONCE from the canonical list — never hard-coded —
# so inserting a stage never silently shifts the halt-after gates below.
_ENG_ORD = stage_ord("engineer")
_PREP_ORD = stage_ord("prep")

_visitor = ExecutionVisitor()

USAGE = """usage: pipeline.sh [OPTIONS]

  -b, --bootstrap         provision pipeline labels on PIPELINE_REPO first
  -r, --repo  owner/repo  target repo; sets PIPELINE_REPO
  -l, --live              PIPELINE_DRY_RUN=0 (default: dry-run, no mutations)
  -e, --engineer  BIN     Engineer binary (default: scripts/mock-engineer.sh)
  -f, --fixture   FILE    issue list JSON for offline testing
  -u, --until     STAGE   halt the tick AFTER <stage> completes, exit 0, and
                          leave the dumped artifacts on disk for inspection.
                          Valid stages (in order):
                            intake workorder prep engineer intake-invoice closure
                          (closure == the full tick == no flag.)
      --from      STAGE   resume the tick AT <stage>, injecting --artifact as
                          that stage's input and SKIPPING every earlier stage
                          (no intake, no re-classification). Requires --artifact.
                          Valid stages: prep | engineer | intake-invoice | closure.
                          Composes with --until (which caps the forward run).
  -a, --artifact  FILE    captured artifact fed to the --from stage: a Job
                          Request (schemas/job-request.json) for `engineer`; an
                          Invoice (schemas/invoice.json) for `intake-invoice` /
                          `closure`. Validated against its schema as DATA before
                          the stage runs; never executed or eval'd.
  -h, --help              show this help

Stages (the canonical tick, in execution order):
  intake          read & rank queued issues, claim the next one off GitHub
  workorder       turn the claimed issue into a structured work order
  prep            provision the work plan's declared harness before issuing
  engineer        run the Engineer binary; emit a Job Request -> Invoice
  intake-invoice  hand the Invoice to architect-intake for verification
  closure         apply the final label/ledger transition (== the full tick)

Examples:
  # Dry-run against a target repo using the mock engineer:
  bash scripts/pipeline.sh --repo owner/my-repo

  # Provision labels, then run live with the mock engineer:
  bash scripts/pipeline.sh --bootstrap --repo owner/my-repo --live

  # Live run with Ruflo as the Engineer:
  bash scripts/pipeline.sh --repo owner/my-repo --live --engineer ruflo

  # Fully offline smoke test:
  bash scripts/pipeline.sh --fixture scripts/fixtures/queued-issues.json

  # Replay exactly the engineer stage on a captured Job Request:
  bash scripts/pipeline.sh --from engineer \\
       --artifact .dispatch/artifacts/tick-XXXX/job-request.json --until engineer
"""


def _usage() -> "common.PipelineExit":
    print(USAGE, file=sys.stderr, end="")
    raise common.PipelineExit(0)


def _validate_artifact(file: str, schema: str) -> bool:
    """Guard a replay --artifact (E2-3/#32): exists, JSON object, required keys."""
    label = os.path.basename(schema)
    if not os.path.isfile(schema):
        common.err(f"internal: schema not found: {schema}")
        return False
    if not os.path.isfile(file):
        common.err(f"--artifact file not found: {file}")
        return False
    try:
        data = json.loads(open(file).read())
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        common.err(f"--artifact is not valid JSON (object expected): {file}")
        return False
    try:
        required = json.loads(open(schema).read()).get("required", []) or []
    except (ValueError, TypeError):
        required = []
    missing = " ".join(k for k in required if k not in data)
    if missing:
        common.err(
            f"--artifact {file} is not schema-valid ({label}): "
            f"missing required field(s): {missing}"
        )
        return False
    return True


def _run_engineer(engineer: str, job_json: str) -> str:
    result = proc.run([engineer, job_json], forward_stderr=True)
    return result.stdout.rstrip("\n")


def _pipeline_resume(stage: str, artifact: str, engineer: str) -> int:
    artifact_json = open(artifact).read()
    td = None
    if os.environ.get("DISPATCH_ARTIFACTS_DIR"):
        td = Path(os.environ["DISPATCH_ARTIFACTS_DIR"]) / os.environ["DISPATCH_TICK_ID"]
        td.mkdir(parents=True, exist_ok=True)
    common.log(f"pipeline: resume from stage: {stage} (artifact: {artifact})")
    until_ord = os.environ.get("DISPATCH_UNTIL_ORD")

    if stage == "prep":
        ctx = TickContext(job_request=artifact_json)
        from .stages import PrepStage

        if not PrepStage().accept(_visitor, ctx):
            common.err(
                "pipeline: prep blocked issuance for the captured job request — halting"
            )
            return 1
        if until_ord and int(until_ord) <= _PREP_ORD:
            common.log(f"pipeline: halted after stage: {os.environ['DISPATCH_UNTIL_STAGE']}")
            return 0
        prep_invoice = _run_engineer(engineer, artifact_json)
        if td:
            (td / "invoice.json").write_text(prep_invoice + "\n")
        if until_ord and int(until_ord) <= _ENG_ORD:
            common.log(f"pipeline: halted after stage: {os.environ['DISPATCH_UNTIL_STAGE']}")
            return 0
        architect_intake.run_invoice(prep_invoice)
    elif stage == "engineer":
        invoice = _run_engineer(engineer, artifact_json)
        if td:
            (td / "invoice.json").write_text(invoice + "\n")
        if until_ord and int(until_ord) <= _ENG_ORD:
            common.log(f"pipeline: halted after stage: {os.environ['DISPATCH_UNTIL_STAGE']}")
            return 0
        architect_intake.run_invoice(invoice)
    elif stage in ("intake-invoice", "closure"):
        architect_intake.run_invoice(artifact_json)

    common.log("pipeline: done.")
    return 0


def _make_bridge(engineer: str) -> str:
    """Write the engineer->intake bridge (a generated bash launcher).

    dispatch invokes ENGINEER_BIN (this bridge) for each claimed issue; the
    bridge calls the real engineer, taps the Invoice into the tick dir, honours
    the --until engineer gate, then hands the Invoice to architect-intake.
    """
    fd, path = tempfile.mkstemp(prefix="pipeline-bridge.", suffix=".sh", dir="/tmp")
    os.close(fd)
    pipeline_bin = common.PIPELINE_ROOT / "pipeline"
    bridge = f"""#!/usr/bin/env bash
set -euo pipefail
invoice="$("{engineer}" "$1")"
if [[ -n "${{DISPATCH_ARTIFACTS_DIR:-}}" ]]; then
  _td="${{DISPATCH_ARTIFACTS_DIR}}/${{DISPATCH_TICK_ID:-tick-unknown}}"
  mkdir -p "${{_td}}"
  printf '%s\\n' "${{invoice}}" > "${{_td}}/invoice.json"
fi
if [[ -n "${{DISPATCH_UNTIL_ORD:-}}" && "${{DISPATCH_UNTIL_ORD}}" -le {_ENG_ORD} ]]; then
  exit 0
fi
"{pipeline_bin}" devtools intake-invoice "${{invoice}}"
"""
    Path(path).write_text(bridge)
    os.chmod(path, 0o755)
    return path


def _pipeline_tick() -> int:
    common.tick_record_start()
    # Test seam: simulate a tick that crashes right after starting.
    if os.environ.get("DISPATCH_CRASH_AFTER_START_TEST"):
        common.die("injected post-start crash (DISPATCH_CRASH_AFTER_START_TEST test seam)")
    common.log("pipeline: dispatch starting")
    rc = 0
    try:
        rc = dispatch.main([])
    except common.PipelineExit as exc:
        rc = exc.code
    except SystemExit as exc:  # pragma: no cover
        rc = exc.code or 0
    common.tick_record_end(rc)
    until_stage = os.environ.get("DISPATCH_UNTIL_STAGE")
    if until_stage and until_stage != "closure":
        common.log(
            f"pipeline: halted after stage: {until_stage} "
            f"(artifacts: {os.environ.get('DISPATCH_ARTIFACTS_DIR', '<unset>')})"
        )
    common.log("pipeline: done.")
    return rc


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    bootstrap = False
    engineer = os.environ.get("ENGINEER_BIN") or str(SCRIPTS_DIR / "mock-engineer.sh")
    until = ""
    from_stage = ""
    artifact = ""

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-b", "--bootstrap"):
            bootstrap = True; i += 1
        elif a in ("-r", "--repo"):
            os.environ["PIPELINE_REPO"] = argv[i + 1]; i += 2
        elif a in ("-l", "--live"):
            os.environ["PIPELINE_DRY_RUN"] = "0"; i += 1
        elif a in ("-e", "--engineer"):
            engineer = argv[i + 1]; i += 2
        elif a in ("-f", "--fixture"):
            os.environ["PIPELINE_FIXTURE_ISSUES"] = argv[i + 1]; i += 2
        elif a in ("-u", "--until"):
            until = argv[i + 1]; i += 2
        elif a == "--from":
            from_stage = argv[i + 1]; i += 2
        elif a in ("-a", "--artifact"):
            artifact = argv[i + 1]; i += 2
        elif a in ("-h", "--help"):
            _usage()
        else:
            common.die(f"unknown flag: {a} (try --help)")

    # Validate --until against the canonical stage vocabulary.
    until_ord = None
    if until:
        until_ord = stage_ord(until)
        if until_ord is None:
            common.err(f"unknown --until stage: {until}")
            common.err(f"valid stages (in order): {' '.join(STAGE_NAMES)}")
            return 2
        os.environ["DISPATCH_UNTIL_STAGE"] = until
        os.environ["DISPATCH_UNTIL_ORD"] = str(until_ord)

    # Validate --from/--artifact (E2-3/#32).
    if from_stage:
        if from_stage in ("prep", "engineer"):
            from_schema = str(SCRIPTS_DIR.parent / "schemas" / "job-request.json")
        elif from_stage in ("intake-invoice", "closure"):
            from_schema = str(SCRIPTS_DIR.parent / "schemas" / "invoice.json")
        else:
            common.err(f"unknown --from stage: {from_stage}")
            common.err("valid --from stages: prep engineer intake-invoice closure")
            return 2
        if not artifact:
            common.err(f"--from {from_stage} requires --artifact <file>")
            return 2
        if not _validate_artifact(artifact, from_schema):
            return 2
        from_ord = stage_ord(from_stage)
        if until and until_ord < from_ord:
            common.err(
                f"--until {until} (stage {until_ord}) precedes --from {from_stage} "
                f"(stage {from_ord}); nothing to run"
            )
            return 2
    elif artifact:
        common.err("--artifact given without --from <stage>")
        return 2

    # Resolve relative engineer paths to absolute so the bridge works from any cwd.
    if not engineer.startswith("/") and os.path.isfile(engineer):
        engineer = str(Path(engineer).resolve())

    repo_disp = os.environ.get("PIPELINE_REPO") or "<gh default>"
    common.log(
        f"pipeline: repo={repo_disp}  dry_run={os.environ['PIPELINE_DRY_RUN']}  "
        f"engineer={os.path.basename(engineer)}"
    )

    # One stable tick id per tick, shared by every stage's artifact dump (E2-1).
    if not os.environ.get("DISPATCH_TICK_ID"):
        from datetime import datetime, timezone

        os.environ["DISPATCH_TICK_ID"] = "tick-" + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )

    # Stage 0 — bootstrap labels (optional, idempotent).
    if bootstrap:
        common.log(f"pipeline: provisioning labels on {repo_disp}")
        bootstrap_labels.main([])

    # E2-3 — resume/replay a single stage from a captured artifact. The shell
    # relied on `set -e` to abort with the resume's non-zero rc (e.g. a prep
    # fail-safe block) before its trailing `exit 0`; propagate it explicitly.
    if from_stage:
        return _pipeline_resume(from_stage, artifact, engineer)

    # Stage 1+2+3 — dispatch -> engineer -> intake, wired through the bridge.
    bridge = _make_bridge(engineer)
    os.environ["ENGINEER_BIN"] = bridge
    try:
        try:
            with common.tick_lock():
                return _pipeline_tick()
        except common._LockBusy:
            return 0
    finally:
        try:
            os.unlink(bridge)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except common.PipelineExit as exc:
        sys.exit(exc.code)
