"""stages.py — the six canonical pipeline stages as visitor *elements*.

The former bash kept the stage vocabulary as a string array
(``DISPATCH_STAGES=(intake workorder prep engineer intake-invoice closure)``)
and ``stage_ord`` as a linear scan. Here each stage is a first-class element
that ``accept``s a :class:`~src.orchestration.visitors.StageVisitor`,
dispatching to the visitor's per-stage method. The canonical order — and thus
``stage_ord`` — is the order of :data:`PIPELINE_STAGES`.

Keeping the ordinal derived from this list (never hard-coded) is the same
invariant the shell guarded: inserting ``prep`` between ``workorder`` and
``engineer`` shifts every downstream gate automatically.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Stage(ABC):
    """A pipeline stage. ``accept`` double-dispatches to the visitor."""

    #: canonical stage name, as it appears on the --until / --from CLI.
    name: str = ""

    @abstractmethod
    def accept(self, visitor, ctx):
        """Dispatch to ``visitor.visit_<stage>(self, ctx)``."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Stage {self.name}>"


class IntakeStage(Stage):
    name = "intake"

    def accept(self, visitor, ctx):
        return visitor.visit_intake(self, ctx)


class WorkorderStage(Stage):
    name = "workorder"

    def accept(self, visitor, ctx):
        return visitor.visit_workorder(self, ctx)


class PrepStage(Stage):
    name = "prep"

    def accept(self, visitor, ctx):
        return visitor.visit_prep(self, ctx)


class EngineerStage(Stage):
    name = "engineer"

    def accept(self, visitor, ctx):
        return visitor.visit_engineer(self, ctx)


class IntakeInvoiceStage(Stage):
    name = "intake-invoice"

    def accept(self, visitor, ctx):
        return visitor.visit_intake_invoice(self, ctx)


class ClosureStage(Stage):
    name = "closure"

    def accept(self, visitor, ctx):
        return visitor.visit_closure(self, ctx)


# The canonical tick, in execution order (was DISPATCH_STAGES in common.sh).
PIPELINE_STAGES: list[Stage] = [
    IntakeStage(),
    WorkorderStage(),
    PrepStage(),
    EngineerStage(),
    IntakeInvoiceStage(),
    ClosureStage(),
]

STAGE_NAMES: list[str] = [s.name for s in PIPELINE_STAGES]


def stage_ord(name: str):
    """1-based ordinal of ``name`` in the canonical list, or ``None``.

    Mirrors common.sh ``stage_ord``: success returns the position, an unknown
    name returns ``None`` (the shell returned non-zero with no output).
    """
    try:
        return STAGE_NAMES.index(name) + 1
    except ValueError:
        return None


def stage_by_name(name: str):
    for stage in PIPELINE_STAGES:
        if stage.name == name:
            return stage
    return None
