"""§10.2–§10.5 posterior formulas against hand-computed values (≥3 cases each)."""

from __future__ import annotations

import math

import pytest

from engine.ranking.posteriors import (
    composite,
    ctr_posterior,
    p_beats_cpa,
    p_beats_ctr,
    p_beats_roas,
    roas_posterior,
    theta_posterior,
)

approx = pytest.approx


@pytest.mark.parametrize(
    ("imps", "clicks", "m", "k", "alpha", "beta"),
    [
        (1000, 30, 0.02, 500, 40.0, 1460.0),  # 0.02·500+30, 0.98·500+970
        (0, 0, 0.01, 500, 5.0, 495.0),  # no data → the prior
        (10, 20, 0.01, 100, 11.0, 99.0),  # clicks clamped to impressions
    ],
)
def test_ctr_posterior(
    imps: int, clicks: int, m: float, k: float, alpha: float, beta: float
) -> None:
    p = ctr_posterior(imps, clicks, m, k)
    assert p.alpha == approx(alpha)
    assert p.beta == approx(beta)
    assert p.mean == approx(alpha / (alpha + beta))
    assert p.p10 < p.mean < p.p90


@pytest.mark.parametrize(
    ("conv", "spend", "theta_parent", "target", "x", "a", "b"),
    [
        (10, 5000, 1 / 500, 500, 2, 12.0, 6000.0),  # b0=1000, a0=2
        (0, 0, 1e-5, 500, 2, 0.1, 1000.0),  # a0 = 0.01 → floored to 0.1
        (5, 1000, 0.004, 250, 4, 9.0, 2000.0),  # b0=1000, a0=4
    ],
)
def test_theta_posterior(
    conv: float, spend: float, theta_parent: float, target: float, x: float, a: float, b: float
) -> None:
    t = theta_posterior(conv, spend, theta_parent, target, x)
    assert t.a == approx(a)
    assert t.b == approx(b)
    assert t.cpa_shrunk == approx(b / a)
    assert t.cpa_p10 < t.cpa_shrunk < t.cpa_p90


def test_theta_exponential_case_closed_form() -> None:
    # a = 1 → Gamma is Exponential(rate b): quantiles and survival are elementary
    t = theta_posterior(0, 0, 1 / 500, 500, 1)
    assert t.a == approx(1.0)
    assert t.b == approx(500.0)
    assert t.cpa_p90 == approx(500 / -math.log(0.9))  # 1/θ_p10
    assert t.cpa_p10 == approx(500 / -math.log(0.1))  # 1/θ_p90
    assert p_beats_cpa(t, 500) == approx(math.exp(-1))
    assert p_beats_cpa(t, 250) == approx(math.exp(-2))
    assert p_beats_cpa(t, 1000) == approx(math.exp(-0.5))


@pytest.mark.parametrize(
    ("conv", "value", "aov_parent", "k", "aov"),
    [
        (10, 20000, 1500, 5, 27500 / 15),
        (0, 0, 1500, 5, 1500.0),  # no conversions → parent AOV
        (100, 100000, 3000, 5, 115000 / 105),
    ],
)
def test_roas_posterior(conv: float, value: float, aov_parent: float, k: float, aov: float) -> None:
    t = theta_posterior(10, 5000, 1 / 500, 500, 2)  # a=12, b=6000
    r = roas_posterior(t, conv, value, aov_parent, k)
    assert r.aov_shrunk == approx(aov)
    assert r.roas_shrunk == approx(12 / 6000 * aov)
    assert r.roas_p10 < r.roas_shrunk < r.roas_p90


def test_p_beats_roas_and_ctr() -> None:
    t = theta_posterior(0, 0, 1 / 500, 500, 1)  # Exponential(500)
    assert p_beats_roas(t, 2, 1000) == approx(math.exp(-1))  # P(θ > 0.002)
    assert p_beats_roas(t, 4, 1000) == approx(math.exp(-2))
    assert p_beats_roas(t, 2, 0) == 0.0
    uniform = ctr_posterior(0, 0, 0.5, 2)  # Beta(1, 1)
    assert p_beats_ctr(uniform, 0.3) == approx(0.7)
    assert p_beats_ctr(uniform, 0.9) == approx(0.1)
    assert p_beats_ctr(ctr_posterior(0, 0, 0.5, 4), 0.5) == approx(0.5)  # symmetric Beta(2,2)


@pytest.mark.parametrize(
    ("p", "fatigue", "score"), [(0.876, False, 88), (0.876, True, 78), (0.05, True, 0)]
)
def test_composite(p: float, fatigue: bool, score: int) -> None:
    assert composite(p, fatigue) == score
