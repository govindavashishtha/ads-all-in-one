"""§10.7 weekly allocation: zero-sum, clamps, eligibility, seed determinism."""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal

from engine.enums import EntityType, Platform
from engine.ranking.allocation import AllocationParams, Member, allocate, seed_for

AS_OF = dt.date(2026, 11, 9)
P = AllocationParams(
    objective="conversions",
    mc_draws=10000,
    share_floor_factor=Decimal("0.5"),
    share_ceiling=Decimal("0.6"),
    auto_max_budget_change_pct=Decimal(15),
    min_spend_pace=Decimal("0.9"),
    max_frequency_7d=Decimal("3.5"),
    min_useful_change_pct=Decimal(5),
)


def _m(i: int, budget: str, a: float, b: float, pace: float | None = 1.0) -> Member:
    return Member((EntityType.AD_GROUP, i), Platform.META, Decimal(budget), a, b, None, pace, 1.0)


MEMBERS = [
    _m(1, "1000", 40, 10000),  # CPA ~250: strong winner
    _m(2, "1000", 20, 10000),  # CPA ~500
    _m(3, "1500", 10, 10000),  # CPA ~1000: loser
]


def test_zero_sum_and_clamped() -> None:
    out = allocate(MEMBERS, P, AS_OF)
    assert out, "a clear winner/loser pool should move budget"
    assert sum(d.delta for d in out) == 0
    for d in out:
        assert abs(d.delta) <= d.current * Decimal("0.15")
        assert abs(d.delta) >= d.current * Decimal("0.05")
        assert d.delta == d.delta.quantize(Decimal("0.01"))
    by = {d.key[1]: d.delta for d in out}
    assert by[1] > 0 and by[3] < 0


def test_seed_determinism() -> None:
    assert seed_for(AS_OF) == 20261109
    assert allocate(MEMBERS, P, AS_OF) == allocate(list(reversed(MEMBERS)), P, AS_OF)


def test_increase_eligibility_and_small_pools() -> None:
    low_pace = [_m(1, "1000", 40, 10000, pace=0.5), *MEMBERS[1:]]
    out = allocate(low_pace, P, AS_OF)
    assert all(d.delta <= 0 or d.key[1] != 1 for d in out)
    assert sum(d.delta for d in out) == 0
    assert allocate(MEMBERS[:1], P, AS_OF) == []
    assert allocate(MEMBERS, dataclasses.replace(P, objective="traffic"), AS_OF) == []
