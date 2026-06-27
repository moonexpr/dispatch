"""classifier.py — fluent client for the triage classifier (HANDOFF §5.3).

A thin builder around ``src/classifier/classify.py`` so the dispatch loop
reads as intent, not subprocess plumbing::

    verdict = (Classifier()
               .for_issue(num)
               .title(title)
               .body(body)
               .classify())
    verdict.action, verdict.scope, verdict.route, verdict.confidence
    verdict.raw   # canonical JSON; recorded durably as the routing marker

SECURITY (HANDOFF §8): classify.py is the pipeline's quarantine reader and has
no exec/tool capability. This client only *invokes* it as a subprocess with the
issue text passed as argv data — it never interprets that text itself.
"""
from __future__ import annotations

import json
import os

from engine import proc

from . import common

CLASSIFIER = str(common.PIPELINE_ROOT / "src" / "classifier" / "classify.py")


class Verdict:
    """A parsed TriageResult plus the raw JSON the classifier emitted.

    ``raw`` is the byte-stable JSON dispatch records as the durable routing
    marker; the named fields are the same values pre-unpacked for the caller.
    """

    __slots__ = ("raw", "action", "scope", "route", "confidence")

    def __init__(self, raw: str, data: dict) -> None:
        self.raw = raw
        self.action = data.get("action")
        self.scope = data.get("scope")
        self.route = data.get("route")
        self.confidence = data.get("confidence")


class Classifier:
    """Fluent builder that invokes classify.py and returns a :class:`Verdict`.

    Each setter returns ``self`` so calls chain; :meth:`classify` runs the
    quarantine reader once and parses its output.
    """

    def __init__(self) -> None:
        self._title = ""
        self._body = ""
        self._num = "?"

    def for_issue(self, num) -> "Classifier":
        """Issue number — used only in the failure message for traceability."""
        self._num = num
        return self

    def title(self, title) -> "Classifier":
        self._title = title or ""
        return self

    def body(self, body) -> "Classifier":
        self._body = body or ""
        return self

    def classify(self) -> Verdict:
        """Run the classifier; die on a non-zero exit (mirrors §5.1 behaviour)."""
        raw = proc.run_checked(
            [os.environ["PYTHON_BIN"], CLASSIFIER,
             "--title", self._title, "--body", self._body],
            fail_msg=f"classifier failed for #{self._num} (see stderr)",
            on_fail=common.die,
        ).strip()
        return Verdict(raw, json.loads(raw))
