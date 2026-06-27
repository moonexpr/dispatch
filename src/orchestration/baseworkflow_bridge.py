"""baseworkflow_bridge.py — the live-tick seam onto the BaseWorkflow engine.

Phase 2 of wiring the dispatch live tick onto ``src/baseworkflow``. This module
is an **additive, feature-flagged, fail-safe** seam: when ``DISPATCH_ENGINE`` is
set to ``baseworkflow`` (default is ``visitor``), :func:`author_via_baseworkflow`
runs a :class:`~baseworkflow.BaseWorkflow` over the Job Request the workorder
stage just built, and folds the workflow's authored ``orchestration_script`` and
``work_plan`` deliverables back into the Job Request dict so the downstream
``engineer`` stage (``engineer_sdk.py``) can consume the Architect decomposition.

Design rules (binding, per the Phase-2 contract):

  * **Additive.** The default ``visitor`` path is byte-for-byte unchanged — this
    module is only reached when ``DISPATCH_ENGINE == "baseworkflow"``.
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
from typing import Any, Dict

from . import common


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


def author_via_baseworkflow(job_request_json: str) -> str:
    """Enrich a Job Request via a BaseWorkflow run; fail-safe to the original.

    Parses ``job_request_json`` (the compact JSON the workorder stage produced),
    runs a ``BaseWorkflow`` over it (dry-run gated by ``PIPELINE_DRY_RUN``), and
    merges the authored ``orchestration_script`` and ``work_plan`` deliverables
    into the request dict. Returns the enriched request as compact JSON.

    On ANY failure the ORIGINAL ``job_request_json`` is returned unchanged and a
    diagnostic is written to stderr — the seam never breaks the tick.
    """
    try:
        req = json.loads(job_request_json)
        if not isinstance(req, dict):
            raise ValueError("job request is not a JSON object")

        # Import lazily so the default (visitor) path never pays the import cost
        # and a broken baseworkflow tree can't break module import of the tick.
        # ``baseworkflow`` itself does ``import bindings`` (the package under
        # src/baseworkflow/), so that dir must be on sys.path — mirror what the
        # roundabout demo does.
        import os as _os

        _bw_dir = _os.path.join(str(common.PIPELINE_ROOT), "src", "baseworkflow")
        if _bw_dir not in sys.path:
            sys.path.insert(0, _bw_dir)
        import baseworkflow as bw  # noqa: WPS433 (intentional local import)

        job = _job_from_request(req)
        triage = _triage_from_request(req)
        summary = bw.run_live(job, triage, dry_run=common.is_dry_run())
        deliv = summary.get("deliverables") or {}

        enriched = dict(req)
        for key in ("orchestration_script", "work_plan"):
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
            f"baseworkflow-bridge: authoring failed for issue #{issue or '?'} "
            f"({type(exc).__name__}: {exc}); returning original job request "
            f"unchanged (fail-safe)",
            file=sys.stderr,
        )
        return job_request_json
