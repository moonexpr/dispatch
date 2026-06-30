#!/usr/bin/env python3
"""tuning.py — single, declarative tuning surface for the dispatch pipeline.

The values that govern WORK SELECTION (which issue is picked) and PROMPT
GENERATION (how the work order reads) live in one editable YAML file,
``app/config/tuning.yml`` (reached via the ``foundation.filesys`` facade — #142),
so they can be fine-tuned without touching Python.

This loader reads that file and deep-merges it over the in-code ``DEFAULTS``
below. An **absent** file degrades to ``DEFAULTS`` (fail-safe: the committed
YAML mirrors ``DEFAULTS``, so today's behaviour is reproduced). A file that is
**present but malformed** — unparseable YAML or an unknown/typo'd key — fails
**loud** with ``ConfigError`` instead of silently reverting (#156, #130). The
parsed result is mtime-memoized so repeated calls don't re-parse. Override the
path with ``DISPATCH_TUNING_FILE``.

Capability-safe: YAML is read through ``foundation.filesys`` (``yaml.safe_load``,
which never executes tags), so the classifier (a quarantine reader, HANDOFF §8)
gains no capability. Deterministic: identical config always yields identical
constants.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Tuple

# The tuning surface, relative to the app dir (foundation.filesys resolves it).
_TUNING_REL = "config/tuning.yml"

# Repo root on sys.path so ``from foundation import filesys`` resolves regardless of
# how this module was imported (consumers put only the repo root on PYTHONPATH). This
# is path-only — no import side effects — mirroring workplan_config's seam.
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# In-code defaults — the canonical fallback. The committed tuning.json mirrors
# these values and is the surface an admin edits; the file overrides these.
DEFAULTS: Dict[str, Any] = {
    "selection": {
        "classify": {
            "hints": {
                "xs": ["typo", "rename", "comment", "one-line", "one line",
                       "wording", "docstring", "lint", "format"],
                "s": ["add a flag", "small", "minor", "tweak", "adjust", "bump version"],
                "l": ["refactor", "redesign", "architecture", "migration",
                      "rewrite", "overhaul", "multi-service", "breaking change"],
                "wontdo": ["wontfix", "won't do", "wont do", "by design", "not planned"],
                "dup": ["duplicate", "dupe", "already reported", "same as #"],
                "vague": ["not sure", "maybe", "somehow", "investigate", "unclear",
                          "?", "thoughts", "discuss"],
            },
            "conf": {
                "base": 0.60,
                "scope_signal_weight": 0.10,
                "scope_signal_cap": 3,
                "body_long_bonus": 0.10,
                "body_long_threshold": 80,
                "vague_weight": 0.12,
                "vague_cap": 3,
                "body_short_penalty": 0.15,
                "body_short_threshold": 25,
                "clamp_min": 0.05,
                "clamp_max": 0.99,
                "round_ndigits": 4,
                "dup_cap": 0.50,
            },
            "scope_route": {"xs": "gen-local", "s": "gen-local",
                            "m": "gen-default", "l": "gen-frontier"},
            # Controlled vocabularies (TriageResult enums). The CONTENT — which
            # actions/scopes/routes are valid — is data; the classification LOGIC
            # stays in classify.py. epic_prefix is the (lowercased) title marker
            # that flags an [Epic] container issue for decomposition.
            "vocab": {
                "actions": ["implement", "needs-human", "wont-do",
                            "duplicate?", "decompose"],
                "scopes": ["xs", "s", "m", "l"],
                "routes": ["gen-local", "gen-default", "gen-frontier"],
                "epic_prefix": "[epic]",
            },
        },
        "ranker": {
            "fwd_patterns": [
                r"blocked\s+by\s+#(\d+)",
                r"depends?\s+on\s+#(\d+)",
                r"requires?\s+#(\d+)",
                r"needs\s+#(\d+)",
                r"follow[-\s]?up\s+to\s+#(\d+)",
                r"prerequisite:?\s*#(\d+)",
                r"after\s+#(\d+)",
                r"^\s*[-*]\s*\[[ xX]\]\s*#(\d+)",
            ],
            "rev_patterns": [
                r"unblocks?:?\s*(?:issue\s*)?#(\d+)",
                r"blocks?\s+#(\d+)",
                r"prerequisite\s+for\s+#(\d+)",
                r"parent\s+epic:?\s*#(\d+)",
            ],
        },
    },
    "generation": {
        "approval": {
            "scope_budget": {"xs": 20000, "s": 40000, "m": 80000, "l": 160000},
            "phase_split": [["READ", 0.15], ["IMPLEMENT", 0.45],
                            ["VERIFY", 0.12], ["COMMIT & PR", 0.08]],
        },
        "decompose": {
            "swarm_max": 15,
            # Complexity-aware staffing (#134). The number of IMPLEMENTATION
            # slices a job is split into scales with signals already on the
            # item — never with new intake data (that is #132/#133, separate).
            # Trivial issues stay at 1 slice (today's 2-unit plan); deep/large
            # issues fan out into real swarms, capped at swarm_max.
            "complexity": {
                # Base implementation slices by triage scope. xs/s/m stay at 1
                # (zero behaviour change for small/trivial work); l/xl earn more.
                "scope_slices": {"xs": 1, "s": 1, "m": 1, "l": 2, "xl": 3},
                # Extra slices per label signal whose name CONTAINS one of these
                # substrings (lowercased match). Summed across matches.
                "label_slice_bonus": {"epic": 2, "deep-bug": 1},
                # Labels (substring match) marking a deep/hard bug — they add a
                # diagnosis phase and one diagnostic slice's worth of bonus.
                "deep_bug_labels": ["bug", "regression", "crash", "defect", "race"],
                # One extra slice per this many chars of issue body (a thread-depth
                # proxy: a long, detailed body implies more independent work),
                # capped by thread_depth_max_bonus. 0 disables the body signal.
                "thread_depth_chars_per_slice": 1200,
                "thread_depth_max_bonus": 2,
                # Emit a distinct DIAGNOSIS unit (a real phase split, not a stub)
                # when the slice count reaches this, or a deep-bug label is present.
                "diagnosis_min_slices": 2,
            },
            # Work-order UNIT TEXT templates (#144 text-templates slice). The
            # template TEXT is data here; decompose.plan still COMPUTES the fields
            # (issue/path/gate/focus/suffix) and fills these via str.format. A
            # missing config falls back to these DEFAULTS (byte-identical text).
            # Any literal brace in template text must be escaped {{ }}.
            "templates": {
                "diagnosis_deliverable": (
                    "Diagnose issue #{issue}: reproduce, isolate the root cause across "
                    "{focus}, and write down the fix plan the implementation slices follow."),
                "diagnosis_acceptance": (
                    "Root cause is identified and a concrete, sliceable fix plan is recorded."),
                "file_deliverable": (
                    "Implement the change in `{path}` per the issue's acceptance criteria."),
                "file_acceptance": (
                    "`{path}` is correct in isolation and {gate} stays green."),
                "slice_deliverable": (
                    "Implement issue #{issue}{suffix} per its acceptance criteria."),
                "slice_acceptance": (
                    "{gate} stays green and this slice's portion of the acceptance criteria is met."),
                "verify_deliverable": (
                    "Integrate the units, run {gate}, and open the PR with `Closes #{issue}`."),
                "verify_acceptance": (
                    "{gate} is green (0 FAIL); PR opened against `main`, not merged."),
            },
            # Feature decomposition (greenfield multi-feature builds). When a job is
            # a "build an app / website" ask that enumerates features/pages/routes
            # AND references no existing files, decompose by FEATURE instead of by
            # slice: a foundation unit first, then one unit PER FEATURE (a parallel
            # wave of team agents), then the integration/verify tail. This is what
            # turns "build a personal accounting app" into a foundation-first
            # sequence of parallel work units. Falls back to the slice path when no
            # feature list or build-intent signal is present (existing behaviour).
            "features": {
                # Markdown headings whose bullet list enumerates the features to
                # build (lowercased substring match on the heading text).
                "headings": ["features", "pages", "routes", "screens", "modules",
                             "milestones", "scope"],
                # Build-intent signals in the title/body that arm feature
                # decomposition (substring, lowercased). Without one, the slice
                # path runs unchanged.
                "build_signals": ["build", "create", "scaffold", "new app",
                                  "web app", "webapp", "website", "application",
                                  "full app", "from scratch", "greenfield",
                                  "vercel", "next.js", "nextjs"],
                # Minimum feature bullets before the feature path engages.
                "min_features": 2,
                # Upper bound on feature units (foundation + verify are extra; the
                # whole plan is still bounded by swarm_max).
                "max_features": 12,
                # Unit text templates (filled via str.format with issue/gate/feature/route).
                "templates": {
                    "foundation_deliverable": (
                        "Scaffold the foundation for #{issue}: project skeleton, the shared "
                        "data/model + persistence layer, and the app shell every feature builds on."),
                    "foundation_acceptance": (
                        "The project builds and runs, the shared foundation is in place, and {gate} stays green."),
                    "feature_deliverable": (
                        "Build the \"{feature}\" feature{route} for #{issue} end-to-end per the acceptance criteria."),
                    "feature_acceptance": (
                        "The \"{feature}\" feature works end-to-end and {gate} stays green."),
                },
            },
            # Research slice. When an issue carries an information gap — a research
            # label, or a Research/Open-questions/Unknowns section — the architect
            # schedules a RESEARCH unit FIRST that every implementation unit depends
            # on, so findings ground the build. Depth picks the skill: a deep signal
            # (deep label, or l/xl scope) runs the `/deep-research` harness; otherwise
            # a surface-level `/research` pass. No signal -> no research unit (existing
            # behaviour).
            "research": {
                # Labels (substring, lowercased) that request a research slice.
                "labels": ["research", "needs-research", "spike", "investigate",
                           "investigation", "rfc", "unknown", "discovery"],
                # Labels that escalate to the deep-research harness.
                "deep_labels": ["deep-research", "deep-dive", "unfamiliar", "novel",
                                "greenfield", "architecture"],
                # Body headings whose section enumerates the open questions to research.
                "headings": ["research", "open questions", "unknowns", "questions",
                             "investigation", "unknowns / risks", "risks"],
                # Triage scopes that escalate to deep research even without a deep label.
                "deep_scopes": ["l", "xl"],
                # The skills the research unit dispatches to (surfaced in its prompt).
                "deep_skill": "/deep-research",
                "surface_skill": "/research",
                "templates": {
                    "deliverable": (
                        "Research {focus} for #{issue} via {skill}: gather sources, resolve the open "
                        "questions, and record the findings + decisions the implementation units rely on."),
                    "acceptance": (
                        "The open questions are answered with cited sources and a concrete, recorded "
                        "decision the implementation units can build on."),
                },
            },
            "specialization_rules": [
                {"any": [{"basename_contains": "smoke"}, {"path_contains": "test"},
                         {"path_contains": "/fixtures/"}],
                 "function": "engineer", "domain": "QA / test automation"},
                {"any": [{"basename_endswith": ".sh"}, {"path_startswith": "scripts/"}],
                 "function": "engineer", "domain": "developer tooling (shell)"},
                {"any": [{"path_startswith": "schemas/"},
                         {"all_of": [{"basename_endswith": ".json"}, {"path_contains": "schema"}]}],
                 "function": "engineer", "domain": "data contracts / schema"},
                {"any": [{"path_startswith": ".github/"}, {"basename_startswith": "ci"}],
                 "function": "engineer", "domain": "CI/CD"},
                {"any": [{"basename_endswith": ".md"}, {"basename_equals": "claude.md"}],
                 "function": "writer", "domain": "developer documentation"},
                {"any": [{"path_startswith": "intake/"}],
                 "function": "engineer", "domain": "data-pipeline (Python)"},
                {"any": [{"path_startswith": "classifier/"}],
                 "function": "engineer", "domain": "ML / classification (Python)"},
                {"any": [{"path_startswith": "foundation/models"}],
                 "function": "engineer", "domain": "LLM integration (Python)"},
                {"any": [{"path_startswith": "architect/"}],
                 "function": "engineer", "domain": "orchestration (Python)"},
                {"any": [{"basename_endswith": ".py"}],
                 "function": "engineer", "domain": "backend (Python)"},
                {"default": True, "function": "engineer", "domain": "general software"},
            ],
        },
        "resources": {"max_files": 6, "cap_bytes": 6000,
                      "contract_cap": 4000, "schema_cap": 4000},
        # Conversation + history embedding caps (#132). intake.py fetches the
        # issue's comment thread and a relevant slice of commit history; the
        # architect embeds them as new EMBEDDED RESOURCES subsections. These caps
        # keep a busy thread from blowing the plan's read budget — data-driven so
        # the bound is tuned here, not in Python (per the systems/content split).
        # max_comments / max_commits: how many of each to embed (most-recent
        # kept). comment_cap_bytes: per-comment body cap. history_cap_bytes:
        # total cap on the rendered history block.
        "intake": {"max_comments": 8, "comment_cap_bytes": 1200,
                   "max_commits": 10, "history_cap_bytes": 2000},
        "workorder": {
            "route_alias": {"gen-local": "local", "gen-default": "sonnet",
                            "gen-frontier": "opus"},
            "box_width": 70,
            # TEST PROCEDURE block LAYOUT template (#144 text-templates slice).
            # The label/layout TEXT is data; workorder still COMPUTES the
            # setup/exercise/verify values and fills this via str.format. The
            # header line (Unit id — specialist) stays in Python. Missing config
            # falls back to this DEFAULT (byte-identical text).
            "block": {
                "test_procedure": (
                    "  Setup:     {setup}\n"
                    "  Exercise:  {exercise}\n"
                    "  Verify:    {verify}"),
            },
            # PLAN-section PHASE TEXT templates (#133). The phase prose is DATA;
            # workorder COMPUTES the per-phase token allocations + impl hint and
            # fills these via str.format. `diagnose` is the TRIAGE-FIRST phase
            # rendered BETWEEN read and implement: it tells the engineer to engage
            # the embedded conversation + history (#132 grounding), confirm/extend
            # the existing triage, RECORD the diagnosis, and only then implement.
            # The diagnosis comment is a WRITE to the thread, so the template gates
            # it behind the dry-run rails (post only when DRY_RUN=0; else capture in
            # a commit message). It draws from the READ allocation — no new token
            # line — so approval.phase_split arithmetic is unchanged.
            "plan": {
                "read": (
                    "PHASE 1 — READ ({read}): everything you need is embedded above under "
                    "EMBEDDED RESOURCES — the referenced source, the worker contract, the Invoice "
                    "schema, and the conventions. Confirm your understanding against it. Do NOT "
                    "fetch or research additional files unless a STOP CONDITION applies."),
                "diagnose": (
                    "PHASE 2 — DIAGNOSE / TRIAGE-FIRST (within the READ allocation, {read}): before "
                    "you change any code, engage the issue thread. {grounding} Confirm or extend the "
                    "existing triage, reproduce the problem if this is a bug, and isolate the root "
                    "cause. RECORD your diagnosis and the fix plan the implementation will follow: "
                    "when DRY_RUN is 1 (it is {dry_run} here) capture it in your first commit message "
                    "on `{branch}` (do NOT post to the thread); only when DRY_RUN=0 may you ALSO post "
                    "it as a brief thread comment on issue #{issue}. Do not implement until the root "
                    "cause and a concrete, sliceable fix plan are written down."),
                "implement": (
                    "PHASE 3 — IMPLEMENT ({implement}): {impl_hint}. Commit incrementally on "
                    "`{branch}`."),
                "verify": (
                    "PHASE 4 — VERIFY ({verify}): run {gate}. It must pass. Fix causes, not "
                    "symptoms. If it cannot pass within budget, open a DRAFT PR explaining which "
                    "check fails."),
                "commit": (
                    "PHASE 5 — COMMIT & PR ({commit}): stage only files you touched. Commit "
                    "(signed). Open one PR against `main` with the done-criteria checklist and "
                    "`Closes #{issue}`. Stop. Return the Invoice."),
                "reserve": (
                    "RESERVE ({reserve}): contingency held by ADMIN — do not pre-spend."),
                "grounding_with_thread": (
                    "Read the embedded 'Conversation so far' (the maintainer/triage discussion) and "
                    "'Relevant history' (linked PRs / recent commits) above — they are the grounding "
                    "for this task; build on the diagnosis already recorded there rather than "
                    "starting cold."),
                "grounding_no_thread": (
                    "No conversation or linked history was embedded for this issue; diagnose from the "
                    "issue body and the referenced source above."),
            },
        },
        # Research-mode gap-detection (Pillar 2, E6-1). The deterministic architect
        # gap heuristic (architect/research.py) reads these. `enabled` is a
        # clean kill-switch (false => the heuristic always returns needs_research=False).
        "research": {
            "enabled": True,
            "low_confidence_threshold": 0.55,
            "needs_research_label": "needs-research",
            "route": "gen-local",
            # Dispatch-path actuation (E6-2). When true, the architect renders a
            # research work order on a detected gap; default false because offline /
            # cross-repo discovery cannot yet distinguish a genuine gap from a
            # target-repo file simply absent in the dispatch tree (would spuriously
            # flip legitimate implementation issues). The heuristic (`enabled`) and
            # rendering ship now; flip this on once discovery is target-repo aware.
            "dispatch_enabled": False,
        },
    },
    # Budget oracle / soft-cap config (Pillar 3). The committed tuning.json holds
    # the operator-decided values (#48 via #68); these are the safe fallbacks so a
    # missing budget block still deep-merges to a usable shape.
    "budget": {
        "window": {
            "plan_tier": "max20",
            "window_token_limit": 220000,
            "soft_cap_fraction": 0.80,
        },
        "plan_limits": {"pro": 44000, "max5": 88000, "max20": 220000},
        "custom_limit_tokens": 0,
    },
    # Crash-recovery policy for the cron reaper (Pillar 1, E1-3). The committed
    # tuning.json holds the operator-decided values (issue #49, committed via #68);
    # these fallbacks keep a missing recovery block deep-merging to a usable shape.
    "recovery": {
        "reaper_timeout_hours": 4,
        "engineer_failure_policy": "architect-rescaffold",
    },
    # Routing policy (#137, #144). The orchestration layer's scope->route map, the
    # CI-failure fix-ladder (attempt -> model tier), the ladder cap, and the
    # intake confidence floor — declarative DATA the Python interprets. The
    # MATCHERS stay in code (common.py / fix_dispatch.py); only the values live
    # here. scope_route mirrors selection.classify.scope_route (the classifier's
    # copy, used at triage time); this one is the runtime route_for_scope fallback.
    # fix_ladder maps the stringified attempt number to its model tier; an attempt
    # past the last rung (or 0/invalid) yields needs_human_route. fix_attempt_cap
    # is the highest attempt that still gets a tier (must equal the ladder length).
    "routing": {
        "scope_route": {"xs": "gen-local", "s": "gen-local",
                        "m": "gen-default", "l": "gen-frontier"},
        "default_route": "gen-default",
        "fix_ladder": {"1": "gen-local", "2": "gen-default", "3": "gen-frontier"},
        "fix_attempt_cap": 3,
        "needs_human_route": "needs-human",
        "confidence_threshold": 0.55,
    },
}


def _merge(base: Any, ovr: Any) -> Any:
    """Deep-merge ``ovr`` over ``base``. Dicts merge; lists/scalars replace.
    Keys starting with ``_`` (documentation) are dropped from the result."""
    if isinstance(base, dict) and isinstance(ovr, dict):
        out: Dict[str, Any] = {}
        for k in base:
            if k.startswith("_"):
                continue
            out[k] = base[k] if k not in ovr else _merge(base[k], ovr[k])
        for k in ovr:                       # override-only keys
            if k.startswith("_") or k in out:
                continue
            out[k] = ovr[k]
        return out
    return ovr


class ConfigError(ValueError):
    """A *present* tuning file is unparseable or carries an unknown/mis-shaped key.

    Raised so a typo'd or half-saved config fails loud at load instead of being
    silently swallowed back to DEFAULTS (#156)."""


def _validate(data: Any, ref: Any, trail: str = "") -> None:
    """Raise ``ConfigError`` if ``data`` (the override file) carries a key absent
    from ``ref`` (the DEFAULTS skeleton) or flips its mapping/scalar shape.

    Only KEY STRUCTURE is checked — scalar and list *values* are the admin's to
    set freely. ``_``-prefixed (documentation) keys are ignored. Lists are
    opaque: their elements are data (e.g. ``specialization_rules`` rule dicts,
    ``phase_split`` pairs), not config keys, so they are never recursed into."""
    if not isinstance(data, dict):
        return
    for k, v in data.items():
        if isinstance(k, str) and k.startswith("_"):
            continue
        where = f"{trail}.{k}" if trail else str(k)
        if k not in ref:
            raise ConfigError(f"unknown tuning key: {where}")
        if isinstance(ref[k], dict) != isinstance(v, dict):
            raise ConfigError(f"tuning key {where}: mapping/scalar shape mismatch")
        if isinstance(ref[k], dict):
            _validate(v, ref[k], where)


def _config_path() -> str:
    return os.environ.get("DISPATCH_TUNING_FILE") or _TUNING_REL


# mtime-memoized parse results, keyed by absolute path → (mtime, merged config).
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def load(path: str = "") -> Dict[str, Any]:
    """Return the merged tuning config (file deep-merged over ``DEFAULTS``).

    Resolution: explicit ``path`` arg > ``$DISPATCH_TUNING_FILE`` >
    ``app/config/tuning.yml``. An **absent** file degrades to ``DEFAULTS``
    (fail-safe). A **present** file that is unparseable or carries an unknown
    key raises ``ConfigError`` (fail-loud, #156). Results are mtime-memoized."""
    relpath = path or _config_path()
    try:
        from foundation import filesys  # lazy: keep import-time dependency-light
        abspath = filesys.resolve(relpath)
    except Exception:
        return _merge(DEFAULTS, {})         # engine/PyYAML unavailable -> DEFAULTS
    try:
        mtime = os.path.getmtime(abspath)
    except OSError:
        return _merge(DEFAULTS, {})         # absent file -> fail-safe DEFAULTS
    cached = _CACHE.get(abspath)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        data = filesys.read_yaml(relpath)
    except FileNotFoundError:
        return _merge(DEFAULTS, {})
    except Exception as exc:                # malformed YAML -> loud
        raise ConfigError(f"tuning file {abspath} is unparseable: {exc}") from exc
    if not data:                            # empty document -> DEFAULTS
        merged = _merge(DEFAULTS, {})
    else:
        if not isinstance(data, dict):
            raise ConfigError(f"tuning file {abspath} is not a mapping")
        _validate(data, DEFAULTS)           # typo'd/unknown key -> loud
        merged = _merge(DEFAULTS, data)
    _CACHE[abspath] = (mtime, merged)
    return merged


# --------------------------------------------------------------------------
# Computed constants — consumers do `from tuning import SCOPE_BUDGET`, the same
# idiom as the old module-level constants, just sourced from the config.
# --------------------------------------------------------------------------
_CFG = load()
_SEL = _CFG["selection"]
_GEN = _CFG["generation"]

# selection
CLASSIFY_HINTS: Dict[str, Tuple[str, ...]] = {
    k: tuple(v) for k, v in _SEL["classify"]["hints"].items()}
CONF: Dict[str, Any] = dict(_SEL["classify"]["conf"])
SCOPE_ROUTE: Dict[str, str] = dict(_SEL["classify"]["scope_route"])
# Classifier controlled vocabularies (TriageResult enums) — data, not logic.
_VOCAB = _SEL["classify"]["vocab"]
ACTIONS: Tuple[str, ...] = tuple(_VOCAB["actions"])
SCOPES: Tuple[str, ...] = tuple(_VOCAB["scopes"])
ROUTES: Tuple[str, ...] = tuple(_VOCAB["routes"])
EPIC_PREFIX: str = str(_VOCAB["epic_prefix"])
DEP_FWD: Tuple[str, ...] = tuple(_SEL["ranker"]["fwd_patterns"])
DEP_REV: Tuple[str, ...] = tuple(_SEL["ranker"]["rev_patterns"])

# generation
SCOPE_BUDGET: Dict[str, int] = {k: int(v) for k, v in _GEN["approval"]["scope_budget"].items()}
PHASE_SPLIT: Tuple[Tuple[str, float], ...] = tuple(
    (name, frac) for name, frac in _GEN["approval"]["phase_split"])
SWARM_MAX: int = int(_GEN["decompose"]["swarm_max"])
# Complexity-aware decomposition knobs (#134) — read by decompose.plan to scale
# implementation slices / emit a diagnosis phase from signals already on the item.
DECOMPOSE_COMPLEXITY: Dict[str, Any] = dict(_GEN["decompose"]["complexity"])
# Work-order UNIT TEXT templates (#144) — data filled by decompose.plan via
# str.format; the classification/staffing logic stays Python.
DECOMPOSE_TEMPLATES: Dict[str, str] = dict(_GEN["decompose"]["templates"])
# Feature decomposition knobs — read by decompose.plan to turn a greenfield
# multi-feature build into a foundation-first sequence of parallel feature units.
DECOMPOSE_FEATURES: Dict[str, Any] = dict(_GEN["decompose"]["features"])
# Research-slice knobs — read by decompose.plan to schedule a /deep-research or
# surface /research unit ahead of implementation when the issue has a gap.
DECOMPOSE_RESEARCH: Dict[str, Any] = dict(_GEN["decompose"]["research"])
SPEC_RULES: List[Dict[str, Any]] = list(_GEN["decompose"]["specialization_rules"])
RES_CAPS: Dict[str, int] = {k: int(v) for k, v in _GEN["resources"].items()}
# Conversation + history embedding caps (#132) — read by intake/intake.py
# (how many comments/commits to fetch) and architect/resources.py (per-item
# byte caps on what gets embedded into the work order).
INTAKE_CAPS: Dict[str, int] = {k: int(v) for k, v in _GEN["intake"].items()}
ROUTE_ALIAS: Dict[str, str] = dict(_GEN["workorder"]["route_alias"])
BOX_W: int = int(_GEN["workorder"]["box_width"])
# Work-order block LAYOUT templates (#144) — label/layout text as data, filled
# by workorder via str.format; the computed values stay Python.
WORKORDER_BLOCK: Dict[str, str] = dict(_GEN["workorder"]["block"])
# PLAN-section PHASE TEXT templates (#133) — the triage-first phase prose as data,
# filled by workorder via str.format; the token allocations stay computed in Python.
WORKORDER_PLAN: Dict[str, str] = dict(_GEN["workorder"]["plan"])

# research-mode gap-detection (Pillar 2, E6-1) — the new generation.research block,
# consumed by architect/research.py::detect_gap.
RESEARCH: Dict[str, Any] = dict(_GEN.get("research", {}))

# budget (Pillar 3 oracle / soft-cap) — exposed the same way as the above.
# NOTE: deliberately NOT named *_TOKEN(S) — these are token-count limits, not
# secrets, and the smoke §7.5 secret-scan keys off a *_TOKEN/_KEY/_SECRET regex.
_BUD = _CFG.get("budget", {})
_BUD_WINDOW = _BUD.get("window", {})
BUDGET_PLAN_TIER: str = str(_BUD_WINDOW.get("plan_tier", "max20"))
BUDGET_WINDOW_LIMIT: int = int(_BUD_WINDOW.get("window_token_limit") or 0)
BUDGET_SOFT_CAP_FRACTION: float = float(_BUD_WINDOW.get("soft_cap_fraction", 0.80))
BUDGET_PLAN_LIMITS: Dict[str, int] = {k: int(v) for k, v in (_BUD.get("plan_limits") or {}).items()}
BUDGET_CUSTOM_LIMIT: int = int(_BUD.get("custom_limit_tokens") or 0)

# recovery (Pillar 1 reaper / engineer-failure policy) — exposed the same way as
# the above. The reaper timeout is read by the bash reaper (scripts/dispatch.sh)
# as the fallback when DISPATCH_CLAIM_TIMEOUT_HOURS is unset.
_REC = _CFG.get("recovery", {})
RECOVERY_REAPER_TIMEOUT_HOURS: float = float(_REC.get("reaper_timeout_hours", 4))
RECOVERY_ENGINEER_FAILURE_POLICY: str = str(
    _REC.get("engineer_failure_policy", "architect-rescaffold"))

# routing (#137, #144) — the orchestration scope->route map, the CI fix-ladder,
# its cap, and the intake confidence floor, read by orchestration/common.py
# (route_for_scope / tier_for_attempt) and fix_dispatch.py (the cap message).
# DATA here; the matchers stay Python.
_ROUTING = _CFG.get("routing", {})
ROUTING_SCOPE_ROUTE: Dict[str, str] = dict(_ROUTING.get("scope_route", {}))
ROUTING_DEFAULT_ROUTE: str = str(_ROUTING.get("default_route", "gen-default"))
FIX_LADDER: Dict[str, str] = {str(k): str(v) for k, v in (_ROUTING.get("fix_ladder") or {}).items()}
FIX_ATTEMPT_CAP: int = int(_ROUTING.get("fix_attempt_cap", len(FIX_LADDER) or 3))
NEEDS_HUMAN_ROUTE: str = str(_ROUTING.get("needs_human_route", "needs-human"))
CONFIDENCE_THRESHOLD: float = float(_ROUTING.get("confidence_threshold", 0.55))


def route_for_scope(scope: str) -> str:
    """Classifier scope -> generation model group (§5.2/§5.3), per the config
    routing.scope_route map. An unknown scope falls back to routing.default_route."""
    return ROUTING_SCOPE_ROUTE.get(scope, ROUTING_DEFAULT_ROUTE)


def tier_for_attempt(attempt) -> str:
    """Fix-ladder (§5.5) per config routing.fix_ladder: attempt -> model tier.
    An attempt past the last rung (or 0/invalid) yields the needs_human sentinel."""
    return FIX_LADDER.get(str(attempt), NEEDS_HUMAN_ROUTE)


# --------------------------------------------------------------------------
# Specialization rule evaluator (used by decompose). First matching rule wins;
# conditions in a rule's "any" list are OR'd; an "all_of" condition is an AND.
# Matching is against the lowercased repo-relative path and its basename.
# --------------------------------------------------------------------------
def _cond(cond: Dict[str, Any], path: str, base: str) -> bool:
    for k, v in cond.items():
        if k == "all_of":
            return all(_cond(c, path, base) for c in v)
        if k == "basename_contains":
            return v in base
        if k == "basename_endswith":
            return base.endswith(v)
        if k == "basename_startswith":
            return base.startswith(v)
        if k == "basename_equals":
            return base == v
        if k == "path_contains":
            return v in path
        if k == "path_startswith":
            return path.startswith(v)
    return False


def spec_for(path: str) -> Tuple[str, str]:
    """Map a file/area to a (function, domain) specialization per SPEC_RULES."""
    p = path.lower()
    base = os.path.basename(p)
    for rule in SPEC_RULES:
        if rule.get("default"):
            return rule["function"], rule["domain"]
        if any(_cond(c, p, base) for c in rule.get("any", [])):
            return rule["function"], rule["domain"]
    return "engineer", "general software"
