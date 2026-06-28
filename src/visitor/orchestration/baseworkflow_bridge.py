"""baseworkflow_bridge.py — the live-tick seam onto the workflow engine.

Wires the dispatch live tick onto a workflow engine selected by ``DISPATCH_ENGINE``:
``baseworkflow`` (default, ``src/baseworkflow``) or ``websitewf`` (the web-development
overlay engine, ``src/websitewf`` — BaseWorkflow + the ``websitewf.yml`` overlay). On
every tick :func:`author_via_workflow` runs the chosen workflow's ``run_live`` over the
Job Request the workorder stage just built, and folds the authored
``orchestration_script`` / ``work_plan`` / ``plan`` deliverables back into the Job
Request dict so the downstream ``engineer`` stage (``engineer_sdk.py``) can consume the
Architect decomposition.

The historical ``DISPATCH_ENGINE=visitor`` authoring fallback (which left the Job
Request unchanged) is DEPRECATED and DISABLED for release — it is no longer
selectable; see ``visitors.py`` ``visit_workorder``.

Design rules (binding):

  * **Dry-run honoured.** ``BaseWorkflow`` is run via ``run_live(..., dry_run=…)``
    where the dry-run flag is read from ``PIPELINE_DRY_RUN`` (the pipeline's
    single source of truth via ``common.is_dry_run``). Under dry-run the
    ``RealActionFactory`` inference runner returns a deterministic placeholder and
    makes **no** model/network call.
  * **Fail-safe.** Any exception — a malformed request, a workflow validation
    error, anything — is logged to stderr and the ORIGINAL Job Request is
    returned unchanged. The seam must NEVER break the tick.

Nothing else in the pipeline changes: prep / engineer / closure consume the
(possibly enriched) Job Request exactly as before.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

# ``common`` (the dry-run-aware gh wrapper + run-ledger) was lifted out of this
# retired visitor lineage into src/baseworkflow/subsystems/ so the workflow engine
# owns its logic. This bridge is itself dead-lineage glue (only the deprecated
# visitor tick + tests reach it); it resolves ``common`` from the new home via the
# same bare-name idiom it uses for ``import baseworkflow`` below.
_SUBSYS = str(Path(__file__).resolve().parents[3] / "src" / "baseworkflow" / "subsystems")
if _SUBSYS not in sys.path:
    sys.path.insert(0, _SUBSYS)
import common  # noqa: E402  src/baseworkflow/subsystems/common.py


def _triage_from_request(req: Dict[str, Any]) -> Dict[str, Any]:
    """Build the BaseWorkflow ``triage`` dict from a Job Request.

    The Job Request carries ``route``/``scope``/``confidence`` (the classifier
    verdict the workorder stage folded in) but not ``action`` — only issues the
    classifier marked ``action == "implement"`` ever reach the workorder stage
    (``dispatch.py`` routes ``decompose``/other actions away before claiming), so
    ``"implement"`` is the correct, grounded default here.
    """
    return {
        "action": req.get("action") or "implement",
        "scope": req.get("scope") or "m",
        "route": req.get("route") or "gen-default",
        "confidence": req.get("confidence"),
    }


def _job_from_request(req: Dict[str, Any]) -> Dict[str, Any]:
    """Build the BaseWorkflow ``job`` dict from a Job Request.

    Shapes match the roundabout demo: ``{issue, title, body, labels}``. The Job
    Request has no ``labels`` field of its own, so labels default to ``[]``.
    """
    labels = req.get("labels")
    if isinstance(labels, str):
        labels = [s for s in (p.strip() for p in labels.split(",")) if s]
    elif not isinstance(labels, list):
        labels = []
    return {
        "issue": req.get("issue"),
        "title": req.get("title") or "",
        "body": req.get("body") or "",
        "labels": labels,
    }


def _workflow_module(engine: str) -> Any:
    """Import and return the workflow module for ``engine`` (``run_live`` lives on
    it). ``websitewf`` -> ``src/websitewf/websitewf.py``; anything else ->
    baseworkflow. Both self-manage their own ``sys.path`` for ``import bindings``."""
    import os as _os

    root = str(common.PIPELINE_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    if engine == "websitewf":
        from src.websitewf import websitewf as wf_mod  # noqa: WPS433

        return wf_mod
    bw_dir = _os.path.join(root, "src", "baseworkflow")
    if bw_dir not in sys.path:
        sys.path.insert(0, bw_dir)
    import baseworkflow as wf_mod  # noqa: WPS433 (intentional local import)

    return wf_mod


def author_via_workflow(job_request_json: str, engine: str = "baseworkflow") -> str:
    """Enrich a Job Request via a workflow run; fail-safe to the original.

    Parses ``job_request_json`` (the compact JSON the workorder stage produced),
    runs the ``engine`` workflow's ``run_live`` over it (dry-run gated by
    ``PIPELINE_DRY_RUN``), and merges the authored ``orchestration_script``,
    ``work_plan`` and ``plan`` deliverables into the request dict. Returns the
    enriched request as compact JSON. ``engine`` is ``baseworkflow`` (default) or
    ``websitewf``.

    The ``plan`` deliverable (``decompose.plan()`` output:
    ``{"units": [...], "staffing": {...}, "criteria": [...]}``) is what unlocks
    multi-agent fan-out downstream: the SDK Engineer's ``_extract_units`` already
    recognises ``job["plan"]["units"]`` as a decomposition carrier and
    materialises one engineering agent per unit. Without it the Engineer sees no
    units and falls back to a single agent — even though the Architect authored a
    multi-unit decomposition (the orchestration_script ``phases`` carry the same
    units in flattened form, which the Engineer does NOT read). Folding ``plan``
    is the seam that makes the authored staffing actually staff agents.

    On ANY failure the ORIGINAL ``job_request_json`` is returned unchanged and a
    diagnostic is written to stderr — the seam never breaks the tick.
    """
    try:
        req = json.loads(job_request_json)
        if not isinstance(req, dict):
            raise ValueError("job request is not a JSON object")

        # Import lazily so a broken workflow tree can't break module import of the
        # tick, and only the selected engine pays the import cost.
        wf_mod = _workflow_module(engine)

        job = _job_from_request(req)
        triage = _triage_from_request(req)
        summary = wf_mod.run_live(job, triage, dry_run=common.is_dry_run())
        deliv = summary.get("deliverables") or {}

        enriched = dict(req)
        # ``plan`` carries the structured units/staffing the Engineer fans out on
        # (job["plan"]["units"]); orchestration_script/work_plan are the authored
        # program + plan record. Fold all three when the workflow produced them.
        for key in ("orchestration_script", "work_plan", "plan"):
            if key in deliv and deliv[key] is not None:
                enriched[key] = deliv[key]

        return json.dumps(enriched, separators=(",", ":"))
    except Exception as exc:  # noqa: BLE001 — fail-safe is the whole point.
        issue = ""
        try:
            issue = str(json.loads(job_request_json).get("issue", ""))
        except Exception:  # noqa: BLE001
            pass
        print(
            f"workflow-bridge[{engine}]: authoring failed for issue #{issue or '?'} "
            f"({type(exc).__name__}: {exc}); returning original job request "
            f"unchanged (fail-safe)",
            file=sys.stderr,
        )
        return job_request_json


def author_via_baseworkflow(job_request_json: str) -> str:
    """Back-compat shim: author via the default ``baseworkflow`` engine. New callers
    should use :func:`author_via_workflow` and pass the engine explicitly."""
    return author_via_workflow(job_request_json, "baseworkflow")
