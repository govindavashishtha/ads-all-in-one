"""GET /metrics time series (§17). Prometheus lives at the API root /metrics (not here)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from api.auth.adapter import Viewer
from api.deps import ClockDep, DbDep, SettingsDep, load_client
from api.schemas import MetricsPoint, MetricsSeries
from api.timeutil import MAX_RANGE_DAYS, account_zones
from engine.db.models import MetricsDaily, MetricsHourly
from engine.enums import EntityType, Platform

router = APIRouter(tags=["metrics"])

HOURLY_LEVELS = {EntityType.ACCOUNT, EntityType.CAMPAIGN}


@router.get("/metrics", response_model=MetricsSeries)
def metrics_series(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    entity_type: EntityType,
    entity_id: int,
    grain: Literal["day", "hour"] = "day",
    from_: Annotated[dt.date | None, Query(alias="from", description="account-local date")] = None,
    to: Annotated[dt.date | None, Query(description="account-local date, inclusive")] = None,
    platform: Annotated[
        Platform | None, Query(description="needed only for entity_type=account")
    ] = None,
) -> MetricsSeries:
    """Daily (``metrics_daily``) or hourly (``metrics_hourly``: account/campaign only)."""
    client = load_client(db, settings)
    if grain == "hour" and entity_type not in HOURLY_LEVELS:
        raise HTTPException(422, "grain=hour is only available for account and campaign")
    zone = account_zones(db, client)[platform] if platform else ZoneInfo(client.timezone)
    today = clock().astimezone(zone).date()
    to_date = to or today
    from_date = from_ or to_date - dt.timedelta(days=27 if grain == "day" else 1)
    if from_date > to_date:
        raise HTTPException(422, "from must be <= to")
    if (to_date - from_date).days > MAX_RANGE_DAYS:
        raise HTTPException(422, f"range is limited to {MAX_RANGE_DAYS} days")

    points: list[MetricsPoint] = []
    found_platform: Platform | None = platform
    if grain == "day":
        q = select(MetricsDaily).where(
            MetricsDaily.client_id == client.client_id,
            MetricsDaily.entity_type == entity_type,
            MetricsDaily.entity_id == entity_id,
            MetricsDaily.date.between(from_date, to_date),
        )
        if platform:
            q = q.where(MetricsDaily.platform == platform)
        for r in db.scalars(q.order_by(MetricsDaily.date)):
            found_platform = Platform(r.platform)
            points.append(
                MetricsPoint(
                    date=r.date,
                    impressions=r.impressions,
                    clicks=r.clicks,
                    spend=r.spend,
                    conversions=r.conversions,
                    conv_value=r.conv_value,
                    video_views=r.video_views,
                )
            )
    else:
        start = dt.datetime.combine(from_date, dt.time())
        end = dt.datetime.combine(to_date + dt.timedelta(days=1), dt.time())
        hq = select(MetricsHourly).where(
            MetricsHourly.client_id == client.client_id,
            MetricsHourly.entity_type == entity_type,
            MetricsHourly.entity_id == entity_id,
            MetricsHourly.ts_hour >= start,
            MetricsHourly.ts_hour < end,
        )
        if platform:
            hq = hq.where(MetricsHourly.platform == platform)
        for h in db.scalars(hq.order_by(MetricsHourly.ts_hour)):
            found_platform = Platform(h.platform)
            points.append(
                MetricsPoint(
                    ts_hour=h.ts_hour,
                    date=h.ts_hour.date(),
                    impressions=h.impressions,
                    clicks=h.clicks,
                    spend=h.spend,
                    conversions=h.conversions,
                    conv_value=h.conv_value,
                )
            )
    return MetricsSeries(
        entity_type=entity_type,
        entity_id=entity_id,
        platform=found_platform,
        grain=grain,
        from_date=from_date,
        to_date=to_date,
        points=points,
    )
