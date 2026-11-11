"""GET /overview (§17): spend vs caps, CPA/ROAS, pace, alerts, mode, kill switch."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.auth.adapter import Viewer
from api.deps import ClockDep, DbDep, SettingsDep, load_client
from api.schemas import AlertCounts, KillSwitchState, OverviewResponse, RangeMetrics, SpendBlock
from api.services.config import active_limits
from api.services.queries import build_range, money
from api.timeutil import account_zones, complete_days_window, parse_days
from engine.db.models import (
    AdAccount,
    Alert,
    EmergencyAction,
    MetricsDaily,
    MetricsHourly,
    Proposal,
)
from engine.enums import EntityType, Mode, Platform, ProposalStatus
from engine.guardrails.limits_schema import Cap

router = APIRouter(tags=["overview"])


def _fraction(num: Decimal, den: Decimal | None) -> float | None:
    return float(num / den) if den else None


def _spend_today(db: Session, cid: str, platform: Platform, today: dt.date) -> Decimal:
    start = dt.datetime.combine(today, dt.time())
    hourly = db.scalar(
        select(func.sum(MetricsHourly.spend)).where(
            MetricsHourly.client_id == cid,
            MetricsHourly.platform == platform,
            MetricsHourly.entity_type == EntityType.ACCOUNT,
            MetricsHourly.ts_hour >= start,
            MetricsHourly.ts_hour < start + dt.timedelta(days=1),
        )
    )
    if hourly is not None:
        return money(hourly)
    daily = db.scalar(
        select(func.sum(MetricsDaily.spend)).where(
            MetricsDaily.client_id == cid,
            MetricsDaily.platform == platform,
            MetricsDaily.entity_type == EntityType.ACCOUNT,
            MetricsDaily.date == today,
        )
    )
    return money(daily)


def _account_daily(
    db: Session, cid: str, platform: Platform, start: dt.date, end: dt.date
) -> RangeMetrics:
    row = db.execute(
        select(
            func.coalesce(func.sum(MetricsDaily.impressions), 0),
            func.coalesce(func.sum(MetricsDaily.clicks), 0),
            func.coalesce(func.sum(MetricsDaily.spend), 0),
            func.coalesce(func.sum(MetricsDaily.conversions), 0),
            func.coalesce(func.sum(MetricsDaily.conv_value), 0),
        ).where(
            MetricsDaily.client_id == cid,
            MetricsDaily.platform == platform,
            MetricsDaily.entity_type == EntityType.ACCOUNT,
            MetricsDaily.date.between(start, end),
        )
    ).one()
    return build_range(
        impressions=row[0],
        clicks=row[1],
        spend=row[2],
        conversions=row[3],
        conv_value=row[4],
        from_date=start,
        to_date=end,
    )


@router.get("/overview", response_model=OverviewResponse)
def overview(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    range_: str = Query("7d", alias="range", description="e.g. 7d, 28d (complete account days)"),
) -> OverviewResponse:
    days = parse_days(range_)
    client = load_client(db, settings)
    cid = client.client_id
    now = clock()
    zones = account_zones(db, client)
    limits, limits_version = active_limits(db, cid)

    blocks: list[SpendBlock] = []
    for platform in Platform:
        local = now.astimezone(zones[platform])
        today = local.date()
        start, end = complete_days_window(today, days)
        spend_today = _spend_today(db, cid, platform, today)
        mtd_before = (
            _account_daily(
                db, cid, platform, today.replace(day=1), today - dt.timedelta(days=1)
            ).spend
            if today.day > 1
            else Decimal("0.00")
        )
        cap: Cap | None = limits.caps.for_platform(platform) if limits else None
        mtd = money(mtd_before + spend_today)
        blocks.append(
            SpendBlock(
                platform=platform.value,
                account_date=today,
                elapsed_day_fraction=(local.hour * 3600 + local.minute * 60 + local.second) / 86400,
                spend_today=spend_today,
                daily_cap=cap.daily if cap else None,
                spent_fraction_today=_fraction(spend_today, cap.daily if cap else None),
                mtd_spend=mtd,
                monthly_cap=cap.monthly if cap else None,
                mtd_fraction=_fraction(mtd, cap.monthly if cap else None),
                range=_account_daily(db, cid, platform, start, end),
            )
        )

    total_cap = limits.caps.total if limits else None
    tot_today = money(sum((b.spend_today for b in blocks), Decimal(0)))
    tot_mtd = money(sum((b.mtd_spend for b in blocks), Decimal(0)))
    r = [b.range for b in blocks]
    total = SpendBlock(
        platform="total",
        account_date=None,
        spend_today=tot_today,
        daily_cap=total_cap.daily if total_cap else None,
        spent_fraction_today=_fraction(tot_today, total_cap.daily if total_cap else None),
        mtd_spend=tot_mtd,
        monthly_cap=total_cap.monthly if total_cap else None,
        mtd_fraction=_fraction(tot_mtd, total_cap.monthly if total_cap else None),
        range=build_range(
            impressions=sum(x.impressions for x in r),
            clicks=sum(x.clicks for x in r),
            spend=sum((x.spend for x in r), Decimal(0)),
            conversions=sum((x.conversions for x in r), Decimal(0)),
            conv_value=sum((x.conv_value for x in r), Decimal(0)),
        ),
    )

    counts = AlertCounts()
    for severity, n in db.execute(
        select(Alert.severity, func.count())
        .where(Alert.client_id == cid, Alert.acked_at.is_(None))
        .group_by(Alert.severity)
    ).all():
        if severity in ("info", "warn", "critical"):
            setattr(counts, severity, int(n))
    counts.total = counts.info + counts.warn + counts.critical

    open_approvals = db.scalar(
        select(func.count()).where(
            Proposal.client_id == cid, Proposal.status == ProposalStatus.AWAITING_APPROVAL.value
        )
    )
    active_emergency = db.scalar(
        select(func.count()).where(
            EmergencyAction.client_id == cid, EmergencyAction.restored_at.is_(None)
        )
    )
    synced = {p.value: None for p in Platform} | {
        Platform(p).value: ts
        for p, ts in db.execute(
            select(AdAccount.platform, func.max(AdAccount.last_synced_at))
            .where(AdAccount.client_id == cid)
            .group_by(AdAccount.platform)
        ).all()
    }
    return OverviewResponse(
        client_id=cid,
        client_name=client.name,
        currency=client.currency,
        mode=Mode(client.mode),
        hard_max_mode=settings.hard_max_mode,
        kill_switch=KillSwitchState(
            effective=settings.effective_kill_switch(client.kill_switch),
            db=client.kill_switch,
            env_override=settings.kill_switch,
        ),
        range_days=days,
        platforms=blocks,
        total=total,
        open_alerts=counts,
        open_approvals=int(open_approvals or 0),
        active_emergency_actions=int(active_emergency or 0),
        last_synced_at=synced,
        limits_version=limits_version,
        note=None if limits else "no valid active limits config; caps unavailable",
    )
