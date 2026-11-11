"""GET /overview (§17, §18 Overview page): spend vs caps, CPA/ROAS, pace, alerts, mode, kill
switch — one call carries everything the Overview page draws:

- per platform + total: today's spend vs daily cap, MTD vs monthly cap, range KPIs, account
  timezone, the intraday cumulative spend vs the expected curve (§12.4 step 2: learned from the
  last 28 days with ≥ 14 usable days, else h/24 — the same functions the pacing job uses), and
  the pacing job's projected end-of-day spend (``system_status``; null until it ran today);
- the daily trend (account-local dates, platforms summed by date), active rules targets, open
  warn/critical alerts and active emergency actions.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.auth.adapter import Viewer
from api.deps import ClockDep, DbDep, SettingsDep, load_client
from api.schemas import (
    AlertCounts,
    AlertOut,
    EmergencyActionOut,
    IntradayPoint,
    KillSwitchState,
    OverviewResponse,
    RangeMetrics,
    SpendBlock,
    TrendPoint,
)
from api.services.config import active_limits, active_targets
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
from engine.enums import EntityType, Mode, Platform, ProposalStatus, Severity
from engine.guardrails.limits_schema import Cap, LimitsConfig
from engine.jobs.pacing import curve_at, load_curve
from engine.system_status import get_statuses, pacing_key

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


def _hourly_today(db: Session, cid: str, platform: Platform, today: dt.date) -> dict[int, Decimal]:
    start = dt.datetime.combine(today, dt.time())
    rows = db.execute(
        select(MetricsHourly.ts_hour, func.sum(MetricsHourly.spend))
        .where(
            MetricsHourly.client_id == cid,
            MetricsHourly.platform == platform,
            MetricsHourly.entity_type == EntityType.ACCOUNT,
            MetricsHourly.ts_hour >= start,
            MetricsHourly.ts_hour < start + dt.timedelta(days=1),
        )
        .group_by(MetricsHourly.ts_hour)
    ).all()
    return {ts.hour: money(v) for ts, v in rows}


def _intraday(
    per_hour: dict[int, Decimal], now_hour: int, fractions: list[float], cap: Decimal | None
) -> list[IntradayPoint]:
    """``fractions[h]`` = expected cumulative share at the END of hour h (curve at h+1)."""
    out: list[IntradayPoint] = []
    cum = Decimal("0.00")
    for h in range(24):
        started = h <= now_hour
        spend = per_hour.get(h, Decimal("0.00")) if started else None
        if spend is not None:
            cum += spend
        out.append(
            IntradayPoint(
                hour=h,
                spend=spend,
                cumulative_spend=money(cum) if started else None,
                expected_cumulative=money(cap * Decimal(str(fractions[h]))) if cap else None,
            )
        )
    return out


def _trend(
    db: Session, cid: str, windows: dict[Platform, tuple[dt.date, dt.date]]
) -> list[TrendPoint]:
    acc: dict[dt.date, dict[str, Any]] = {}
    for platform, (start, end) in windows.items():
        rows = db.execute(
            select(
                MetricsDaily.date,
                func.coalesce(func.sum(MetricsDaily.impressions), 0),
                func.coalesce(func.sum(MetricsDaily.clicks), 0),
                func.coalesce(func.sum(MetricsDaily.spend), 0),
                func.coalesce(func.sum(MetricsDaily.conversions), 0),
                func.coalesce(func.sum(MetricsDaily.conv_value), 0),
            )
            .where(
                MetricsDaily.client_id == cid,
                MetricsDaily.platform == platform,
                MetricsDaily.entity_type == EntityType.ACCOUNT,
                MetricsDaily.date.between(start, end),
            )
            .group_by(MetricsDaily.date)
        ).all()
        for d, imps, clicks, spend, conv, value in rows:
            a = acc.setdefault(
                d,
                {
                    "impressions": 0,
                    "clicks": 0,
                    "spend": Decimal(0),
                    "conversions": Decimal(0),
                    "conv_value": Decimal(0),
                    "by": {p.value: Decimal("0.00") for p in Platform},
                },
            )
            a["impressions"] += int(imps)
            a["clicks"] += int(clicks)
            a["spend"] += Decimal(spend)
            a["conversions"] += Decimal(conv)
            a["conv_value"] += Decimal(value)
            a["by"][platform.value] = money(spend)
    out = []
    for d in sorted(acc):
        a = acc[d]
        r = build_range(
            impressions=a["impressions"],
            clicks=a["clicks"],
            spend=a["spend"],
            conversions=a["conversions"],
            conv_value=a["conv_value"],
        )
        out.append(
            TrendPoint(
                date=d,
                impressions=r.impressions,
                clicks=r.clicks,
                spend=r.spend,
                conversions=r.conversions,
                conv_value=r.conv_value,
                cpa=r.cpa,
                roas=r.roas,
                spend_by_platform=a["by"],
            )
        )
    return out


def _projection(
    snap: tuple[dict[str, Any], dt.datetime] | None, today: dt.date
) -> tuple[Decimal | None, dt.datetime | None]:
    if snap is None:
        return None, None
    value, at = snap
    if value.get("local_date") != today.isoformat() or value.get("projected_eod") is None:
        return None, None
    try:
        return money(Decimal(str(value["projected_eod"]))), at
    except ArithmeticError:
        return None, None


def _total_block(  # noqa: PLR0917 — private helper of overview()
    blocks: list[SpendBlock],
    platform_fracs: dict[Platform, list[float]],
    platform_hours: dict[Platform, dict[int, Decimal]],
    curve_sources: set[str],
    now_hour: int,
    with_account: set[Platform],
    limits: LimitsConfig | None,
) -> SpendBlock:
    total_cap = limits.caps.total if limits else None
    tot_today = money(sum((b.spend_today for b in blocks), Decimal(0)))
    tot_mtd = money(sum((b.mtd_spend for b in blocks), Decimal(0)))
    r = [b.range for b in blocks]
    # Total pace: hours are each account's local hours; expected share = cap-weighted mix of
    # the platform curves (equals the platform curve when only one platform has a cap).
    weights = {b_p: (b.daily_cap or Decimal(0)) for b_p, b in zip(Platform, blocks, strict=True)}
    wsum = sum(weights.values(), Decimal(0))
    total_fracs = [
        float(
            sum((weights[p] * Decimal(str(platform_fracs[p][h])) for p in Platform), Decimal(0))
            / wsum
        )
        if wsum
        else (h + 1) / 24
        for h in range(24)
    ]
    total_hours: dict[int, Decimal] = {}
    for per_hour in platform_hours.values():
        for h, v in per_hour.items():
            total_hours[h] = total_hours.get(h, Decimal(0)) + v
    with_accounts = [b for b_p, b in zip(Platform, blocks, strict=True) if b_p in with_account]
    total_projected = (
        money(sum((b.projected_eod or Decimal(0) for b in with_accounts), Decimal(0)))
        if with_accounts and all(b.projected_eod is not None for b in with_accounts)
        else None
    )
    projected_ats = [b.projected_eod_at for b in with_accounts if b.projected_eod_at]
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
        timezone=None,
        projected_eod=total_projected,
        projected_eod_at=min(projected_ats) if total_projected is not None else None,
        curve_source=curve_sources.pop() if len(curve_sources) == 1 else "mixed",
        intraday=_intraday(
            total_hours, now_hour, total_fracs, total_cap.daily if total_cap else None
        ),
    )

    return total


@router.get("/overview", response_model=OverviewResponse)
def overview(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    range_: str = Query("7d", alias="range", description="e.g. 7d, 28d (complete account days)"),
    trend_days: int = Query(28, ge=1, le=90, description="days in `trend` (complete days)"),
) -> OverviewResponse:
    days = parse_days(range_)
    client = load_client(db, settings)
    cid = client.client_id
    now = clock()
    zones = account_zones(db, client)
    limits, limits_version = active_limits(db, cid)
    accounts = {
        Platform(a.platform): a
        for a in db.scalars(
            select(AdAccount).where(AdAccount.client_id == cid).order_by(AdAccount.id)
        )
    }
    snaps = get_statuses(db, cid, [pacing_key(p.value) for p in Platform])

    blocks: list[SpendBlock] = []
    trend_windows: dict[Platform, tuple[dt.date, dt.date]] = {}
    platform_fracs: dict[Platform, list[float]] = {}
    platform_hours: dict[Platform, dict[int, Decimal]] = {}
    curve_sources: set[str] = set()
    now_hour = 0
    for platform in Platform:
        local = now.astimezone(zones[platform])
        today = local.date()
        now_hour = max(now_hour, local.hour)
        start, end = complete_days_window(today, days)
        trend_windows[platform] = complete_days_window(today, trend_days)
        spend_today = _spend_today(db, cid, platform, today)
        acct = accounts.get(platform)
        curve = load_curve(db, cid, acct.id, platform, today) if acct else None
        curve_sources.add("linear" if curve is None else "learned")
        fracs = [curve_at(curve, h + 1) for h in range(24)]
        platform_fracs[platform] = fracs
        per_hour = _hourly_today(db, cid, platform, today)
        platform_hours[platform] = per_hour
        projected, projected_at = _projection(snaps.get(pacing_key(platform.value)), today)
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
                timezone=acct.timezone if acct else None,
                projected_eod=projected,
                projected_eod_at=projected_at,
                curve_source="linear" if curve is None else "learned",
                intraday=_intraday(per_hour, local.hour, fracs, cap.daily if cap else None),
            )
        )

    total = _total_block(
        blocks, platform_fracs, platform_hours, curve_sources, now_hour, set(accounts), limits
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
        timezone=client.timezone,
        targets=active_targets(db, cid),
        trend_days=trend_days,
        trend=_trend(db, cid, trend_windows),
        alerts=[
            AlertOut.model_validate(a)
            for a in db.scalars(
                select(Alert)
                .where(
                    Alert.client_id == cid,
                    Alert.acked_at.is_(None),
                    Alert.severity.in_([Severity.WARN.value, Severity.CRITICAL.value]),
                )
                .order_by(Alert.created_at.desc(), Alert.id.desc())
                .limit(10)
            )
        ],
        emergency_actions=[
            EmergencyActionOut.model_validate(e)
            for e in db.scalars(
                select(EmergencyAction)
                .where(EmergencyAction.client_id == cid, EmergencyAction.restored_at.is_(None))
                .order_by(EmergencyAction.triggered_at.desc())
            )
        ],
    )
