"""Posterior math for ranking (plan §10.2–§10.5). Pure functions, floats in / floats out.

Money is converted to ``float`` only inside these formulas (they are statistics, not
accounting); callers quantise results back to ``Decimal`` before storing (§0 rule 4).
No sampling here: ``p_beats_target`` uses closed-form CDFs, so results are exact and
reproducible. Monte Carlo (allocation, §10.7) lives in ``engine.ranking.allocation`` with a
fixed seed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy import stats as sps

A0_FLOOR = 0.1  # §10.3: if a0 < 0.1, set a0 = 0.1
CTR_FALLBACK = 0.01  # §10.1: CTR_parent when even the account prior is too small


@dataclass(frozen=True, slots=True)
class CtrPosterior:
    alpha: float
    beta: float
    mean: float
    p10: float
    p90: float


def ctr_posterior(impressions: float, clicks: float, m: float, k: float) -> CtrPosterior:
    """§10.2 Beta-Binomial. ``m`` = parent CTR, ``k`` = ctr_prior_impressions."""
    n = max(float(impressions), 0.0)
    c = min(max(float(clicks), 0.0), n)  # clamp c <= n
    m = min(max(m, 1e-9), 1 - 1e-9)  # keep both shape parameters > 0
    alpha = m * k + c
    beta = (1 - m) * k + n - c
    dist = sps.beta(alpha, beta)
    return CtrPosterior(
        alpha=alpha,
        beta=beta,
        mean=alpha / (alpha + beta),
        p10=float(dist.ppf(0.1)),
        p90=float(dist.ppf(0.9)),
    )


@dataclass(frozen=True, slots=True)
class ThetaPosterior:
    """Gamma posterior on conversions per currency unit (shape ``a``, rate ``b``)."""

    a: float
    b: float
    cpa_shrunk: float  # b / a
    cpa_p10: float  # optimistic CPA = 1 / theta_p90
    cpa_p90: float  # pessimistic CPA = 1 / theta_p10

    def theta_ppf(self, q: float) -> float:
        return float(sps.gamma.ppf(q, self.a, scale=1 / self.b))

    def theta_sf(self, x: float) -> float:
        """P(theta > x)."""
        return float(1 - sps.gamma.cdf(x, self.a, scale=1 / self.b))


def theta_posterior(
    conversions: float,
    spend: float,
    theta_parent: float,
    target_cpa: float,
    prior_spend_x_cpa: float,
) -> ThetaPosterior:
    """§10.3 Gamma-Poisson: prior weight b0 = prior_spend_x_cpa × target_cpa, a0 = θ_parent·b0."""
    v = max(float(conversions), 0.0)
    s = max(float(spend), 0.0)
    b0 = prior_spend_x_cpa * target_cpa
    a0 = max(theta_parent * b0, A0_FLOOR)
    a = a0 + v
    b = b0 + s
    th_p90 = float(sps.gamma.ppf(0.9, a, scale=1 / b))
    th_p10 = float(sps.gamma.ppf(0.1, a, scale=1 / b))
    return ThetaPosterior(
        a=a,
        b=b,
        cpa_shrunk=b / a,
        cpa_p10=1 / th_p90 if th_p90 > 0 else math.inf,
        cpa_p90=1 / th_p10 if th_p10 > 0 else math.inf,
    )


@dataclass(frozen=True, slots=True)
class RoasPosterior:
    aov_shrunk: float
    roas_shrunk: float
    roas_p10: float
    roas_p90: float


def roas_posterior(
    theta: ThetaPosterior,
    conversions: float,
    conv_value: float,
    aov_parent: float,
    aov_prior_conversions: float,
) -> RoasPosterior:
    """§10.4: AOV shrinkage, ROAS = θ × AOV (order-value variance ignored, known limitation)."""
    v = max(float(conversions), 0.0)
    w = max(float(conv_value), 0.0)
    aov = (w + aov_prior_conversions * aov_parent) / (v + aov_prior_conversions)
    return RoasPosterior(
        aov_shrunk=aov,
        roas_shrunk=(theta.a / theta.b) * aov,
        roas_p10=theta.theta_ppf(0.1) * aov,
        roas_p90=theta.theta_ppf(0.9) * aov,
    )


def p_beats_cpa(theta: ThetaPosterior, target_cpa: float) -> float:
    """§10.5 objective=conversions: P(CPA < target) = P(θ > 1/target)."""
    return theta.theta_sf(1 / target_cpa)


def p_beats_roas(theta: ThetaPosterior, target_roas: float, aov_shrunk: float) -> float:
    """§10.5 objective=value: P(ROAS > target) = P(θ > target_roas / AOV)."""
    if aov_shrunk <= 0:
        return 0.0
    return theta.theta_sf(target_roas / aov_shrunk)


def p_beats_ctr(ctr: CtrPosterior, ctr_parent: float) -> float:
    """§10.5 objective=traffic: P(CTR > CTR_parent)."""
    return float(1 - sps.beta.cdf(ctr_parent, ctr.alpha, ctr.beta))


def composite(p_beats_target: float, fatigue_flag: bool) -> int:
    """§10.5 display-only score: round(100 × p) − 10 if fatigued, floor 0. Never used in rules."""
    score = round(100 * p_beats_target) - (10 if fatigue_flag else 0)
    return max(score, 0)
