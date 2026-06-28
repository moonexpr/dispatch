#!/usr/bin/env python3
"""decompose.py — ARCHITECT work decomposition + staffing (deterministic).

Splits a job into UNITS OF WORK — one cohesive, independently-verifiable
deliverable per unit, owned end-to-end by a single specialist — and staffs them.
Each specialist is named as `<domain> <function>` (operator's taxonomy):
the function is the role noun (engineer, writer, designer, analyst…) and the
domain qualifies it (developer tooling, data-pipeline, QA / test automation…).

Agent count = number of units, capped at the swarm maximum. Units with no
unmet dependency run in parallel; the integration/verification unit runs last.
Offline + deterministic: identical inputs -> identical plan.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tuning  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # architect dir (verify)
import verify  # noqa: E402

# Hard cap on agents (= number of units). Tunable via app/config/tuning.yml
# (generation.decompose.swarm_max).
_SWARM_MAX = tuning.SWARM_MAX

# Complexity-aware staffing knobs (#134) — all thresholds/counts live in config
# (generation.decompose.complexity); this module only matches + renders.
_CX = tuning.DECOMPOSE_COMPLEXITY

# Work-order UNIT TEXT templates (#144) — the template STRINGS live in config
# (generation.decompose.templates); this module COMPUTES the fields
# (issue/path/gate/focus/suffix) and fills them via str.format. A missing config
# falls back to DEFAULTS (byte-identical text).
_TPL = tuning.DECOMPOSE_TEMPLATES


def _spec_for(path: str):
    """Map a file/area to a (function, domain) specialization.

    The ordered match rules live in app/config/tuning.yml
    (generation.decompose.specialization_rules) and are evaluated by
    tuning.spec_for — edit the config to retune staffing labels.
    """
    return tuning.spec_for(path)


def _spec(function: str, domain: str) -> Dict[str, str]:
    return {"function": function, "domain": domain, "label": f"{domain} {function}"}


# A heading line ("## Acceptance criteria", "**Success criteria**", "## Definition
# of Done", etc.) and a bullet line ("- ...", "* ...", "1. ..."). Used to lift the
# issue's own criteria into the work order so DONE CRITERIA is concrete, not a
# generic placeholder. Acceptance/Success Criteria takes precedence over a
# Definition of Done section when both are present.
_CRIT_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*\s*)?(?:acceptance|success)\s+criteria\b", re.IGNORECASE)
_DOD_HEADING = re.compile(
    r"^\s*(?:#{1,6}\s*|\*\*\s*)?definition\s+of\s+done\b", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_MD = re.compile(r"[*_`]+")


def _capture_under(body: str, heading: "re.Pattern[str]", limit: int) -> List[str]:
    """Bullet items under the first heading matching ``heading``; ``[]`` if none.

    Stops at the next Markdown heading or the first blank line after the items.
    Markdown emphasis is stripped and internal whitespace/newlines collapse to a
    single space, so no extracted line can break the ``- [ ]`` framing downstream
    (issue text is parsed as untrusted data only)."""
    out: List[str] = []
    capturing = False
    for line in body.splitlines():
        if heading.search(line):
            capturing = True
            continue
        if not capturing:
            continue
        if re.match(r"^\s*#{1,6}\s+\S", line):       # next heading ends the section
            break
        m = _BULLET.match(line)
        if m:
            item = " ".join(_MD.sub("", m.group(1)).split())
            if item:
                out.append(item)
        elif line.strip() == "" and out:             # blank line after items ends it
            break
    return out[:limit]


def extract_criteria(body: str, *, limit: int = 12) -> List[str]:
    """Return the bullet items under the issue body's own acceptance section, with
    markdown stripped. Prefers an 'Acceptance criteria' / 'Success criteria'
    heading; falls back to a 'Definition of Done' heading. ``[]`` when neither is
    present. Issue text is parsed as data only."""
    if not body:
        return []
    items = _capture_under(body, _CRIT_HEADING, limit)
    if not items:
        items = _capture_under(body, _DOD_HEADING, limit)
    return items


def _label_names(job: Dict[str, Any]) -> List[str]:
    """Lowercased label names off the job. Labels may be plain strings or GitHub
    ``{"name": ...}`` dicts; absent entirely on some callers. Issue/label text is
    untrusted data only — used here purely as a deterministic signal."""
    out: List[str] = []
    for lb in (job.get("labels") or []):
        name = lb.get("name") if isinstance(lb, dict) else lb
        if name:
            out.append(str(name).lower())
    return out


def _complexity(job: Dict[str, Any]) -> Dict[str, Any]:
    """Derive a deterministic implementation-slice count + diagnosis flag from
    signals ALREADY on the item (scope, labels, body length) — never from new
    intake data (#132/#133 are separate, blocked). All thresholds come from
    config (generation.decompose.complexity), so this stays a matcher only.

    Returns {slices, diagnosis, signals} where ``slices`` is the number of
    implementation slices (>=1) and ``diagnosis`` requests a distinct diagnosis
    phase. xs/s/m scope with no label/body signal yields slices==1 — today's
    plan — so small/trivial issues are unchanged."""
    scope = str(job.get("scope") or "m").lower()
    labels = _label_names(job)
    body = job.get("body") or ""

    scope_slices = _CX.get("scope_slices", {}) or {}
    base = int(scope_slices.get(scope, scope_slices.get("m", 1)) or 1)

    # Label bonus: summed over each configured signal whose substring appears in
    # ANY label name (deterministic, order-independent).
    label_bonus = 0
    matched: List[str] = []
    for sig, bonus in (_CX.get("label_slice_bonus", {}) or {}).items():
        if any(str(sig).lower() in name for name in labels):
            label_bonus += int(bonus)
            matched.append(str(sig))

    # Deep-bug labels add a diagnostic slice and request the diagnosis phase.
    deep_bug = any(
        any(str(s).lower() in name for name in labels)
        for s in (_CX.get("deep_bug_labels", []) or [])
    )
    bug_bonus = 1 if deep_bug else 0

    # Thread-depth proxy: a long, detailed body implies more independent work.
    per = int(_CX.get("thread_depth_chars_per_slice", 0) or 0)
    depth_bonus = 0
    if per > 0:
        depth_bonus = min(len(body) // per, int(_CX.get("thread_depth_max_bonus", 0) or 0))

    slices = base + label_bonus + bug_bonus + depth_bonus
    slices = max(1, min(slices, _SWARM_MAX))

    diag_min = int(_CX.get("diagnosis_min_slices", 0) or 0)
    diagnosis = deep_bug or (diag_min > 0 and slices >= diag_min)
    return {
        "slices": slices,
        "diagnosis": diagnosis,
        "signals": {"scope": scope, "labels": matched, "deep_bug": deep_bug,
                    "depth_bonus": depth_bonus},
    }


def plan(job: Dict[str, Any], discovered: List[str],
         *, verify_cmd: str = "bash scripts/smoke.sh") -> Dict[str, Any]:
    issue = job.get("issue")
    criteria = extract_criteria(job.get("body") or "")
    gate = verify.gate_ref(verify_cmd)
    cx = _complexity(job)

    # Units are assembled WITHOUT ids, then lettered A,B,C… in emission order at
    # the end — so a prepended diagnosis phase keeps the ids contiguous.
    units: List[Dict[str, Any]] = []

    # Diagnosis phase (#134): a distinct investigation unit for deep/complex work
    # (deep-bug label, or a high enough slice count). It relies ONLY on signals
    # already on the item — root cause from the issue thread + referenced code —
    # NOT on new intake history (that grounding is #133, separate and blocked).
    if cx["diagnosis"]:
        focus = ", ".join(f"`{p}`" for p in discovered[:4]) or "the referenced code paths"
        units.append({
            "id": "",
            "deliverable": _TPL["diagnosis_deliverable"].format(issue=issue, focus=focus),
            "files": list(discovered),
            "specialization": _spec("analyst", "diagnostics / root-cause"),
            "depends_on": [],
            "phase": "diagnosis",
            "acceptance": _TPL["diagnosis_acceptance"].format(),
        })

    # Implementation phase: one cohesive unit per referenced file, then — when
    # complexity asks for more slices than there are files — generic slices to
    # reach the target count. xs/s/m with no label/body signal => 1 slice, so a
    # trivial issue keeps today's single implementation unit (zero inflation).
    for path in discovered:
        fn, domain = _spec_for(path)
        units.append({
            "id": "",
            "deliverable": _TPL["file_deliverable"].format(path=path),
            "files": [path],
            "specialization": _spec(fn, domain),
            "depends_on": [],
            "phase": "implement",
            "acceptance": _TPL["file_acceptance"].format(path=path, gate=gate),
        })

    impl_emitted = len(discovered)
    target_slices = cx["slices"]
    n_generic = max(target_slices - impl_emitted, 1 if impl_emitted == 0 else 0)
    for k in range(n_generic):
        # Slice the implementation into independent vertical cuts so distinct
        # agents can build them concurrently — a real fan-out, earned by the
        # complexity signal, not a stub.
        suffix = f" (slice {k + 1} of {n_generic})" if (n_generic > 1 or impl_emitted) else ""
        units.append({
            "id": "",
            "deliverable": _TPL["slice_deliverable"].format(issue=issue, suffix=suffix),
            "files": [],
            "specialization": _spec("engineer", "general software"),
            "depends_on": [],
            "phase": "implement",
            "acceptance": _TPL["slice_acceptance"].format(gate=gate),
        })

    # Letter the impl/diagnosis units now so the verify unit can depend on them.
    for i, u in enumerate(units):
        u["id"] = chr(ord("A") + i)

    # Always-present integration + verification unit; depends on every prior unit.
    impl_ids = [u["id"] for u in units]
    verify_id = chr(ord("A") + len(units))
    units.append({
        "id": verify_id,
        "deliverable": _TPL["verify_deliverable"].format(gate=gate, issue=issue),
        "files": ["scripts/smoke.sh"],
        "specialization": _spec("engineer", "QA / test automation"),
        "depends_on": impl_ids,
        "phase": "verify",
        "acceptance": _TPL["verify_acceptance"].format(gate=gate),
    })

    # Cap at the swarm max. Keep the integration/verify unit (the serial tail) —
    # shed implementation/diagnosis units from the front, never the gate — then
    # repoint verify's depends_on at the survivors so the DAG stays consistent.
    capped = len(units) > _SWARM_MAX
    if capped:
        verify_unit = units[-1]
        kept = units[:_SWARM_MAX - 1]
        for i, u in enumerate(kept):           # re-letter the survivors A,B,C…
            u["id"] = chr(ord("A") + i)
        verify_unit["id"] = chr(ord("A") + len(kept))
        verify_unit["depends_on"] = [u["id"] for u in kept]
        units = kept + [verify_unit]

    staffing = {
        "agent_count": len(units),
        "swarm_max": _SWARM_MAX,
        "capped": capped,
        "parallel": [u["id"] for u in units if not u["depends_on"]],
        "sequential_tail": [u["id"] for u in units if u["depends_on"]],
        # Why this many agents (#134): the complexity signal that drove the slice
        # count, surfaced for the work order / parallelization rationale.
        "complexity": cx,
        "assignments": [
            {"unit": u["id"], "specialist": u["specialization"]["label"]} for u in units
        ],
    }
    return {"units": units, "staffing": staffing, "criteria": criteria}
