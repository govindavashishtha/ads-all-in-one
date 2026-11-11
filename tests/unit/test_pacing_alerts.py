"""Pacing math (§12.4 steps 2–5), alert-only in Phase 1."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from engine.bootstrap import DEFAULT_LIMITS_PATH, load_yaml
from engine.connectors.base import SpendSnapshot
from engine.enums import Platform
from engine.guardrails.limits_schema import LimitsConfig, parse_limits
from engine.jobs.pacing import curve_at, evaluate_platform, intraday_curve, project_eod

D = Decimal


def flat_day() -> list[Decimal]:
    return [D(10)] * 24


def test_curve_needs_14_days_else_linear() -> None:
    assert intraday_curve([flat_day()] * 13) is None
    assert curve_at(None, 6) == pytest.approx(0.25)
    c = intraday_curve([flat_day()] * 14)
    assert c is not None and len(c) == 25
    assert c[0] == 0 and c[24] == pytest.approx(1.0) and c[12] == pytest.approx(0.5)
    assert curve_at(c, 6.5) == pytest.approx(6.5 / 24)


def test_curve_shape_and_interpolation() -> None:
    evening = [D(0)] * 12 + [D(10)] * 12  # all spend after noon
    c = intraday_curve([evening] * 20)
    assert c is not None
    assert c[12] == 0.0 and c[18] == pytest.approx(0.5)
    assert curve_at(c, 18.5) == pytest.approx((0.5 + 7 / 12) / 2)
    # empty days are ignored, not counted
    assert intraday_curve([[D(0)] * 24] * 30) is None


def test_projection_floor() -> None:
    assert project_eod(D("100"), 0.5) == D("200.00")
    assert project_eod(D("100"), 0.0) == D("2000.00")  # max(curve, 0.05)


def _limits() -> LimitsConfig:
    return parse_limits(load_yaml(DEFAULT_LIMITS_PATH))


def snap(spend: str, camps: dict[str, str] | None = None) -> SpendSnapshot:
    return SpendSnapshot(
        Platform.META,
        dt.date(2026, 11, 11),
        D(spend),
        {k: D(v) for k, v in (camps or {}).items()},
        dt.datetime(2026, 11, 11, tzinfo=dt.UTC),
    )


def test_quiet_day_no_alerts() -> None:
    lim = _limits()
    cap = lim.caps.meta.daily
    r = evaluate_platform(
        snap(str(cap * D("0.30"))),
        limits=lim,
        elapsed_hours=12.0,
        curve=None,
        mtd_before_today=D(0),
        campaign_budgets={},
    )
    assert r.warn_reasons == [] and r.emergency == []
    assert r.projected_eod == (cap * D("0.30") / D("0.5")).quantize(D("0.01"))


def test_warn_and_emergency_thresholds_from_limits() -> None:
    lim = _limits()
    cap = lim.caps.meta.daily
    p = lim.pacing
    spent = (p.emergency_spent_fraction * cap).quantize(D("0.01"))
    r = evaluate_platform(
        snap(str(spent), {"c1": "900"}),
        limits=lim,
        elapsed_hours=20.0,
        curve=None,
        mtd_before_today=D(0),
        campaign_budgets={"c1": ("Camp", D("100"))},
    )
    assert any("of daily cap" in w for w in r.warn_reasons)
    assert any("Camp spent 900" in w for w in r.warn_reasons)  # > flex × budget
    assert [c.trigger for c in r.emergency] == ["daily_cap"]


def test_no_projection_before_min_elapsed_hours() -> None:
    lim = _limits()
    r = evaluate_platform(
        snap("1"),
        limits=lim,
        elapsed_hours=float(lim.pacing.min_elapsed_hours) - 0.5,
        curve=None,
        mtd_before_today=D(0),
        campaign_budgets={},
    )
    assert r.projected_eod is None


def test_monthly_emergency_uses_mtd() -> None:
    lim = _limits()
    r = evaluate_platform(
        snap("0"),
        limits=lim,
        elapsed_hours=10.0,
        curve=None,
        mtd_before_today=lim.caps.meta.monthly,
        campaign_budgets={},
    )
    assert [c.trigger for c in r.emergency] == ["monthly_cap"]
