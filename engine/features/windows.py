"""Window arithmetic (plan §10, §11.1). All dates are ad-account-local days.

- ``_1d`` = ``as_of`` (yesterday); ``_7d`` / ``_28d`` / ``_30d`` = N complete days ending ``as_of``.
- ``_s14`` (scoring window) = ``score_window_days`` days ending ``settle_days`` complete days
  before ``as_of`` (DECISIONS D-2: the conservative reading — the ``settle_days`` most recent
  complete days are excluded, so with settle_days=2 and as_of=11-10 the window ends 11-08).
- prior window = ``prior_window_days`` days ending at the same settled date (§10.1).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from engine.rules.schema import Stats


@dataclass(frozen=True, slots=True)
class Window:
    start: dt.date
    end: dt.date


def trailing(as_of: dt.date, days: int) -> Window:
    return Window(as_of - dt.timedelta(days=days - 1), as_of)


def settled_end(as_of: dt.date, stats: Stats) -> dt.date:
    return as_of - dt.timedelta(days=stats.settle_days)


def scoring_window(as_of: dt.date, stats: Stats) -> Window:
    return trailing(settled_end(as_of, stats), stats.score_window_days)


def prior_window(as_of: dt.date, stats: Stats) -> Window:
    return trailing(settled_end(as_of, stats), stats.prior_window_days)


def lookback_start(as_of: dt.date, stats: Stats) -> dt.date:
    """Earliest date any window for ``as_of`` touches (for loading metrics)."""
    return min(
        trailing(as_of, 30).start,
        prior_window(as_of, stats).start,
        scoring_window(as_of, stats).start,
    )
