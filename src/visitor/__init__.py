"""src.visitor — the legacy visitor-pattern lineage, consolidated.

This package collects everything that is **not** part of the canonical
workflow engine, so the two lineages read as two distinct camps in the source
tree:

  * **Workflow engine (canonical).** Top-level ``engine/`` (the generic
    Controller/Action workflow runtime) plus ``src/baseworkflow/`` (the dispatch
    BaseWorkflow + its bindings). This is the execution layer PROJECT.md
    documents; it is self-contained against ``engine/`` and imports nothing from
    this package.

  * **Visitor lineage (here).** The ``StageVisitor`` / ``ExecutionVisitor``
    stage-walk tick (``orchestration/``) and the standalone, non-workflow
    subsystems it grew up with:

        orchestration/   the visitor-pattern tick driver (visitors, stages,
                         statemachine, dispatch, pipeline, closure, seam, ...)
        architect/       work-order authoring helpers
        budget/          budget guard / oracle / reconcile
        classifier/      issue classification
        intake/          gh intake + ranking
        ledger/          run-ledger
        reports/         reporting

Consolidated here for organisational clarity only. This move is **structural,
not operational**: imports that still reference the old ``src.<pkg>`` /
``src.orchestration`` paths are intentionally left untouched and are expected to
need rewiring before this lineage runs again. ``src/tuning.py`` stays at the
top of ``src/`` because it is a shared tuning surface used by both the workflow
engine and this lineage.
"""
