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
from foundation.workflow.tuning import get_tuning  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # architect dir (verify)
import verify  # noqa: E402

_tuning = get_tuning()

# Hard cap on agents (= number of units). Tunable via app/config/tuning.yml
# (generation.decompose.swarm_max).
_SWARM_MAX = _tuning.swarm_max

# Complexity-aware staffing knobs (#134) — all thresholds/counts live in config
# (generation.decompose.complexity); this module only matches + renders.
_CX = _tuning.decompose_complexity

# Work-order UNIT TEXT templates (#144) — the template STRINGS live in config
# (generation.decompose.templates); this module COMPUTES the fields
# (issue/path/gate/focus/suffix) and fills them via str.format. A missing config
# falls back to DEFAULTS (byte-identical text).
_TPL = _tuning.decompose_templates

# Feature-decomposition knobs (greenfield multi-feature builds). When a job is a
# "build an app" ask that enumerates features/pages/routes AND references no
# existing files, decompose by FEATURE: a foundation unit, then one unit per
# feature (a parallel wave of team agents), then the verify tail. Config lives in
# generation.decompose.features; this module only matches + renders.
_FEAT = _tuning.decompose_features
_FTPL = dict(_FEAT.get("templates") or {})

# Light backend-vs-frontend hint for staffing a feature unit (data only).
_BACKEND_HINT = re.compile(
    r"\b(api|backend|server|database|db|persistence|auth|schema|migration|model|endpoint|webhook|cron|queue)\b",
    re.IGNORECASE)
_FEATURE_HEADINGS = [str(h).lower() for h in (_FEAT.get("headings") or [])]
_BUILD_SIGNALS = [str(s).lower() for s in (_FEAT.get("build_signals") or [])]

# Research-slice knobs (generation.decompose.research). When an issue has an
# information gap, the architect schedules a RESEARCH unit (deep /deep-research or
# surface /research) ahead of implementation; the implementation units depend on it.
_RES = _tuning.decompose_research
_RTPL = dict(_RES.get("templates") or {})
_RESEARCH_LABELS = [str(s).lower() for s in (_RES.get("labels") or [])]
_DEEP_RESEARCH_LABELS = [str(s).lower() for s in (_RES.get("deep_labels") or [])]
_RESEARCH_HEADINGS = [str(h).lower() for h in (_RES.get("headings") or [])]
_DEEP_SCOPES = [str(s).lower() for s in (_RES.get("deep_scopes") or [])]


def _spec_for(path: str):
    """Map a file/area to a (function, domain) specialization.

    The ordered match rules live in app/config/tuning.yml
    (generation.decompose.specialization_rules) and are evaluated by
    tuning.spec_for — edit the config to retune staffing labels.
    """
    return _tuning.spec_for(path)


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


def _slug(text: str, *, words: int = 4) -> str:
    parts = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(parts[:words]) or "feature"


def _feature_descriptor(text: str) -> Dict[str, str]:
    """Turn one feature/page/route bullet into ``{name, route}``. An explicit
    ``/path`` token in the text wins; otherwise a slug route is derived from the
    name. Issue text is parsed as untrusted data only."""
    name = " ".join(text.split()).strip(" .:-")
    m = re.search(r"(?<!\w)(/[A-Za-z0-9][\w/-]*)", text)
    route = m.group(1) if m else "/" + _slug(name)
    # Trim a trailing "at /route" style suffix from the displayed name.
    name = re.sub(r"\s*[\(\[]?\s*(?:at\s+)?/[\w/-]+\s*[\)\]]?\s*$", "", name).strip() or name
    return {"name": name[:80], "route": route}


def _feature_spec(name: str):
    """Light backend-vs-frontend staffing hint for a feature unit (data only)."""
    if _BACKEND_HINT.search(name):
        return ("engineer", "backend / API")
    return ("engineer", "frontend / UI")


def _build_intent(job: Dict[str, Any]) -> bool:
    text = f"{job.get('title', '')}\n{job.get('body', '')}".lower()
    return any(sig in text for sig in _BUILD_SIGNALS)


def extract_features(body: str, *, limit: int = 0) -> List[Dict[str, str]]:
    """Feature/page/route bullets under the first configured feature heading, as
    ``[{name, route}]``. ``[]`` when no such section is present. Data only."""
    if not body:
        return []
    cap = limit or int(_FEAT.get("max_features", 12) or 12)
    for h in _FEATURE_HEADINGS:
        pat = re.compile(r"^\s*(?:#{1,6}\s*|\*\*\s*)?" + re.escape(h) + r"\b", re.IGNORECASE)
        items = _capture_under(body, pat, cap)
        if items:
            return [_feature_descriptor(it) for it in items][:cap]
    return []


def _detect_features(job: Dict[str, Any], discovered: List[str]) -> List[Dict[str, str]]:
    """Return a feature list IFF the job is a greenfield, multi-feature build:
    a build-intent signal, an enumerated feature/page/route section, and NO
    referenced existing files. Otherwise ``[]`` — the slice path runs unchanged,
    so existing bug/refactor decomposition is byte-identical."""
    if discovered:                       # references existing files -> not greenfield
        return []
    if not _build_intent(job):
        return []
    feats = extract_features(job.get("body") or "")
    if len(feats) < int(_FEAT.get("min_features", 2) or 2):
        return []
    cap = max(1, min(int(_FEAT.get("max_features", 12) or 12), _SWARM_MAX - 2))
    return feats[:cap]


def _detect_research(job: Dict[str, Any]) -> Dict[str, str]:
    """Return ``{depth, skill, focus}`` when the issue carries an information gap
    the architect should resolve before building — a research label or a
    Research / Open-questions / Unknowns section. ``{}`` otherwise (no research
    unit; existing behaviour). Depth escalates to the ``/deep-research`` harness on
    a deep label or an l/xl scope, else a surface ``/research`` pass. Issue text is
    parsed as untrusted data only."""
    labels = _label_names(job)
    body = job.get("body") or ""
    scope = str(job.get("scope") or "").lower()

    focus_items: List[str] = []
    for h in _RESEARCH_HEADINGS:
        pat = re.compile(r"^\s*(?:#{1,6}\s*|\*\*\s*)?" + re.escape(h) + r"\b", re.IGNORECASE)
        focus_items = _capture_under(body, pat, 6)
        if focus_items:
            break

    has_label = any(any(sig in name for name in labels) for sig in _RESEARCH_LABELS)
    if not has_label and not focus_items:
        return {}

    deep = (any(any(sig in name for name in labels) for sig in _DEEP_RESEARCH_LABELS)
            or scope in _DEEP_SCOPES)
    skill = _RES.get("deep_skill", "/deep-research") if deep else _RES.get("surface_skill", "/research")
    focus = "; ".join(focus_items[:4]) if focus_items else \
        "the unfamiliar libraries, APIs, and concepts the issue references"
    return {"depth": "deep" if deep else "surface", "skill": str(skill), "focus": focus[:200]}


def _waves(units: List[Dict[str, Any]]) -> List[List[str]]:
    """Topological waves over ``depends_on``: each wave is the set of units whose
    dependencies are satisfied by earlier waves — a parallel wave of team agents.
    Engineering proceeds wave by wave (foundation, then features, then verify)."""
    done: set = set()
    waves: List[List[str]] = []
    remaining = list(units)
    while remaining:
        ready = [u for u in remaining if all(d in done for d in (u.get("depends_on") or []))]
        if not ready:                    # dependency-cycle guard: emit the rest
            ready = remaining
        ready_ids = {u["id"] for u in ready}
        waves.append([u["id"] for u in ready])
        done |= ready_ids
        remaining = [u for u in remaining if u["id"] not in ready_ids]
    return waves


def plan(job: Dict[str, Any], discovered: List[str],
         *, verify_cmd: str = "bash scripts/smoke.sh") -> Dict[str, Any]:
    issue = job.get("issue")
    criteria = extract_criteria(job.get("body") or "")
    gate = verify.gate_ref(verify_cmd)
    cx = _complexity(job)

    # Units are assembled WITHOUT ids, then lettered A,B,C… in emission order at
    # the end — so a prepended research/diagnosis/foundation phase keeps ids
    # contiguous. Cross-unit dependencies are resolved by phase after lettering.
    units: List[Dict[str, Any]] = []

    # Research slice: scheduled FIRST when the issue has an information gap, so the
    # /deep-research (deep) or /research (surface) findings ground every later unit.
    research = _detect_research(job)
    if research:
        units.append({
            "id": "",
            "deliverable": _RTPL["deliverable"].format(
                issue=issue, focus=research["focus"], skill=research["skill"]),
            "files": [],
            "specialization": _spec("researcher", "research / investigation"),
            "depends_on": [],
            "phase": "research",
            "acceptance": _RTPL["acceptance"].format(skill=research["skill"]),
            "skill": research["skill"],
            "depth": research["depth"],
        })

    features = _detect_features(job, discovered)
    if features:
        # FEATURE DECOMPOSITION (greenfield multi-feature build): a foundation unit
        # first (the shared skeleton every feature needs), then ONE unit PER FEATURE
        # — all depending only on the foundation, so they form a single parallel
        # wave of team agents — then the integration/verify tail. This is what turns
        # "build a personal accounting app" into a foundation-first sequence of
        # parallel work units, baked into baseworkflow (every engine inherits it).
        units.append({
            "id": "",
            "deliverable": _FTPL["foundation_deliverable"].format(issue=issue),
            "files": [],
            "specialization": _spec("engineer", "application scaffolding / platform"),
            "depends_on": [],
            "phase": "foundation",
            "acceptance": _FTPL["foundation_acceptance"].format(gate=gate),
        })
        for feat in features:
            fn, domain = _feature_spec(feat["name"])
            route = feat.get("route") or ""
            route_txt = f" ({route})" if route else ""
            unit = {
                "id": "",
                "deliverable": _FTPL["feature_deliverable"].format(
                    feature=feat["name"], route=route_txt, issue=issue),
                "files": [],
                "specialization": _spec(fn, domain),
                "depends_on": [],        # resolved to the foundation id after lettering
                "phase": "feature",
                "acceptance": _FTPL["feature_acceptance"].format(feature=feat["name"], gate=gate),
            }
            if route:
                unit["route"] = route
            units.append(unit)
    else:
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

    # A research unit added ahead of the build can push the plan past swarm_max;
    # trim trailing implementation/feature units to fit, never the research,
    # foundation, or verify units. (No research -> the swarm cap below is the only
    # gate, so existing capping behaviour is unchanged.)
    if research and len(units) + 1 > _SWARM_MAX:
        head = [u for u in units if u["phase"] in ("research", "foundation")]
        body_units = [u for u in units if u["phase"] not in ("research", "foundation")]
        keep = max(1, _SWARM_MAX - 1 - len(head))
        units = head + body_units[:keep]

    # Letter the research/diagnosis/foundation/feature units now so verify can depend on them.
    for i, u in enumerate(units):
        u["id"] = chr(ord("A") + i)

    # Resolve cross-unit dependencies by phase now that ids are assigned: the
    # research unit gates the build (implementation depends on its findings); the
    # foundation gates the features. With no research/foundation this is a no-op
    # (deps stay empty), so non-research and slice plans are byte-identical.
    research_id = next((u["id"] for u in units if u["phase"] == "research"), None)
    foundation_id = next((u["id"] for u in units if u["phase"] == "foundation"), None)
    for u in units:
        if u["phase"] == "research":
            u["depends_on"] = []
        elif u["phase"] == "feature":
            u["depends_on"] = [foundation_id] if foundation_id else (
                [research_id] if research_id else [])
        elif u["phase"] in ("foundation", "implement", "diagnosis"):
            u["depends_on"] = [research_id] if research_id else []

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
        # The engineering execution order as a sequence of parallel waves: each
        # inner list is a set of units whose deps are satisfied, run concurrently
        # by team agents. For a feature build this reads [[foundation],[features…],[verify]].
        "waves": _waves(units),
        # Why this many agents (#134): the complexity signal that drove the slice
        # count, surfaced for the work order / parallelization rationale.
        "complexity": cx,
        "assignments": [
            {"unit": u["id"], "specialist": u["specialization"]["label"]} for u in units
        ],
    }
    return {"units": units, "staffing": staffing, "criteria": criteria}
