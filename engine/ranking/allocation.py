"""Weekly budget allocation (plan §10.7). Pure function + a DB wrapper for the weekly job.

1. pools: ad sets of one managed ABO Meta campaign, or managed campaigns sharing a ``pool``
   label; members are budget units, data_sufficient, not learning, not in cooldown; < 2 → skip.
2. win share: ``mc_draws`` samples of θ (× AOV for value) per member with
   ``numpy.random.default_rng(seed = as_of as YYYYMMDD)``; share = fraction of draws won.
3. clamp to [share_floor_factor / n, share_ceiling], renormalise (single pass, as written).
4. delta = share × pool_total − current, clamped to ± auto_max_budget_change_pct of current.
5. no increase when spend_pace_7d < min_spend_pace (or unknown) or Meta frequency_7d > max.
6. zero-sum: scale the larger side down so increases == decreases.
7. drop deltas under min_useful_change_pct, then re-balance (repeat until stable — DECISIONS
   D-15), round to the cent toward zero and fix the residual cents on the larger side so the
   pool total is exactly unchanged.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

import numpy as np

from engine.enums import Platform
from engine.features.data import EntityKey

CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Member:
    key: EntityKey
    platform: Platform
    current_budget: Decimal
    theta_a: float
    theta_b: float
    aov: float | None
    spend_pace_7d: float | None
    frequency_7d: float | None


@dataclass(frozen=True, slots=True)
class AllocationParams:
    objective: str
    mc_draws: int
    share_floor_factor: Decimal
    share_ceiling: Decimal
    auto_max_budget_change_pct: Decimal
    min_spend_pace: Decimal
    max_frequency_7d: Decimal
    min_useful_change_pct: Decimal


@dataclass(frozen=True, slots=True)
class Delta:
    key: EntityKey
    current: Decimal
    win_share: float
    target_share: float
    delta: Decimal
    new_budget: Decimal
    change_pct: Decimal


def seed_for(as_of: dt.date) -> int:
    return int(as_of.strftime("%Y%m%d"))


def win_shares(members: list[Member], objective: str, draws: int, seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    cols = []
    for m in members:
        th = rng.gamma(shape=m.theta_a, scale=1 / m.theta_b, size=draws)
        if objective == "value":
            th = th * (m.aov or 0.0)
        cols.append(th)
    mat = np.column_stack(cols)
    winners = np.argmax(mat, axis=1)
    counts = np.bincount(winners, minlength=len(members))
    return [float(c) / draws for c in counts]


def target_shares(win: list[float], floor_factor: float, ceiling: float) -> list[float]:
    n = len(win)
    lo, hi = floor_factor / n, ceiling
    clamped = [min(max(w, lo), hi) for w in win]
    total = sum(clamped)
    return [c / total for c in clamped]


def _balance(deltas: list[Decimal]) -> list[Decimal]:
    inc = sum((d for d in deltas if d > 0), Decimal(0))
    dec = -sum((d for d in deltas if d < 0), Decimal(0))
    if inc == 0 or dec == 0:
        return [Decimal(0)] * len(deltas)
    if inc > dec:
        f = dec / inc
        return [d * f if d > 0 else d for d in deltas]
    f = inc / dec
    return [d * f if d < 0 else d for d in deltas]


def allocate(members: list[Member], p: AllocationParams, as_of: dt.date) -> list[Delta]:
    members = sorted(members, key=lambda m: (m.key[0].value, m.key[1]))
    if len(members) < 2 or p.objective not in ("conversions", "value"):
        return []
    win = win_shares(members, p.objective, p.mc_draws, seed_for(as_of))
    share = target_shares(win, float(p.share_floor_factor), float(p.share_ceiling))
    total = sum((m.current_budget for m in members), Decimal(0))
    cap = p.auto_max_budget_change_pct / 100
    deltas: list[Decimal] = []
    for m, s in zip(members, share, strict=True):
        d = Decimal(str(s)) * total - m.current_budget
        d = max(min(d, m.current_budget * cap), -m.current_budget * cap)
        if d > 0 and (
            m.spend_pace_7d is None
            or Decimal(str(m.spend_pace_7d)) < p.min_spend_pace
            or (
                m.platform is Platform.META
                and m.frequency_7d is not None
                and Decimal(str(m.frequency_7d)) > p.max_frequency_7d
            )
        ):
            d = Decimal(0)
        deltas.append(d)
    deltas = _balance(deltas)
    min_useful = p.min_useful_change_pct / 100
    for _ in range(len(members) + 1):
        dropped = [
            Decimal(0) if abs(d) < m.current_budget * min_useful else d
            for d, m in zip(deltas, members, strict=True)
        ]
        dropped = _balance(dropped)
        if dropped == deltas:
            break
        deltas = dropped
    rounded = [d.quantize(CENT, rounding=ROUND_DOWN) for d in deltas]
    residual = sum(rounded, Decimal(0))  # >0: increases exceed decreases
    if residual != 0 and any(rounded):
        side = [i for i, d in enumerate(rounded) if (d > 0) == (residual > 0) and d != 0]
        if side:
            i = max(side, key=lambda j: abs(rounded[j]))
            rounded[i] -= residual
    out: list[Delta] = []
    for m, w, s, d in zip(members, win, share, rounded, strict=True):
        if d == 0:
            continue
        out.append(
            Delta(
                key=m.key,
                current=m.current_budget,
                win_share=w,
                target_share=s,
                delta=d,
                new_budget=m.current_budget + d,
                change_pct=(d / m.current_budget * 100).quantize(Decimal("0.0001")),
            )
        )
    return out
