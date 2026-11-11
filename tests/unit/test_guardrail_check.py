"""§12.3 checks 1–12: every block reason, ordering, budget arithmetic and approval triggers."""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from engine.bootstrap import DEFAULT_LIMITS_PATH, load_yaml
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
from engine.guardrails.check import (
    Allow,
    Block,
    BlockReason,
    BudgetUnitSnapshot,
    EntitySnapshot,
    GuardrailState,
    NeedsApproval,
    ProposalView,
    check,
    derived_direction,
)
from engine.guardrails.limits_schema import parse_limits

NOW = dt.datetime(2026, 11, 11, 6, tzinfo=dt.UTC)
LIMITS = parse_limits(load_yaml(DEFAULT_LIMITS_PATH))
ENTITY = EntitySnapshot(
    platform=Platform.META,
    entity_type=EntityType.CAMPAIGN,
    entity_id=1,
    campaign_id=1,
    campaign_managed=True,
    automation_scope=AutomationScope.FULL,
    budget_level=BudgetLevel.CAMPAIGN,
    is_budget_unit=True,
    learning=False,
    status="active",
    current_daily_budget=Decimal("1000"),
    start_of_day_budget=Decimal("1000"),
    active_ads_in_group=None,
    last_applied_change_at=None,
    last_manual_change_at=None,
)
STATE = GuardrailState(
    mode=Mode.AUTO,
    kill_switch_db=False,
    kill_switch_env=False,
    limits=LIMITS,
    entity=ENTITY,
    active_budget_units=(BudgetUnitSnapshot(Platform.META, EntityType.CAMPAIGN, 1, Decimal(1000)),),
    mtd_spend={Platform.META: Decimal(0), Platform.GOOGLE: Decimal(0)},
    remaining_days_in_month=20,
    auto_pauses_applied_today=0,
    now=NOW,
)


def prop(action: Action = Action.BUDGET_CHANGE, **kw: Any) -> ProposalView:
    params = kw.pop("params", {"change_pct": 10} if action is Action.BUDGET_CHANGE else {})
    base: dict[str, Any] = {
        "id": None,
        "source": ProposalSource.RULE,
        "rule_key": "r",
        "entity_type": EntityType.CAMPAIGN,
        "entity_id": 1,
        "action": action,
        "params": params,
        "risk": Risk.LOW,
        "spend_direction": derived_direction(action, params),
    }
    base.update(kw)
    return ProposalView(**base)


def state(**kw: Any) -> GuardrailState:
    entity = dataclasses.replace(ENTITY, **kw.pop("entity", {}))
    return dataclasses.replace(STATE, entity=entity, **kw)


def reason(p: ProposalView, s: GuardrailState, **kw: Any) -> BlockReason | None:
    r = check(p, s, **kw)
    return r.reason if isinstance(r, Block) else None


def test_baseline_allow() -> None:
    r = check(prop(), STATE)
    assert isinstance(r, Allow) and r.new_daily_budget == Decimal("1100.00")


@pytest.mark.parametrize(
    ("p", "s", "expected"),
    [
        (prop(), state(mode=Mode.READ_ONLY), BlockReason.MODE_READ_ONLY),
        (prop(), state(kill_switch_db=True), BlockReason.KILL_SWITCH),
        (prop(), state(kill_switch_env=True), BlockReason.KILL_SWITCH),
        (prop(Action.PAUSE), state(kill_switch_env=True), BlockReason.KILL_SWITCH),  # not priv.
        (prop(), state(entity={"campaign_managed": False}), BlockReason.NOT_MANAGED),
        (prop(entity_type=EntityType.ACCOUNT), STATE, BlockReason.UNSUPPORTED_SCOPE),
        (prop(spend_direction=SpendDirection.REDUCING), STATE, BlockReason.UNSUPPORTED_SCOPE),
        (prop(params={"change_pct": "x"}), STATE, BlockReason.UNSUPPORTED_SCOPE),
        (prop(), state(entity={"budget_level": BudgetLevel.SHARED}), BlockReason.UNSUPPORTED_SCOPE),
        (prop(), state(entity={"is_budget_unit": False}), BlockReason.UNSUPPORTED_SCOPE),
        (
            prop(entity_type=EntityType.AD_GROUP),
            state(
                entity={
                    "entity_type": EntityType.AD_GROUP,
                    "automation_scope": AutomationScope.CAMPAIGN_ONLY,
                }
            ),
            BlockReason.UNSUPPORTED_SCOPE,
        ),
        (
            prop(),
            state(entity={"last_applied_change_at": NOW - dt.timedelta(days=6)}),
            BlockReason.COOLDOWN,
        ),
        (
            prop(),
            state(entity={"last_manual_change_at": NOW - dt.timedelta(hours=47)}),
            BlockReason.COOLDOWN,
        ),
        (prop(), state(entity={"learning": True}), BlockReason.LEARNING_PHASE),
        (prop(), state(entity={"learning": None}), BlockReason.LEARNING_PHASE),  # NULL = learning
        (prop(Action.PAUSE), state(entity={"learning": None}), BlockReason.LEARNING_PHASE),
        (
            prop(Action.PAUSE, entity_type=EntityType.AD),
            state(entity={"entity_type": EntityType.AD, "active_ads_in_group": 2}),
            BlockReason.MIN_ACTIVE_ADS,
        ),
        (
            prop(Action.PAUSE, entity_type=EntityType.AD),
            state(entity={"entity_type": EntityType.AD, "active_ads_in_group": None}),
            BlockReason.MIN_ACTIVE_ADS,
        ),
        (prop(params={"change_pct": 2}), STATE, BlockReason.CHANGE_TOO_SMALL),
        (  # already +20% today: band exhausted, clamp erases the change
            prop(),
            state(entity={"current_daily_budget": Decimal(1200)}),
            BlockReason.CHANGE_TOO_SMALL,
        ),
        (  # cut below min_daily_budget band → clamp flips nothing but yields < min useful
            prop(params={"change_pct": -50}),
            state(
                entity={"current_daily_budget": Decimal(310), "start_of_day_budget": Decimal(310)}
            ),
            BlockReason.CHANGE_TOO_SMALL,
        ),
        (
            prop(),
            state(
                active_budget_units=(
                    BudgetUnitSnapshot(Platform.META, EntityType.CAMPAIGN, 1, Decimal(1000)),
                    BudgetUnitSnapshot(Platform.META, EntityType.CAMPAIGN, 2, Decimal(10950)),
                )
            ),
            BlockReason.CAP_EXCEEDED,
        ),
        (  # monthly projection: MTD + budgets × remaining days
            prop(),
            state(mtd_spend={Platform.META: Decimal(310000), Platform.GOOGLE: Decimal(0)}),
            BlockReason.CAP_EXCEEDED,
        ),
        (
            prop(Action.ENABLE),
            state(entity={"budget_if_enabled": Decimal(11500)}),
            BlockReason.CAP_EXCEEDED,
        ),
    ],
)
def test_block_reasons(p: ProposalView, s: GuardrailState, expected: BlockReason) -> None:
    assert reason(p, s) is expected


def test_order_first_block_wins() -> None:
    s = state(mode=Mode.READ_ONLY, kill_switch_db=True, entity={"campaign_managed": False})
    assert reason(prop(), s) is BlockReason.MODE_READ_ONLY
    s = state(kill_switch_db=True, entity={"campaign_managed": False})
    assert reason(prop(), s) is BlockReason.KILL_SWITCH
    s = state(entity={"campaign_managed": False, "learning": True})
    assert reason(prop(), s) is BlockReason.NOT_MANAGED


def test_privileged_paths() -> None:
    pause = prop(Action.PAUSE, source=ProposalSource.EMERGENCY)
    s = state(kill_switch_env=True, entity={"learning": True})
    assert isinstance(check(pause, s), Allow)  # privileged reducing passes kill + learning
    assert isinstance(check(prop(Action.PAUSE, from_pause_all=True), s), Allow)
    up = prop(source=ProposalSource.EMERGENCY)
    assert reason(up, state(kill_switch_db=True)) is BlockReason.KILL_SWITCH
    cooled = state(entity={"last_applied_change_at": NOW - dt.timedelta(hours=1)})
    assert isinstance(check(prop(Action.PAUSE, source=ProposalSource.EVALUATOR), cooled), Allow)


def test_budget_clamp_to_band() -> None:
    r = check(prop(params={"change_pct": 50}), STATE)
    assert isinstance(r, NeedsApproval)  # 50% requested > auto max 15%
    assert r.new_daily_budget == Decimal("1200.00")  # clamped to +20% of start-of-day
    assert "change_pct_above_auto_max" in r.reasons
    r2 = check(prop(params={"change_pct": -50}), STATE)
    assert isinstance(r2, NeedsApproval) and r2.new_daily_budget == Decimal("800.00")


def test_approval_triggers_and_suggest() -> None:
    assert check(prop(), state(mode=Mode.SUGGEST)) == NeedsApproval(
        ("mode_suggest",),
        Decimal("1100.00"),
        check(prop(), STATE).detail,
    )
    r = check(
        prop(Action.ADD_NEGATIVE, entity_type=EntityType.AD_GROUP),
        state(entity={"entity_type": EntityType.AD_GROUP, "is_budget_unit": False}),
    )
    assert isinstance(r, NeedsApproval) and r.reasons == ("approval_required_for:add_negative",)
    assert isinstance(check(prop(risk=Risk.HIGH), STATE), NeedsApproval)
    r = check(prop(risk=Risk.MEDIUM), state(mode=Mode.AUTO_SMALL))
    assert isinstance(r, NeedsApproval) and "risk_medium_in_auto_small" in r.reasons
    r = check(prop(Action.PAUSE), state(auto_pauses_applied_today=5))
    assert isinstance(r, NeedsApproval) and r.reasons == ("max_auto_pauses_per_day",)
    big = state(
        entity={"current_daily_budget": Decimal(8000), "start_of_day_budget": Decimal(8000)}
    )
    body = load_yaml(DEFAULT_LIMITS_PATH)
    body["change"]["auto_max_abs_change"] = 1000
    r = check(prop(params={"change_pct": -15}), dataclasses.replace(big, limits=parse_limits(body)))
    assert isinstance(r, NeedsApproval) and "abs_change_above_auto_max" in r.reasons
    # learning ad group: add_negative is not blocked by check 6 but never auto-applies (D-12)
    lg = state(
        entity={"entity_type": EntityType.AD_GROUP, "is_budget_unit": False, "learning": True}
    )
    r = check(prop(Action.ADD_NEGATIVE, entity_type=EntityType.AD_GROUP), lg)
    assert isinstance(r, NeedsApproval) and "entity_learning" in r.reasons
    # approved=True skips steps 10–11 but never the blocks
    assert isinstance(check(prop(risk=Risk.HIGH), state(mode=Mode.SUGGEST), approved=True), Allow)
    assert reason(prop(), state(mode=Mode.READ_ONLY), approved=True) is BlockReason.MODE_READ_ONLY


def test_reducing_actions_skip_caps() -> None:
    over = state(mtd_spend={Platform.META: Decimal(10**7), Platform.GOOGLE: Decimal(0)})
    r = check(prop(params={"change_pct": -10}), over)
    assert isinstance(r, Allow) and r.new_daily_budget == Decimal("900.00")
