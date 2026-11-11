"""§12.6 guardrail invariants, property-tested with hypothesis against ``check``.

1. applying only Allow results keeps active daily budgets ≤ caps (per platform and total);
2. no budget unit moves more than ``max_budget_change_pct_per_day`` from its start-of-day budget;
3. kill switch on → only privileged, spend-reducing Allows;
4. no Allow for a learning (true/NULL) entity unless privileged;
5. no Allow for an unmanaged campaign's entities, ever;
6. ``check`` is deterministic.
"""

from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

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
    BudgetUnitSnapshot,
    EntitySnapshot,
    GuardrailState,
    ProposalView,
    check,
    derived_direction,
    is_privileged,
)
from engine.guardrails.limits_schema import LimitsConfig, parse_limits

NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)
BASE_LIMITS: dict[str, Any] = load_yaml(DEFAULT_LIMITS_PATH)
PRIVILEGED = (ProposalSource.EMERGENCY, ProposalSource.EVALUATOR)

money = st.decimals(min_value=0, max_value=20000, places=2, allow_nan=False, allow_infinity=False)
pos_money = st.decimals(
    min_value=Decimal("0.01"), max_value=20000, places=2, allow_nan=False, allow_infinity=False
)
pct = st.one_of(
    st.integers(-90, 200),
    st.decimals(min_value=-90, max_value=200, places=3, allow_nan=False, allow_infinity=False),
)
opt_dt = st.one_of(st.none(), st.integers(0, 24 * 30).map(lambda h: NOW - dt.timedelta(hours=h)))


@st.composite
def limits_st(draw: st.DrawFn) -> LimitsConfig:
    body = copy.deepcopy(BASE_LIMITS)
    ch = body["change"]
    ch["max_budget_change_pct_per_day"] = draw(st.integers(1, 60))
    ch["min_useful_change_pct"] = draw(st.integers(0, 10))
    ch["auto_max_budget_change_pct"] = draw(st.integers(1, 60))
    ch["auto_max_abs_change"] = draw(st.integers(1, 10000))
    body["campaign"]["min_daily_budget"] = draw(st.integers(1, 500))
    body["campaign"]["max_daily_budget"] = draw(st.integers(1000, 12000))
    body["approval_required_for"] = draw(
        st.lists(st.sampled_from(["new_campaign", "new_ad", "enable", "add_negative"]), unique=True)
    )
    return parse_limits(body)


def _limits_auto() -> LimitsConfig:
    body = copy.deepcopy(BASE_LIMITS)
    body["approval_required_for"] = []
    return parse_limits(body)


@st.composite
def entity_st(draw: st.DrawFn, et: EntityType | None = None) -> EntitySnapshot:
    entity_type = et or draw(st.sampled_from(list(EntityType)))
    budget = draw(st.one_of(st.none(), pos_money))
    return EntitySnapshot(
        platform=draw(st.sampled_from(list(Platform))),
        entity_type=entity_type,
        entity_id=1,
        campaign_id=1,
        campaign_managed=draw(st.booleans()),
        automation_scope=draw(st.sampled_from(list(AutomationScope))),
        budget_level=draw(st.sampled_from(list(BudgetLevel))),
        is_budget_unit=draw(st.booleans()),
        learning=draw(st.sampled_from([None, True, False])),
        status=draw(st.sampled_from(["active", "paused", None])),
        current_daily_budget=budget,
        start_of_day_budget=draw(st.one_of(st.none(), st.just(budget), pos_money)),
        active_ads_in_group=draw(st.one_of(st.none(), st.integers(0, 6))),
        last_applied_change_at=draw(opt_dt),
        last_manual_change_at=draw(opt_dt),
        budget_if_enabled=draw(st.one_of(st.none(), money)),
    )


@st.composite
def proposal_st(draw: st.DrawFn, e: EntitySnapshot) -> ProposalView:
    action = draw(st.sampled_from([a for a in Action if a is not Action.ESCALATE]))
    params: dict[str, Any] = {}
    if action is Action.BUDGET_CHANGE:
        params = {"change_pct": draw(st.one_of(pct, st.just("x"), st.none()))}
    if action is Action.NEW_CAMPAIGN:
        params = {"daily_budget": str(draw(money))}
    honest = derived_direction(action, params)
    return ProposalView(
        id=None,
        source=draw(st.sampled_from(list(ProposalSource))),
        rule_key="r",
        entity_type=e.entity_type if draw(st.integers(0, 9)) else EntityType.ACCOUNT,
        entity_id=e.entity_id,
        action=action,
        params=params,
        risk=draw(st.sampled_from(list(Risk))),
        spend_direction=honest
        if draw(st.integers(0, 9))
        else draw(st.sampled_from(list(SpendDirection))),
        from_pause_all=draw(st.booleans()),
    )


@st.composite
def case_st(draw: st.DrawFn) -> tuple[ProposalView, GuardrailState, bool]:
    e = draw(entity_st())
    p = draw(proposal_st(e))
    units = tuple(
        BudgetUnitSnapshot(draw(st.sampled_from(list(Platform))), EntityType.CAMPAIGN, i, b)
        for i, b in enumerate(draw(st.lists(pos_money, max_size=6)), start=100)
    )
    state = GuardrailState(
        mode=draw(st.sampled_from(list(Mode))),
        kill_switch_db=draw(st.booleans()),
        kill_switch_env=draw(st.booleans()),
        limits=draw(limits_st()),
        entity=e,
        active_budget_units=units,
        mtd_spend={pl: draw(money) for pl in Platform},
        remaining_days_in_month=draw(st.integers(1, 31)),
        auto_pauses_applied_today=draw(st.integers(0, 10)),
        now=NOW,
    )
    return p, state, draw(st.booleans())


# -------------------------------------------------------------------- invariants 3–6


@settings(max_examples=600, deadline=None)
@given(case_st())
def test_invariant_3_kill_switch(case: tuple[ProposalView, GuardrailState, bool]) -> None:
    p, state, approved = case
    r = check(p, state, approved=approved)
    if (state.kill_switch_db or state.kill_switch_env) and isinstance(r, Allow):
        assert is_privileged(p)
        assert derived_direction(p.action, p.params) is SpendDirection.REDUCING
        assert p.spend_direction is SpendDirection.REDUCING


@settings(max_examples=600, deadline=None)
@given(case_st())
def test_invariant_4_learning(case: tuple[ProposalView, GuardrailState, bool]) -> None:
    p, state, approved = case
    r = check(p, state, approved=approved)
    if state.entity.learning is not False and isinstance(r, Allow):
        # DECISIONS D-12: a human approval of a non-blocked action (e.g. add_negative on a
        # learning ad group) is the only non-privileged way through.
        assert is_privileged(p) or approved
    if state.entity.learning is not False and not approved:
        assert not isinstance(r, Allow) or is_privileged(p)


@settings(max_examples=600, deadline=None)
@given(case_st())
def test_invariant_5_unmanaged(case: tuple[ProposalView, GuardrailState, bool]) -> None:
    p, state, approved = case
    if not state.entity.campaign_managed:
        assert not isinstance(check(p, state, approved=approved), Allow)


@settings(max_examples=300, deadline=None)
@given(case_st())
def test_invariant_6_deterministic(case: tuple[ProposalView, GuardrailState, bool]) -> None:
    p, state, approved = case
    first = check(p, state, approved=approved)
    assert check(p, state, approved=approved) == first
    assert check(copy.deepcopy(p), copy.deepcopy(state), approved=approved) == first


# -------------------------------------------------------------------- invariants 1–2


@st.composite
def sequence_st(draw: st.DrawFn) -> tuple[list[dict[str, Any]], list[tuple[int, Any, bool]]]:
    """A within-caps account (2–6 budget units) and a sequence of proposals against it."""
    limits = _limits_auto()
    n = draw(st.integers(2, 6))
    units: list[dict[str, Any]] = []
    for i in range(n):
        pl = draw(st.sampled_from(list(Platform)))
        units.append(
            {
                "id": i,
                "platform": pl,
                "budget": draw(st.integers(300, 4000)),
                "managed": draw(st.booleans()),
                "active": draw(st.integers(0, 4)) > 0,
            }
        )
    for u in units:
        u["budget"] = Decimal(u["budget"])
        u["sod"] = u["budget"]
    # scale down so the starting state is within caps (invariant 1 is about not breaching)
    caps = limits.caps
    while True:
        by = {
            pl: sum((u["budget"] for u in units if u["active"] and u["platform"] is pl), Decimal(0))
            for pl in Platform
        }
        if (
            all(by[pl] <= caps.for_platform(pl).daily for pl in Platform)
            and sum(by.values()) <= caps.total.daily
        ):
            break
        for u in units:
            u["budget"] = u["sod"] = (u["budget"] / 2).quantize(Decimal("0.01"))
    steps = draw(
        st.lists(
            st.tuples(
                st.integers(0, n - 1),
                st.one_of(pct, st.just("enable")),
                st.booleans(),
            ),
            min_size=1,
            max_size=25,
        )
    )
    return units, steps


def _state(units: list[dict[str, Any]], u: dict[str, Any], mode: Mode) -> GuardrailState:
    return GuardrailState(
        mode=mode,
        kill_switch_db=False,
        kill_switch_env=False,
        limits=_limits_auto(),
        entity=EntitySnapshot(
            platform=u["platform"],
            entity_type=EntityType.CAMPAIGN,
            entity_id=u["id"],
            campaign_id=u["id"],
            campaign_managed=u["managed"],
            automation_scope=AutomationScope.FULL,
            budget_level=BudgetLevel.CAMPAIGN,
            is_budget_unit=True,
            learning=False,
            status="active" if u["active"] else "paused",
            current_daily_budget=u["budget"],
            start_of_day_budget=u["sod"],
            active_ads_in_group=None,
            last_applied_change_at=None,  # cooldown off: stress the arithmetic (worst case)
            last_manual_change_at=None,
            budget_if_enabled=u["budget"],
        ),
        active_budget_units=tuple(
            BudgetUnitSnapshot(x["platform"], EntityType.CAMPAIGN, x["id"], x["budget"])
            for x in units
            if x["active"]
        ),
        mtd_spend={pl: Decimal(0) for pl in Platform},
        remaining_days_in_month=1,
        auto_pauses_applied_today=0,
        now=NOW,
    )


@settings(max_examples=400, deadline=None)
@given(sequence_st(), st.sampled_from([m for m in Mode if m is not Mode.READ_ONLY]))
def test_invariants_1_and_2_sequences(
    seq: tuple[list[dict[str, Any]], list[tuple[int, Any, bool]]], mode: Mode
) -> None:
    units, steps = seq
    limits = _limits_auto()
    band = Decimal(limits.change.max_budget_change_pct_per_day) / 100
    for idx, change, approved in steps:
        u = units[idx]
        if change == "enable":
            action, params = Action.ENABLE, {}
        else:
            action, params = Action.BUDGET_CHANGE, {"change_pct": change}
        p = ProposalView(
            id=None,
            source=ProposalSource.RULE,
            rule_key="r",
            entity_type=EntityType.CAMPAIGN,
            entity_id=u["id"],
            action=action,
            params=params,
            risk=Risk.LOW,
            spend_direction=derived_direction(action, params),
        )
        r = check(p, _state(units, u, mode), approved=approved)
        if not isinstance(r, Allow):
            continue
        if action is Action.ENABLE:
            u["active"] = True
        else:
            assert r.new_daily_budget is not None
            u["budget"] = r.new_daily_budget
        # invariant 1
        by = {
            pl: sum((x["budget"] for x in units if x["active"] and x["platform"] is pl), Decimal(0))
            for pl in Platform
        }
        for pl in Platform:
            assert by[pl] <= limits.caps.for_platform(pl).daily
        assert sum(by.values()) <= limits.caps.total.daily
        # invariant 2
        for x in units:
            assert abs(x["budget"] - x["sod"]) <= x["sod"] * band
