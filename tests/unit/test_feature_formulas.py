"""§11.1 feature formulas and window arithmetic (pure parts)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from engine.bootstrap import DEFAULT_RULES_PATH, load_yaml
from engine.features.builder import cpa, ctr, fatigue, money, roas
from engine.features.data import Totals
from engine.features.windows import prior_window, scoring_window, settled_end, trailing
from engine.rules.schema import RulesConfig

STATS = RulesConfig.model_validate(load_yaml(DEFAULT_RULES_PATH)).stats


def test_ratios_and_none() -> None:
    t = Totals(1000, 25, Decimal("1234.50"), Decimal(3), Decimal("6000"))
    assert ctr(t) == 0.025
    assert cpa(t) == Decimal("411.50")
    assert roas(t) is not None and abs(roas(t) - 6000 / 1234.5) < 1e-12  # type: ignore[operator]
    empty = Totals()
    assert ctr(empty) is None and cpa(empty) is None and roas(empty) is None
    assert money(Decimal("10") ** 20) == Decimal("999999999999.99")


def test_leave_one_out_floors_at_zero() -> None:
    parent = Totals(100, 10, Decimal(50), Decimal(1), Decimal(10))
    child = Totals(150, 5, Decimal(60), Decimal(0), Decimal(10))
    assert parent - child == Totals(0, 5, Decimal(0), Decimal(1), Decimal(0))
    assert parent + child == Totals(250, 15, Decimal(110), Decimal(1), Decimal(20))


def test_fatigue_flag() -> None:
    assert fatigue(3.6, 0.01, 0.02, 14) is True
    assert fatigue(3.5, 0.01, 0.02, 14) is False  # strictly greater than 3.5
    assert fatigue(4.0, 0.015, 0.02, 14) is False  # 0.015 ≥ 0.7 × 0.02
    assert fatigue(4.0, 0.01, 0.02, 13.9) is False
    assert fatigue(None, 0.01, 0.02, 30) is False


def test_windows() -> None:
    as_of = dt.date(2026, 11, 10)
    assert trailing(as_of, 7).start == dt.date(2026, 11, 4)
    assert settled_end(as_of, STATS) == as_of - dt.timedelta(days=STATS.settle_days)
    w = scoring_window(as_of, STATS)
    assert (w.end - w.start).days + 1 == STATS.score_window_days
    assert w.end == settled_end(as_of, STATS)
    pw = prior_window(as_of, STATS)
    assert pw.end == w.end and (pw.end - pw.start).days + 1 == STATS.prior_window_days
