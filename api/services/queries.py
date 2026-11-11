"""Read-side query helpers shared by routes (metrics windows, latest scores, audit)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from api.schemas import RangeMetrics, ScoreOut
from engine.db.models import AuditLog, MetricsDaily, Score
from engine.enums import EntityType, Platform

CENT = Decimal("0.01")


def money(value: Decimal | int | None) -> Decimal:
    return Decimal(value or 0).quantize(CENT)


def build_range(
    *,
    impressions: int,
    clicks: int,
    spend: Decimal,
    conversions: Decimal,
    conv_value: Decimal,
    from_date: dt.date | None = None,
    to_date: dt.date | None = None,
) -> RangeMetrics:
    spend = money(spend)
    conversions = Decimal(conversions or 0)
    conv_value = money(conv_value)
    return RangeMetrics(
        from_date=from_date,
        to_date=to_date,
        impressions=int(impressions or 0),
        clicks=int(clicks or 0),
        spend=spend,
        conversions=conversions,
        conv_value=conv_value,
        ctr=(clicks / impressions) if impressions else None,
        cpa=(spend / conversions).quantize(CENT) if conversions > 0 else None,
        roas=float(conv_value / spend) if spend > 0 else None,
    )


def _sums() -> tuple[Any, ...]:
    return (
        func.coalesce(func.sum(MetricsDaily.impressions), 0),
        func.coalesce(func.sum(MetricsDaily.clicks), 0),
        func.coalesce(func.sum(MetricsDaily.spend), 0),
        func.coalesce(func.sum(MetricsDaily.conversions), 0),
        func.coalesce(func.sum(MetricsDaily.conv_value), 0),
    )


def range_metrics(
    db: Session,
    client_id: str,
    entity_type: EntityType,
    ids: Sequence[int],
    windows: dict[Platform, tuple[dt.date, dt.date]],
) -> dict[int, RangeMetrics]:
    """Sums of ``metrics_daily`` per entity over each platform's account-local window."""
    if not ids:
        return {}
    window_clause = or_(
        *(
            and_(MetricsDaily.platform == p, MetricsDaily.date.between(start, end))
            for p, (start, end) in windows.items()
        )
    )
    rows = db.execute(
        select(MetricsDaily.entity_id, MetricsDaily.platform, *_sums())
        .where(
            MetricsDaily.client_id == client_id,
            MetricsDaily.entity_type == entity_type,
            MetricsDaily.entity_id.in_(ids),
            window_clause,
        )
        .group_by(MetricsDaily.entity_id, MetricsDaily.platform)
    ).all()
    out: dict[int, RangeMetrics] = {}
    for entity_id, platform, imps, clicks, spend, conv, value in rows:
        start, end = windows[Platform(platform)]
        out[int(entity_id)] = build_range(
            impressions=imps,
            clicks=clicks,
            spend=spend,
            conversions=conv,
            conv_value=value,
            from_date=start,
            to_date=end,
        )
    return out


def empty_range(
    platform: Platform, windows: dict[Platform, tuple[dt.date, dt.date]]
) -> RangeMetrics:
    start, end = windows[platform]
    return RangeMetrics(from_date=start, to_date=end)


def latest_scores(
    db: Session, client_id: str, entity_type: EntityType, ids: Iterable[int]
) -> dict[int, ScoreOut]:
    id_list = list(ids)
    if not id_list:
        return {}
    rows = db.scalars(
        select(Score)
        .where(
            Score.client_id == client_id,
            Score.entity_type == entity_type,
            Score.entity_id.in_(id_list),
        )
        .order_by(Score.entity_id, Score.as_of.desc())
        .ext(distinct_on(Score.entity_id))
    ).all()
    return {r.entity_id: ScoreOut.model_validate(r) for r in rows}


def audit(
    db: Session,
    *,
    client_id: str,
    actor: str,
    action: str,
    target: str | None,
    payload: dict[str, Any] | None,
    ip: str | None,
    at: dt.datetime,
) -> None:
    db.add(
        AuditLog(
            client_id=client_id,
            actor=actor,
            action=action,
            target=target,
            payload=payload,
            ip=ip,
            at=at,
        )
    )
