#!/usr/bin/env python3
"""seed.py — the ``seed:*`` namespace: the seed controller and its variants.

The three-layer split (ADR-003) in one module: the WORKFLOW references the
``seed.intake`` controller; the CONTROLLERS — registered here as
``ControllerSpec``s, so their needs stay statically computable ("a controller's
needs are the union of its actions'") — reference the ``seed:*`` action tokens;
the ACTION bodies reach implementation complexity only through services
(``ctx.service("github.access")`` / ``…("operator.interactive")``), never by
importing it.

Routing practices #190's supersede: ``seed:route`` materializes the variant
matching the accepted request via ``compile_controller`` (the same code path a
phase reference compiles through) and preempts the seed controller's own
processing with it (``policy=abandon`` — the fallback tail is skipped). A
rejected or unroutable request falls through to ``seed:fallback``, which
records the ``decision`` deliverable; the workflow's ``terminal_when:
seed_unrouted`` completes the lifecycle there.
"""
from __future__ import annotations

from typing import Any, Dict

from foundation.actions import ok
from foundation.needs import KIND_SERVICE
from foundation.workflow import compile_controller, controller_needs, load_workflow

WORKFLOW_PATH = "workflows/baseworkflow.yml"

# The parsed workflow: its resolved manifest library + budgets feed the dynamic
# compile_controller path (route's spawn) exactly as they feed the static one.
_DOC = load_workflow(WORKFLOW_PATH)
_MANIFESTS: Dict[str, Any] = dict(_DOC.manifests)
_BUDGETS: Dict[str, int] = dict(_DOC.budgets)

# What a work item minimally requires. A non-interactive job request missing a
# required field is REJECTED (there is no one to ask); an interactive request
# may leave fields open — they are recorded as open_needs on the payload.
REQUIRED_FIELDS = ("title", "goal")
OPTIONAL_FIELDS = ("acceptance",)


def _acceptance_from(body: str) -> str:
    """Lift an '## Acceptance criteria'-style section out of an issue body."""
    out, taking = [], False
    for ln in (body or "").splitlines():
        if ln.lstrip().startswith("#"):
            taking = "acceptance" in ln.lower()
            continue
        if taking and ln.strip():
            out.append(ln.strip())
    return "\n".join(out)


def _fields_from_prompt(prompt: str) -> Dict[str, str]:
    """Seed the minimal work-item fields from a single freeform prompt.

    The remodel (#unified-prompt): an operator can hand in ONE freeform string
    instead of pre-splitting it into the title/goal "issue format". ``goal`` is
    the whole prompt; ``title`` is its first non-empty line (trimmed). Structural
    fields, when also supplied, still win over these derived ones.
    """
    text = (prompt or "").strip()
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return {"title": first[:72].rstrip(), "goal": text}


def register(registry: Any) -> None:
    """Register the seed controllers, action bodies and predicates."""

    # -- the controllers (the coordination layer) ----------------------------
    registry.register_controller(
        "seed.intake",
        steps=("seed:ingest", "seed:classify", "seed:route", "seed:fallback"),
        description="normalize -> satisfy -> route (supersede) | fallback (record rejection)",
    )
    registry.register_controller(
        "seed.handle.github",
        steps=("seed:pull_issue", "seed:emit_work_item"),
        description="GitHub-issue intake variant",
    )
    registry.register_controller(
        "seed.handle.interactive",
        steps=("seed:resolve_needs", "seed:emit_work_item"),
        description="operator-interactive intake variant",
    )
    registry.register_controller(
        "seed.handle.job",
        steps=("seed:accept_job", "seed:emit_work_item"),
        description="non-interactive job-request intake variant (accept/reject)",
    )

    # -- predicates -----------------------------------------------------------
    @registry.predicate("seed_unrouted")
    def seed_unrouted(result: Any, ctx: Any) -> bool:
        # The seed handed off (supersede) -> no decision deliverable -> continue.
        # It did not (rejection / unroutable) -> the decision ends the lifecycle.
        return ctx.shelves.deliverables.has("decision")

    # -- seed.intake bodies ---------------------------------------------------
    @registry.action("seed_ingest")
    def seed_ingest(inputs: Dict[str, Any]) -> Dict[str, Any]:
        request = dict(inputs.get("request") or {})
        fields = dict(request.get("fields") or request.get("job") or {})
        prompt = str(request.get("prompt") or "")
        # A bare prompt is a first-class input: seed the minimal fields from it
        # when they weren't supplied structurally (structural values still win).
        if prompt and not (fields.get("title") and fields.get("goal")):
            derived = _fields_from_prompt(prompt)
            fields = {**derived, **{k: v for k, v in fields.items() if v}}
        source = str(request.get("source", "")).lower()
        if not source:
            # Content classification (the remodel): a structured GitHub reference
            # is an issue; a bare prompt is handled interactively; anything with
            # complete required fields is a non-interactive job request.
            if request.get("repo") and request.get("issue"):
                source = "github"
            elif prompt and not (request.get("fields") or request.get("job")):
                source = "interactive"
            elif fields.get("title") and fields.get("goal"):
                source = "job"
            else:
                source = "interactive"
        return {
            "envelope": {
                "source": source,
                "repo": request.get("repo", ""),
                "issue": request.get("issue"),
                "fields": fields,
                "prompt": prompt,
            }
        }

    @registry.action("seed_classify", needs_ctx=True)
    def seed_classify(inputs: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
        envelope = inputs.get("envelope") or {}
        source = envelope.get("source") or ""
        variant = f"seed.handle.{source}"
        intake = {
            "source": source, "variant": variant,
            "accepted": False, "unmet": [], "missing": [], "reason": "",
        }
        if not registry.has_controller(variant):
            intake["reason"] = f"unknown request source {source!r}"
            return {"intake": intake}
        # Request satisfaction (ADR-003): the ONE variant this request routes to,
        # against the live provisions — not the whole contract.
        needs = controller_needs(variant, registry=registry, manifests=_MANIFESTS)
        provisions = ctx.services.provisions() if ctx.services is not None else ()
        intake["unmet"] = [n.key for n in needs.unmet(provisions) if n.kind == KIND_SERVICE]
        if intake["unmet"]:
            intake["reason"] = "required services unavailable: " + ", ".join(intake["unmet"])
            return {"intake": intake}
        if source == "github" and not (envelope.get("repo") and envelope.get("issue")):
            intake["reason"] = "github request needs repo + issue"
            return {"intake": intake}
        if source == "job":
            missing = [f for f in REQUIRED_FIELDS if not (envelope.get("fields") or {}).get(f)]
            if missing:
                intake["missing"] = missing
                intake["reason"] = "job request rejected — missing required fields: " + ", ".join(missing)
                return {"intake": intake}
        intake["accepted"] = True
        intake["reason"] = "request satisfies the variant's needs"
        return {"intake": intake}

    def _route_builder(factory: Any):
        def body(payload: Any, ctx: Any) -> Any:
            intake = ctx.shelves.shared.get("intake") or {}
            if not intake.get("accepted"):
                return ok({"routed": None})
            variant = intake["variant"]
            control = compile_controller(
                variant, registry=registry, manifests=_MANIFESTS, factory=factory, budgets=_BUDGETS
            )
            ctx.supersede(control, name=variant, priority=1)  # policy=abandon: take over
            return ok({"routed": variant})

        return body

    registry.register_action("seed_route", _route_builder, needs_factory=True)

    @registry.action("seed_fallback")
    def seed_fallback(inputs: Dict[str, Any]) -> Dict[str, Any]:
        intake = inputs.get("intake") or {}
        return {
            "decision": {
                "accepted": bool(intake.get("accepted")),
                "handled": False,
                "source": intake.get("source"),
                "variant": intake.get("variant"),
                "unmet": list(intake.get("unmet") or ()),
                "missing": list(intake.get("missing") or ()),
                "reason": intake.get("reason") or "no superseding handler took the request",
            }
        }

    # -- variant bodies (each converges on seed:emit_work_item) ---------------
    @registry.action("seed_pull_issue", needs_ctx=True)
    def seed_pull_issue(inputs: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
        envelope = inputs.get("envelope") or {}
        issue = ctx.service("github.access").fetch_issue(envelope.get("repo"), envelope.get("issue"))
        return {
            "payload": {
                "title": issue.get("title", ""),
                "goal": issue.get("body", ""),
                "acceptance": _acceptance_from(issue.get("body", "")),
                "labels": list(issue.get("labels") or ()),
                "open_needs": [],
                "provenance": {
                    "kind": "github.issue",
                    "repo": envelope.get("repo"),
                    "issue": envelope.get("issue"),
                },
            }
        }

    @registry.action("seed_resolve_needs", needs_ctx=True)
    def seed_resolve_needs(inputs: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
        envelope = inputs.get("envelope") or {}
        operator = ctx.service("operator.interactive")
        fields = dict(envelope.get("fields") or {})
        open_needs = []
        for f in REQUIRED_FIELDS + OPTIONAL_FIELDS:
            if not fields.get(f):
                required = f in REQUIRED_FIELDS
                fields[f] = operator.ask(
                    f"Provide the request's {f}{'' if required else ' (optional)'}:", key=f, default=""
                )
            if not fields.get(f):
                open_needs.append(f)  # the party declined — the requirement stays open
        return {
            "payload": {
                **{k: fields.get(k, "") for k in REQUIRED_FIELDS + OPTIONAL_FIELDS},
                "labels": [],
                "open_needs": open_needs,
                "provenance": {"kind": "operator.interactive"},
            }
        }

    @registry.action("seed_accept_job")
    def seed_accept_job(inputs: Dict[str, Any]) -> Dict[str, Any]:
        fields = dict((inputs.get("envelope") or {}).get("fields") or {})
        missing = [f for f in REQUIRED_FIELDS if not fields.get(f)]
        if missing:  # classify gates this; an incomplete payload here is a fault
            raise ValueError(f"job request missing required fields: {', '.join(missing)}")
        return {
            "payload": {
                **{k: fields.get(k, "") for k in REQUIRED_FIELDS + OPTIONAL_FIELDS},
                "labels": list(fields.get("labels") or ()),
                "open_needs": [f for f in OPTIONAL_FIELDS if not fields.get(f)],
                "provenance": {"kind": "job.request"},
            }
        }

    @registry.action("seed_emit_work_item")
    def seed_emit_work_item(inputs: Dict[str, Any]) -> Dict[str, Any]:
        envelope, payload = inputs.get("envelope") or {}, inputs.get("payload") or {}
        return {
            "work_item": {
                "source": envelope.get("source"),
                "title": payload.get("title", ""),
                "goal": payload.get("goal", ""),
                "acceptance": payload.get("acceptance", ""),
                "labels": list(payload.get("labels") or ()),
                "open_needs": list(payload.get("open_needs") or ()),
                "provenance": dict(payload.get("provenance") or {}),
            }
        }
