"""Pure evaluation math (plan §16.2, §16.3). No I/O; deterministic given the seed."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from engine.enums import Verdict

D3_WORSE_SPEND_X_CPA = Decimal(3)  # §16.2: spend_post ≥ 3 × target_cpa with 0 conversions
D3_DELIVERY_COLLAPSE_RATIO = Decimal("0.5")  # §16.2: avg daily spend after < 50% of before
INSUFFICIENT_SPEND_X_CPA = Decimal(2)  # §16.3: s_post < 2 × target_cpa
CONTROL_MIN_SPEND_X_CPA = Decimal(3)  # §16.3: control period spend floor for g
WORSE_RATIO = 1 / 1.15  # §16.3: CPA more than 15% worse
BETTER_RATIO = 1.05
P_WORSE = 0.8
P_BETTER = 0.7
PRIOR_SHAPE = 0.5


@dataclass(frozen=True, slots=True)
class Totals:
    spend: Decimal = Decimal(0)
    conversions: Decimal = Decimal(0)
    conv_value: Decimal = Decimal(0)

    def as_json(self) -> dict[str, str]:
        return {
            "spend": str(self.spend),
            "conversions": str(self.conversions),
            "conv_value": str(self.conv_value),
        }


@dataclass(frozen=True, slots=True)
class D3Result:
    worse: bool
    delivery_collapsed: bool


def d3_check(
    post: Totals,
    *,
    target_cpa: Decimal,
    budget_increase: bool,
    pre_avg_daily: Decimal,
    post_days: int,
) -> D3Result:
    worse = post.spend >= D3_WORSE_SPEND_X_CPA * target_cpa and post.conversions == 0
    collapsed = (
        budget_increase
        and pre_avg_daily > 0
        and post.spend / post_days < D3_DELIVERY_COLLAPSE_RATIO * pre_avg_daily
    )
    return D3Result(worse, bool(collapsed) and not worse)


def insufficient(pre: Totals, post: Totals, target_cpa: Decimal) -> bool:
    """§16.3 ``s_post < 2 × target_cpa``; also no pre-period spend (nothing to compare, D-34)."""
    return post.spend < INSUFFICIENT_SPEND_X_CPA * target_cpa or pre.spend <= 0


def trend_factor(ctrl_pre: Totals, ctrl_post: Totals, target_cpa: Decimal) -> float:
    floor = CONTROL_MIN_SPEND_X_CPA * target_cpa
    if (
        ctrl_pre.spend < floor
        or ctrl_post.spend < floor
        or ctrl_pre.conversions <= 0
        or ctrl_post.conversions <= 0
    ):
        return 1.0
    return float(
        (ctrl_post.conversions / ctrl_post.spend) / (ctrl_pre.conversions / ctrl_pre.spend)
    )


@dataclass(frozen=True, slots=True)
class D7Result:
    verdict: Verdict
    p_worse: float
    p_better: float
    g: float


def d7_verdict(
    pre: Totals,
    post: Totals,
    ctrl_pre: Totals,
    ctrl_post: Totals,
    *,
    target_cpa: Decimal,
    objective: str,
    draws: int,
    seed: int,
) -> D7Result:
    """Gamma-Poisson Monte Carlo of the efficiency ratio ``r = θ_post / (θ_pre × g)``."""
    g = trend_factor(ctrl_pre, ctrl_post, target_cpa)
    rng = np.random.default_rng(seed=seed)
    th_pre = rng.gamma(PRIOR_SHAPE + float(pre.conversions), 1.0 / float(pre.spend), draws)
    th_post = rng.gamma(PRIOR_SHAPE + float(post.conversions), 1.0 / float(post.spend), draws)
    if objective == "value":
        if pre.conversions > 0:
            th_pre = th_pre * float(pre.conv_value / pre.conversions)
        if post.conversions > 0:
            th_post = th_post * float(post.conv_value / post.conversions)
    r = th_post / (th_pre * g)
    p_worse = float(np.mean(r < WORSE_RATIO))
    p_better = float(np.mean(r > BETTER_RATIO))
    if p_worse > P_WORSE:
        verdict = Verdict.WORSE
    elif p_better > P_BETTER:
        verdict = Verdict.BETTER
    else:
        verdict = Verdict.NEUTRAL
    return D7Result(verdict, p_worse, p_better, g)
