"""Guardrail interface (plan §12.3). PURE function: no I/O, no clock, no randomness.

``check(proposal, state, *, approved=False) -> Allow | NeedsApproval | Block``

The 12 ordered checks of §12.3 are implemented below; §12.6 invariants are property-tested in
tests/property/test_guardrail_invariants.py. ``state`` is a plain snapshot built by
``engine.guardrails.state`` so ``check`` stays deterministic (§12.6 invariant 6).
Conservative extensions (all documented in docs/DECISIONS.md): ``spend_direction`` is
re-derived from the action and must match the row; invalid params block as
``unsupported_scope``; actions inside shared-budget campaigns block; ads/keywords inherit
their ad group's learning flag; learning entities never auto-apply (approval needed).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from engine.enums import (
    Action,
    AutomationScope,
    BudgetLevel,
    EntityType,
    Mode,
    Platform,
    ProposalSource,
    Risk,
    SpendDirection,
)
from engine.guardrails.limits_schema import LimitsConfig


class BlockReason(StrEnum):
    """Block reasons in §12.3 order."""

    MODE_READ_ONLY = "mode_read_only"  # 1
    KILL_SWITCH = "kill_switch"  # 2
    NOT_MANAGED = "not_managed"  # 3
    UNSUPPORTED_SCOPE = "unsupported_scope"  # 4
    COOLDOWN = "cooldown"  # 5
    LEARNING_PHASE = "learning_phase"  # 6
    MIN_ACTIVE_ADS = "min_active_ads"  # 7
    CHANGE_TOO_SMALL = "change_too_small"  # 8
    CAP_EXCEEDED = "cap_exceeded"  # 9


@dataclass(frozen=True, slots=True)
class ProposalView:
    """The fields of a ``proposals`` row the guardrail needs."""

    id: int | None
    source: ProposalSource
    rule_key: str | None
    entity_type: EntityType
    entity_id: int
    action: Action
    params: dict[str, Any]
    risk: Risk
    spend_direction: SpendDirection
    from_pause_all: bool = False  # action came from /control/pause-all (privileged, §12.3)


@dataclass(frozen=True, slots=True)
class EntitySnapshot:
    """The proposal's entity and its campaign, as of ``GuardrailState.now``."""

    platform: Platform
    entity_type: EntityType
    entity_id: int
    campaign_id: int
    campaign_managed: bool
    automation_scope: AutomationScope
    budget_level: BudgetLevel
    is_budget_unit: bool
    learning: bool | None  # NULL is treated as true (§8.2)
    status: str | None
    current_daily_budget: Decimal | None
    start_of_day_budget: Decimal | None  # latest entity_history at/before account-day start
    active_ads_in_group: int | None  # for ad pauses
    last_applied_change_at: dt.datetime | None  # latest change_log 'applied' for this entity
    last_manual_change_at: dt.datetime | None
    # spend that becomes active if this entity is enabled (its own budget if it is an inactive
    # budget unit; for a campaign with ad-set budgets, its active ad sets' budgets). Used by
    # check 9 for ``enable``. Added in Phase 2.
    budget_if_enabled: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BudgetUnitSnapshot:
    """One active budget unit (managed or not) for the budget-sum cap check (§12.3 step 9)."""

    platform: Platform
    entity_type: EntityType
    entity_id: int
    daily_budget: Decimal


@dataclass(frozen=True, slots=True)
class GuardrailState:
    mode: Mode
    kill_switch_db: bool
    kill_switch_env: bool
    limits: LimitsConfig
    entity: EntitySnapshot
    active_budget_units: tuple[BudgetUnitSnapshot, ...]
    mtd_spend: dict[Platform, Decimal]
    remaining_days_in_month: int  # account-local, including today
    auto_pauses_applied_today: int
    now: dt.datetime  # UTC; passed in so check() never reads the clock


@dataclass(frozen=True, slots=True)
class Allow:
    new_daily_budget: Decimal | None = None  # clamped value from step 8, if budget_change
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NeedsApproval:
    reasons: tuple[str, ...]
    new_daily_budget: Decimal | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Block:
    reason: BlockReason
    detail: dict[str, Any] = field(default_factory=dict)


GuardrailResult = Allow | NeedsApproval | Block


PRIVILEGED_SOURCES: frozenset[ProposalSource] = frozenset(
    {ProposalSource.EMERGENCY, ProposalSource.EVALUATOR}
)
CENT = Decimal("0.01")
ACTIVE = "active"
_ACTIONABLE: frozenset[Action] = frozenset(Action) - {Action.NOOP, Action.ESCALATE}


def is_privileged(p: ProposalView) -> bool:
    """§12.3: source ∈ {emergency, evaluator} or the action came from /control/pause-all."""
    return p.source in PRIVILEGED_SOURCES or p.from_pause_all


def change_pct(params: dict[str, Any]) -> Decimal | None:
    try:
        pct = Decimal(str(params["change_pct"]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None
    return pct if pct.is_finite() else None


def derived_direction(action: Action, params: dict[str, Any]) -> SpendDirection:
    """§12.3 ``spend_direction`` recomputed from the action (never trusted from the row)."""
    if action in (Action.PAUSE, Action.ADD_NEGATIVE):
        return SpendDirection.REDUCING
    if action is Action.BUDGET_CHANGE:
        pct = change_pct(params)
        if pct is None or pct == 0:
            return SpendDirection.NEUTRAL
        return SpendDirection.REDUCING if pct < 0 else SpendDirection.INCREASING
    if action in (Action.ENABLE, Action.NEW_AD, Action.NEW_CAMPAIGN):
        return SpendDirection.INCREASING
    return SpendDirection.NEUTRAL


def is_learning(entity: EntitySnapshot) -> bool:
    return entity.learning is None or entity.learning


def _floor2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_FLOOR)


def _ceil2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_CEILING)


def _scope_problem(  # noqa: PLR0911
    p: ProposalView, e: EntitySnapshot, direction: SpendDirection
) -> str | None:
    """§12.3 check 4 (+ conservative extras, DECISIONS D-10)."""
    if p.action not in _ACTIONABLE:
        return f"action {p.action.value} is not executable"
    if p.entity_type is not e.entity_type or p.entity_id != e.entity_id:
        return "proposal and entity snapshot disagree"
    if p.entity_type is EntityType.ACCOUNT:
        return "account-level actions are not supported"
    if p.spend_direction is not direction:
        return f"spend_direction {p.spend_direction.value} != derived {direction.value}"
    if e.automation_scope is AutomationScope.CAMPAIGN_ONLY and e.entity_type is not (
        EntityType.CAMPAIGN
    ):
        return "campaign_only automation scope: only the campaign may be changed"
    if e.budget_level is BudgetLevel.SHARED:
        return "campaign uses a shared budget (not supported in v1)"
    if p.action is Action.BUDGET_CHANGE:
        if not e.is_budget_unit:
            return "budget_change on an entity that is not a budget unit"
        if e.current_daily_budget is None or e.current_daily_budget <= 0:
            return "budget unit has no current daily budget"
        pct = change_pct(p.params)
        if pct is None or pct == 0:
            return "budget_change needs a non-zero numeric params.change_pct"
    return None


def _in_cooldown(e: EntitySnapshot, limits: LimitsConfig, now: dt.datetime) -> str | None:
    cd = limits.cooldowns
    if e.last_applied_change_at is not None and now - e.last_applied_change_at < dt.timedelta(
        days=cd.same_entity_days
    ):
        return f"engine change applied at {e.last_applied_change_at.isoformat()}"
    if e.last_manual_change_at is not None and now - e.last_manual_change_at < dt.timedelta(
        hours=cd.after_manual_change_hours
    ):
        return f"manual change at {e.last_manual_change_at.isoformat()}"
    return None


def _new_budget(
    p: ProposalView, e: EntitySnapshot, limits: LimitsConfig
) -> tuple[Decimal | None, dict[str, Any]]:
    """§12.3 check 8. Returns (new budget, detail) or (None, detail) when too small.

    Bounds are rounded inward (band floor/ceil to the cent) so the 2-dp result can never
    leave the ±max_budget_change_pct_per_day band (§12.6 invariant 2). If the band and
    [min, max] daily budget don't overlap, or clamping flips/erases the change, the
    proposal is blocked as ``change_too_small`` (DECISIONS D-11).
    """
    current = e.current_daily_budget
    pct = change_pct(p.params)
    assert current is not None and pct is not None  # guaranteed by check 4
    sod = e.start_of_day_budget if e.start_of_day_budget is not None else current
    band = Decimal(limits.change.max_budget_change_pct_per_day) / 100
    lo = max(_ceil2(sod * (1 - band)), _ceil2(limits.campaign.min_daily_budget))
    hi = min(_floor2(sod * (1 + band)), _floor2(limits.campaign.max_daily_budget))
    detail: dict[str, Any] = {
        "current": str(current),
        "start_of_day_budget": str(sod),
        "requested_change_pct": str(pct),
        "lower_bound": str(lo),
        "upper_bound": str(hi),
    }
    if lo > hi:
        detail["why"] = "daily change band and min/max daily budget do not overlap"
        return None, detail
    raw = current * (1 + pct / 100)
    new = min(max(raw.quantize(CENT, rounding=ROUND_HALF_EVEN), lo), hi)
    detail["new"] = str(new)
    delta = new - current
    if delta == 0 or (delta > 0) != (pct > 0):
        detail["why"] = "clamping removed or reversed the change"
        return None, detail
    min_useful = Decimal(limits.change.min_useful_change_pct) / 100
    if abs(delta) / current < min_useful:
        detail["why"] = "change below min_useful_change_pct"
        return None, detail
    return new, detail


def _cap_problem(
    p: ProposalView, state: GuardrailState, new_budget: Decimal | None
) -> dict[str, Any] | None:
    """§12.3 check 9: budget-sum (daily) and MTD projection (monthly) caps after the change."""
    e = state.entity
    units: dict[tuple[EntityType, int], BudgetUnitSnapshot] = {
        (u.entity_type, u.entity_id): u for u in state.active_budget_units
    }
    key = (e.entity_type, e.entity_id)
    if p.action is Action.BUDGET_CHANGE and new_budget is not None:
        # an inactive unit still gets counted at its new budget (conservative)
        units[key] = BudgetUnitSnapshot(e.platform, e.entity_type, e.entity_id, new_budget)
    extra = Decimal(0)
    if p.action is Action.ENABLE:
        extra = e.budget_if_enabled or Decimal(0)
    elif p.action is Action.NEW_CAMPAIGN:
        try:
            extra = Decimal(str(p.params.get("daily_budget", 0)))
        except (InvalidOperation, TypeError, ValueError):
            return {"why": "new_campaign daily_budget unreadable"}
        if not extra.is_finite() or extra < 0:
            return {"why": "new_campaign daily_budget invalid"}
    by_platform: dict[Platform, Decimal] = {pl: Decimal(0) for pl in Platform}
    for u in units.values():
        by_platform[u.platform] += u.daily_budget
    by_platform[e.platform] += extra
    total = sum(by_platform.values(), Decimal(0))
    caps = state.limits.caps
    days = Decimal(max(state.remaining_days_in_month, 0))
    b_p = by_platform[e.platform]
    cap_p = caps.for_platform(e.platform)
    mtd_p = state.mtd_spend.get(e.platform, Decimal(0))
    mtd_total = sum(state.mtd_spend.values(), Decimal(0))
    problems: list[str] = []
    if b_p > cap_p.daily:
        problems.append(f"{e.platform.value} daily budgets {b_p} > cap {cap_p.daily}")
    if total > caps.total.daily:
        problems.append(f"total daily budgets {total} > cap {caps.total.daily}")
    if mtd_p + b_p * days > cap_p.monthly:
        problems.append(f"{e.platform.value} MTD {mtd_p} + {b_p}×{days} > cap {cap_p.monthly}")
    if mtd_total + total * days > caps.total.monthly:
        problems.append(f"total MTD {mtd_total} + {total}×{days} > cap {caps.total.monthly}")
    if problems:
        return {"why": "; ".join(problems)}
    return None


def check(  # noqa: PLR0911, PLR0912 (ordered §12.3 checks)
    proposal: ProposalView, state: GuardrailState, *, approved: bool = False
) -> GuardrailResult:
    """Run §12.3 checks 1–12 in order; first Block wins. ``approved=True`` skips steps 10–11."""
    p, e, limits = proposal, state.entity, state.limits
    privileged = is_privileged(p)
    direction = derived_direction(p.action, p.params)

    # 1. mode
    if state.mode is Mode.READ_ONLY:
        return Block(BlockReason.MODE_READ_ONLY)
    # 2. kill switch (DB or env): only privileged spend-reducing actions pass
    if (state.kill_switch_db or state.kill_switch_env) and not (
        privileged and direction is SpendDirection.REDUCING
    ):
        return Block(
            BlockReason.KILL_SWITCH, {"db": state.kill_switch_db, "env": state.kill_switch_env}
        )
    # 3. managed
    if not e.campaign_managed:
        return Block(BlockReason.NOT_MANAGED, {"campaign_id": e.campaign_id})
    # 4. scope
    scope = _scope_problem(p, e, direction)
    if scope is not None:
        return Block(BlockReason.UNSUPPORTED_SCOPE, {"why": scope})
    # 5. cooldown
    if not privileged:
        why = _in_cooldown(e, limits, state.now)
        if why is not None:
            return Block(BlockReason.COOLDOWN, {"why": why})
    # 6. learning phase (NULL = learning). Applies to the entity's own learning flag; ads and
    #    keywords carry their ad group's (DECISIONS D-12).
    if (
        not privileged
        and is_learning(e)
        and (
            (p.action is Action.BUDGET_CHANGE and limits.learning_phase.block_budget_changes)
            or (p.action is Action.PAUSE and limits.learning_phase.block_pauses)
        )
    ):
        return Block(BlockReason.LEARNING_PHASE, {"learning": e.learning})
    # 7. keep a minimum of active ads per group
    if p.action is Action.PAUSE and e.entity_type is EntityType.AD:
        active = e.active_ads_in_group
        if active is None:
            return Block(BlockReason.MIN_ACTIVE_ADS, {"why": "active ad count unknown"})
        remaining = active - (1 if e.status == ACTIVE else 0)
        if remaining < limits.change.keep_min_active_ads_per_group:
            return Block(
                BlockReason.MIN_ACTIVE_ADS,
                {
                    "active_after": remaining,
                    "keep_min": limits.change.keep_min_active_ads_per_group,
                },
            )
    # 8. budget arithmetic
    new_budget: Decimal | None = None
    budget_detail: dict[str, Any] = {}
    if p.action is Action.BUDGET_CHANGE:
        new_budget, budget_detail = _new_budget(p, e, limits)
        if new_budget is None:
            return Block(BlockReason.CHANGE_TOO_SMALL, budget_detail)
    # 9. caps (only for spend-increasing actions)
    if direction is SpendDirection.INCREASING:
        cap = _cap_problem(p, state, new_budget)
        if cap is not None:
            return Block(BlockReason.CAP_EXCEEDED, cap)
    if approved:
        return Allow(new_budget, {**budget_detail, "approved": True})
    # 10. suggest mode: everything needs a human
    if state.mode is Mode.SUGGEST:
        return NeedsApproval(("mode_suggest",), new_budget, budget_detail)
    # 11. approval triggers
    reasons: list[str] = []
    if p.action in limits.approval_required_for:
        reasons.append(f"approval_required_for:{p.action.value}")
    if p.risk is Risk.HIGH:
        reasons.append("risk_high")
    if p.risk is Risk.MEDIUM and state.mode is Mode.AUTO_SMALL:
        reasons.append("risk_medium_in_auto_small")
    if p.action is Action.BUDGET_CHANGE and new_budget is not None:
        current = e.current_daily_budget
        assert current is not None
        requested = change_pct(p.params) or Decimal(0)
        effective = (new_budget - current) / current * 100
        auto_pct = Decimal(limits.change.auto_max_budget_change_pct)
        if abs(requested) > auto_pct or abs(effective) > auto_pct:
            reasons.append("change_pct_above_auto_max")
        if abs(new_budget - current) > limits.change.auto_max_abs_change:
            reasons.append("abs_change_above_auto_max")
    if (
        p.action is Action.PAUSE
        and state.auto_pauses_applied_today >= limits.change.max_auto_pauses_per_day
    ):
        reasons.append("max_auto_pauses_per_day")
    if not privileged and is_learning(e):
        reasons.append("entity_learning")  # DECISIONS D-12: learning entities never auto-apply
    if reasons:
        return NeedsApproval(tuple(reasons), new_budget, budget_detail)
    # 12.
    return Allow(new_budget, budget_detail)


def result_json(
    result: GuardrailResult, *, approved: bool, mode: Mode, checked_at: dt.datetime
) -> dict[str, Any]:
    """Serialisable form stored in ``proposals.guardrail_result``."""
    base: dict[str, Any] = {
        "checked_at": checked_at.isoformat(),
        "mode": mode.value,
        "approved": approved,
    }
    if isinstance(result, Block):
        return {**base, "result": "block", "reason": result.reason.value, "detail": result.detail}
    nb = str(result.new_daily_budget) if result.new_daily_budget is not None else None
    if isinstance(result, NeedsApproval):
        return {
            **base,
            "result": "needs_approval",
            "reasons": list(result.reasons),
            "new_daily_budget": nb,
            "detail": result.detail,
        }
    return {**base, "result": "allow", "new_daily_budget": nb, "detail": result.detail}
