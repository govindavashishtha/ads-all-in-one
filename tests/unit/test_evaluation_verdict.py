"""Pure evaluation math (plan §16.2, §16.3)."""

from __future__ import annotations

from decimal import Decimal

from engine.enums import Verdict
from engine.evaluation.verdict import Totals, d3_check, d7_verdict, insufficient, trend_factor

CPA = Decimal(500)


def T(spend: float, conv: float, value: float = 0) -> Totals:
    return Totals(Decimal(str(spend)), Decimal(str(conv)), Decimal(str(value)))


def test_d3_worse_needs_three_cpas_and_zero_conversions() -> None:
    assert d3_check(
        T(1500, 0), target_cpa=CPA, budget_increase=False, pre_avg_daily=Decimal(0), post_days=3
    ).worse
    assert not d3_check(
        T(1499, 0), target_cpa=CPA, budget_increase=False, pre_avg_daily=Decimal(0), post_days=3
    ).worse
    assert not d3_check(
        T(5000, 1), target_cpa=CPA, budget_increase=False, pre_avg_daily=Decimal(0), post_days=3
    ).worse


def test_d3_delivery_collapse_only_for_budget_increase() -> None:
    kw = {"target_cpa": CPA, "pre_avg_daily": Decimal(1000), "post_days": 3}
    assert d3_check(T(300, 2), budget_increase=True, **kw).delivery_collapsed  # type: ignore[arg-type]
    assert not d3_check(T(300, 2), budget_increase=False, **kw).delivery_collapsed  # type: ignore[arg-type]
    assert not d3_check(T(1500, 2), budget_increase=True, **kw).delivery_collapsed  # type: ignore[arg-type]


def test_insufficient_data() -> None:
    assert insufficient(T(7000, 20), T(999, 1), CPA)  # s_post < 2 × CPA
    assert not insufficient(T(7000, 20), T(1000, 1), CPA)
    assert insufficient(T(0, 0), T(5000, 5), CPA)  # no pre-period spend (D-34)


def test_trend_factor_falls_back_to_one() -> None:
    assert trend_factor(T(1000, 1), T(1000, 1), CPA) == 1.0  # control spend < 3 × CPA
    assert trend_factor(T(5000, 0), T(5000, 5), CPA) == 1.0  # zero conversions
    assert trend_factor(T(5000, 10), T(5000, 5), CPA) == 0.5


def test_d7_verdicts_and_determinism() -> None:
    ctrl = T(0, 0)
    kw = {"target_cpa": CPA, "objective": "conversions", "draws": 10_000}
    worse = d7_verdict(T(7000, 28), T(7000, 7), ctrl, ctrl, seed=42, **kw)  # type: ignore[arg-type]
    better = d7_verdict(T(7000, 7), T(7000, 28), ctrl, ctrl, seed=42, **kw)  # type: ignore[arg-type]
    same = d7_verdict(T(7000, 14), T(7000, 14), ctrl, ctrl, seed=42, **kw)  # type: ignore[arg-type]
    assert worse.verdict is Verdict.WORSE and worse.p_worse > 0.8
    assert better.verdict is Verdict.BETTER and better.p_better > 0.7
    assert same.verdict is Verdict.NEUTRAL
    again = d7_verdict(T(7000, 14), T(7000, 14), ctrl, ctrl, seed=42, **kw)  # type: ignore[arg-type]
    assert (again.p_worse, again.p_better) == (same.p_worse, same.p_better)  # seeded


def test_d7_control_trend_explains_a_market_wide_drop() -> None:
    kw = {"target_cpa": CPA, "objective": "conversions", "draws": 10_000, "seed": 7}
    # unit halved, but so did the control market: not the change's fault
    r = d7_verdict(T(7000, 28), T(7000, 14), T(20000, 80), T(20000, 40), **kw)  # type: ignore[arg-type]
    assert r.g == 0.5 and r.verdict is not Verdict.WORSE


def test_d7_value_objective_uses_order_value() -> None:
    kw = {"target_cpa": CPA, "objective": "value", "draws": 10_000, "seed": 1}
    # same conversion rate, order value tripled → better on value
    r = d7_verdict(T(7000, 14, 14000), T(7000, 14, 42000), T(0, 0), T(0, 0), **kw)  # type: ignore[arg-type]
    assert r.verdict is Verdict.BETTER
