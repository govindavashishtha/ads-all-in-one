"""Feature builder (plan §9.1 step 3, §11.1): one ``features`` row per entity and ``as_of``.

Builds every ``source="features"`` registry variable. Pure computation over a ``Universe`` +
``MetricsStore`` (``compute_features``) plus an idempotent upsert (``build_features``).
Search-term variables (``cost_30d`` …) are computed in ``engine.rules.search_terms``.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from engine.db.models import Feature
from engine.enums import EntityType
from engine.features.data import (
    REMOVED,
    EntityInfo,
    MetricsStore,
    Totals,
    Universe,
    account_day_end,
    load_metrics,
    load_universe,
)
from engine.features.windows import lookback_start, scoring_window, trailing
from engine.rules.registry import names_from
from engine.rules.schema import RulesConfig

CENT = Decimal("0.01")
MAX_MONEY = Decimal("999999999999.99")  # NUMERIC(14,2) ceiling
FEATURE_TYPES = (EntityType.CAMPAIGN, EntityType.AD_GROUP, EntityType.AD, EntityType.KEYWORD)

# §11.1 fatigue_flag constants (fixed by the plan, not configurable)
FATIGUE_FREQUENCY = 3.5
FATIGUE_CTR_RATIO = 0.7
FATIGUE_MIN_DAYS = 14


def money(x: Decimal) -> Decimal:
    return min(x, MAX_MONEY).quantize(CENT, rounding=ROUND_HALF_UP)


def ctr(t: Totals) -> float | None:
    return t.clicks / t.impressions if t.impressions > 0 else None


def cpa(t: Totals) -> Decimal | None:
    return money(t.spend / t.conversions) if t.conversions > 0 else None


def roas(t: Totals) -> float | None:
    return float(t.conv_value / t.spend) if t.spend > 0 else None


def _live_since(e: EntityInfo, store: MetricsStore) -> dt.datetime | None:
    if e.entity_type is EntityType.AD and e.first_live_at is not None:
        return e.first_live_at
    return store.first_active_at.get(e.key)


def fatigue(
    frequency_7d: float | None, ctr_7d: float | None, ctr_28d: float | None, days_live: float | None
) -> bool:
    if frequency_7d is None or ctr_7d is None or ctr_28d is None or days_live is None:
        return False
    return (
        frequency_7d > FATIGUE_FREQUENCY
        and ctr_7d < FATIGUE_CTR_RATIO * ctr_28d
        and days_live >= FATIGUE_MIN_DAYS
    )


def entity_features(
    e: EntityInfo, universe: Universe, store: MetricsStore, as_of: dt.date, rules: RulesConfig
) -> dict[str, Any]:
    key = e.key
    d1 = store.window(key, as_of, as_of)
    w7 = trailing(as_of, 7)
    w28 = trailing(as_of, 28)
    s14 = scoring_window(as_of, rules.stats)
    t7 = store.window(key, w7.start, w7.end)
    t28 = store.window(key, w28.start, w28.end)
    ts = store.window(key, s14.start, s14.end)

    ref = account_day_end(as_of, e.timezone)  # "now" for live-time = end of as_of
    since = _live_since(e, store)
    hours_live = max((ref - since).total_seconds() / 3600, 0.0) if since is not None else None
    days_live = hours_live / 24 if hours_live is not None else None

    frequency_7d = (
        store.frequency.get((key, as_of))
        if e.entity_type
        in (
            EntityType.AD_GROUP,
            EntityType.AD,
        )
        else None
    )
    spend_pace_7d: float | None = None
    if e.is_budget_unit and e.daily_budget is not None and e.daily_budget > 0:
        spend_pace_7d = float(t7.spend / (7 * e.daily_budget))
    c7, c28 = ctr(t7), ctr(t28)
    active_ads: int | None
    if e.entity_type in (EntityType.AD, EntityType.AD_GROUP, EntityType.KEYWORD):
        active_ads = universe.active_ads_in_group.get(e.ad_group_id or -1, 0)
    else:
        active_ads = None
    row: dict[str, Any] = {
        "is_budget_unit": e.is_budget_unit,
        "learning": e.learning,  # NULL -> true (§8.2), stored already coalesced
        "hours_live": hours_live,
        "days_live": days_live,
        "impressions_1d": d1.impressions,
        "clicks_1d": d1.clicks,
        "spend_1d": money(d1.spend),
        "conversions_1d": d1.conversions,
        "spend_7d": money(t7.spend),
        "conversions_7d": t7.conversions,
        "spend_s14": money(ts.spend),
        "conversions_s14": ts.conversions,
        "impressions_s14": ts.impressions,
        "clicks_s14": ts.clicks,
        "ctr_1d": ctr(d1),
        "ctr_7d": c7,
        "ctr_28d": c28,
        "cpa_1d": cpa(d1),
        "cpa_7d": cpa(t7),
        "cpa_28d": cpa(t28),
        "roas_7d": roas(t7),
        "roas_28d": roas(t28),
        "frequency_7d": frequency_7d,
        "spend_pace_7d": spend_pace_7d,
        "fatigue_flag": fatigue(frequency_7d, c7, c28, days_live),
        "active_ads_in_group": active_ads,
    }
    assert set(row) == names_from("features"), "registry drift"
    return row


def compute_features(
    universe: Universe, store: MetricsStore, as_of: dt.date, rules: RulesConfig
) -> dict[tuple[EntityType, int], dict[str, Any]]:
    """Features for every non-removed campaign / ad group / ad / positive keyword."""
    return {
        e.key: entity_features(e, universe, store, as_of, rules)
        for e in universe.of_type(*FEATURE_TYPES)
        if e.status != REMOVED
    }


def upsert_features(
    session: Session,
    client_id: str,
    as_of: dt.date,
    rows: dict[tuple[EntityType, int], dict[str, Any]],
) -> int:
    if not rows:
        return 0
    values = [
        {"client_id": client_id, "entity_type": et, "entity_id": eid, "as_of": as_of, **f}
        for (et, eid), f in sorted(rows.items(), key=lambda kv: (kv[0][0].value, kv[0][1]))
    ]
    stmt = insert(Feature).values(values)
    cols = names_from("features")
    stmt = stmt.on_conflict_do_update(
        index_elements=["client_id", "entity_type", "entity_id", "as_of"],
        set_={c: stmt.excluded[c] for c in cols},
    )
    session.execute(stmt)
    return len(values)


def build_features(
    session: Session, client_id: str, as_of: dt.date, rules: RulesConfig
) -> tuple[Universe, MetricsStore, dict[tuple[EntityType, int], dict[str, Any]]]:
    universe = load_universe(session, client_id)
    store = load_metrics(session, client_id, lookback_start(as_of, rules.stats), as_of)
    rows = compute_features(universe, store, as_of, rules)
    upsert_features(session, client_id, as_of, rows)
    return universe, store, rows
