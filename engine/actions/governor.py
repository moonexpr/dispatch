#!/usr/bin/env python3
"""governor.py — Governors: decorators that wrap any Action to enforce a budget,
a permission policy, or an iteration cap.

A Governor *is* an Action (it subclasses ``Action`` and delegates ``kind`` to the
action it wraps), so governed and ungoverned actions compose identically — a
Sequence cannot tell the difference, which is the whole point of putting these
concerns in decorators rather than inlining ``if over_budget`` checks into action
logic.

Each Governor takes one ``enforce`` flag that is the only thing distinguishing
the mock factory from the real one:

  * ``enforce=False`` (mock) — permissive: budget still *charges* the meter (so
    spend is observable in the end-to-end test) but never aborts; permission
    allows everything; iteration never denies.
  * ``enforce=True`` (real) — budget aborts on a cap breach, permission is
    deny-by-default, iteration denies past the cap.

Three kinds, mapped to the task's Governor taxonomy and (per the addendum) to the
Claude Agent SDK hook surfaces:

  * ``BudgetGovernor``     — a usage-reading gate (SDK cost/usage hook).
  * ``PermissionGovernor`` — deny-by-default tool gate (canUseTool + PreToolUse).
  * ``IterationGovernor``  — bounds repeated invocation (SubagentStop / loop cap).

Leaf module: stdlib + sibling engine leaves.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional, Set

from .action import Action, BudgetMeter, Context
from .result import BudgetExceeded, Error, PermissionDenied, Result


class Governor(Action):
    """Base decorator. Wraps ``inner``; ``_enforce_pre`` may veto before the inner
    action runs (return an ``Error``), and ``_settle_post`` may adjust the result
    or charge accounting afterward."""

    def __init__(self, inner: Action, *, enforce: bool = True, name: str = "") -> None:
        super().__init__(name or inner.name)
        self._inner = inner
        self.enforce = enforce

    @property
    def kind(self) -> str:  # transparent: a governed action reports its own kind
        return self._inner.kind

    @property
    def inner(self) -> Action:
        return self._inner

    def _enforce_pre(self, payload: Any, ctx: Context) -> Optional[Result]:
        return None

    def _settle_post(self, payload: Any, ctx: Context, result: Result) -> Result:
        return result

    def _invoke(self, payload: Any, ctx: Context) -> Result:
        denied = self._enforce_pre(payload, ctx)
        if denied is not None:
            return denied
        result = self._inner.run(payload, ctx)
        return self._settle_post(payload, ctx, result)


class BudgetGovernor(Governor):
    """Charges a per-phase ``BudgetMeter`` (parented to the global workflow meter)
    and, when enforcing, refuses to start an action that would breach the cap.

    The meter is created lazily on first run, parented to ``ctx.meter`` — so a
    phase cap (e.g. ENGINEERING_BUDGET) and the global 1,000,000 total are both
    enforced by one charge. ``estimate`` is the pre-flight cost guess; actual cost
    is taken from ``result.meta['usage']`` if the runner reported it."""

    def __init__(
        self,
        inner: Action,
        *,
        cap: int,
        estimate: int = 0,
        label: str = "",
        phase: str = "",
        enforce: bool = True,
    ) -> None:
        super().__init__(inner, enforce=enforce, name=inner.name)
        self._cap = int(cap)
        self._estimate = int(estimate)
        self._phase = phase
        self._label = label or f"budget:{inner.name}"
        self._meter: Optional[BudgetMeter] = None

    def meter(self, ctx: Context) -> BudgetMeter:
        # A ``phase`` key shares one sub-meter across the whole phase (created by
        # the first governor with that key); otherwise this governor owns a child.
        if self._phase:
            return ctx.phase_meter(self._phase, self._cap)
        if self._meter is None:
            self._meter = ctx.meter.child(self._cap, label=self._label)
        return self._meter

    def _enforce_pre(self, payload: Any, ctx: Context) -> Optional[Result]:
        if not self.enforce:
            return None
        if self._estimate and self.meter(ctx).would_exceed(self._estimate):
            m = self.meter(ctx)
            return Error(
                BudgetExceeded(
                    f"{self._label}: estimate {self._estimate} would exceed remaining {m.remaining}",
                    kind=self._inner.kind,
                    name=self._inner.name,
                ),
                detail="budget pre-flight abort",
                meta={"budget": m.to_dict()},
            )
        return None

    def _settle_post(self, payload: Any, ctx: Context, result: Result) -> Result:
        usage = int(result.meta.get("usage", self._estimate)) if result.ok else 0
        m = self.meter(ctx)
        m.charge(usage)
        # Annotate (frozen Result -> build a new one) so the trace shows spend.
        if result.ok:
            from .result import Output

            return Output(result.value, meta={**result.meta, "budget": m.to_dict()})
        return result


class PermissionGovernor(Governor):
    """Deny-by-default tool/operation gate. The set of permissions an action
    *requires* defaults to the wrapped action's ``spec.tools`` (an Inference
    declares its tool allowlist); a Program that spawns subagents must list the
    ``Agent`` tool or its subagent calls are denied. When enforcing, any required
    permission not in ``allow`` vetoes the action before it runs."""

    WILDCARD = "*"

    def __init__(
        self,
        inner: Action,
        *,
        allow: Iterable[str] = (),
        required: Optional[Callable[[Any, Context], Iterable[str]]] = None,
        enforce: bool = True,
    ) -> None:
        super().__init__(inner, enforce=enforce, name=inner.name)
        self._allow: Set[str] = set(allow)
        self._required_fn = required

    def _required(self, payload: Any, ctx: Context) -> Set[str]:
        if self._required_fn is not None:
            return set(self._required_fn(payload, ctx))
        spec = getattr(self._inner, "spec", None)
        return set(getattr(spec, "tools", ()) or ())

    def _enforce_pre(self, payload: Any, ctx: Context) -> Optional[Result]:
        if not self.enforce or self.WILDCARD in self._allow:
            return None
        missing = self._required(payload, ctx) - self._allow
        if missing:
            return Error(
                PermissionDenied(
                    f"deny-by-default: {sorted(missing)} not in allowlist {sorted(self._allow)}",
                    kind=self._inner.kind,
                    name=self._inner.name,
                ),
                detail="permission denied",
                meta={"missing": sorted(missing)},
            )
        return None


class IterationGovernor(Governor):
    """Bounds how many times the wrapped action may run across a workflow, keyed
    on a shared ``Context`` counter. When enforcing, the (cap+1)-th invocation is
    denied. (Loops also carry their own ``max_iterations``; this governor caps
    cumulative invocation even across separate loop entries.)"""

    def __init__(self, inner: Action, *, cap: int, key: str = "", enforce: bool = True) -> None:
        super().__init__(inner, enforce=enforce, name=inner.name)
        self._cap = int(cap)
        self._key = key or f"iter:{inner.name}"

    def _enforce_pre(self, payload: Any, ctx: Context) -> Optional[Result]:
        n = ctx.tick(self._key)
        if self.enforce and n > self._cap:
            from .result import ActionError

            return Error(
                ActionError(
                    f"iteration cap {self._cap} exceeded ({n}) for {self._inner.name}",
                    kind=self._inner.kind,
                    name=self._inner.name,
                ),
                detail="iteration cap exceeded",
                meta={"iterations": n, "cap": self._cap},
            )
        return None
