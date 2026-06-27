"""bootstrap_labels.py — create the pipeline label vocabulary (HANDOFF §5.6).

Python port of scripts/bootstrap-labels.sh. Owns label *existence* only
(create/upsert via ``gh label create --force``); never applies labels to
issues. Idempotent; dry-run prints intended gh calls and mutates nothing.
"""

from __future__ import annotations

import os
import sys

from . import common

from . import statemachine


def main(argv=None) -> int:
    if not common.is_dry_run():
        common.require_tool(os.environ["GH_BIN"])
    count = 0
    for name in statemachine.all_pipeline_labels():
        if not name:
            continue
        common.gh_mutate(
            "label", "create", name,
            "--color", statemachine.label_color(name),
            "--description", statemachine.label_desc(name),
            "--force",
        )
        count += 1
    mode = "dry-run" if common.is_dry_run() else "applied"
    common.log(f"bootstrap-labels: {count} labels ensured ({mode}).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except common.PipelineExit as exc:
        sys.exit(exc.code)
