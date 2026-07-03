"""architect_intake.py — process an Invoice returned by the Engineer.

Python port of scripts/architect-intake.sh. Decoupled from the Engineer
identity: any process that returns a valid Invoice JSON triggers this handler.
The status -> action mapping lives in ExecutionVisitor.visit_intake_invoice
(the intake-invoice stage); this module only reads the Invoice (arg or stdin)
and drives that stage.
"""

from __future__ import annotations

import sys

from . import common
from .stages import IntakeInvoiceStage
from .visitors import ExecutionVisitor, TickContext

_visitor = ExecutionVisitor()


def run_invoice(invoice_json: str) -> int:
    ctx = TickContext(invoice_json=invoice_json)
    IntakeInvoiceStage().accept(_visitor, ctx)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0]:
        invoice_json = argv[0]
    else:
        invoice_json = sys.stdin.read()
    return run_invoice(invoice_json)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except common.PipelineExit as exc:
        sys.exit(exc.code)
