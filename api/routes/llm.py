"""GET /llm/usage (§13.3, §17): tokens and cost from the ``llm_calls`` ledger."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from sqlalchemy import Date, cast, func, select

from api.auth.adapter import Viewer
from api.deps import ClockDep, DbDep, SettingsDep, load_client
from api.schemas import LLMUsageDay, LLMUsageResponse, LLMUsageTotals
from api.timeutil import parse_days
from engine.db.models import LLMCall

router = APIRouter(tags=["llm"])


def _totals(rows: list[LLMUsageDay]) -> LLMUsageTotals:
    return LLMUsageTotals(
        calls=sum(r.calls for r in rows),
        input_tokens=sum(r.input_tokens for r in rows),
        cached_input_tokens=sum(r.cached_input_tokens for r in rows),
        output_tokens=sum(r.output_tokens for r in rows),
        cost_usd=sum((r.cost_usd for r in rows), Decimal(0)),
    )


@router.get("/llm/usage", response_model=LLMUsageResponse)
def llm_usage(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    range_: str = Query("30d", alias="range"),
) -> LLMUsageResponse:
    """Days are in the client timezone (``TIMEZONE``), matching the token caps' day."""
    days = parse_days(range_)
    client = load_client(db, settings)
    tz = client.timezone
    today = clock().astimezone(ZoneInfo(tz)).date()
    start = min(today - dt.timedelta(days=days - 1), today.replace(day=1))
    local_day = cast(func.timezone(tz, LLMCall.created_at), Date)
    sums = (
        func.count(),
        func.coalesce(func.sum(LLMCall.input_tokens), 0),
        func.coalesce(func.sum(LLMCall.cached_input_tokens), 0),
        func.coalesce(func.sum(LLMCall.output_tokens), 0),
        func.coalesce(func.sum(LLMCall.cost_usd), 0),
    )
    base = select(local_day.label("d"), LLMCall.tier, *sums).where(
        LLMCall.client_id == client.client_id, local_day >= start
    )
    all_rows = [
        LLMUsageDay(
            date=d,
            tier=tier,
            calls=n,
            input_tokens=i,
            cached_input_tokens=c,
            output_tokens=o,
            cost_usd=Decimal(cost),
        )
        for d, tier, n, i, c, o, cost in db.execute(
            base.group_by(local_day, LLMCall.tier).order_by(local_day, LLMCall.tier)
        ).all()
    ]
    window_start = today - dt.timedelta(days=days - 1)
    by_purpose: dict[str, LLMUsageTotals] = {}
    for purpose, n, i, c, o, cost in db.execute(
        select(LLMCall.purpose, *sums)
        .where(LLMCall.client_id == client.client_id, local_day >= window_start)
        .group_by(LLMCall.purpose)
    ).all():
        by_purpose[purpose] = LLMUsageTotals(
            calls=n,
            input_tokens=i,
            cached_input_tokens=c,
            output_tokens=o,
            cost_usd=Decimal(cost),
        )
    return LLMUsageResponse(
        range_days=days,
        days=[r for r in all_rows if r.date >= window_start],
        by_purpose=by_purpose,
        today=_totals([r for r in all_rows if r.date == today]),
        month_to_date=_totals([r for r in all_rows if r.date >= today.replace(day=1)]),
        daily_token_cap=settings.llm_daily_token_cap,
        daily_cost_cap_usd=settings.llm_daily_cost_cap_usd,
        monthly_cost_cap_usd=settings.llm_monthly_cost_cap_usd,
        llm_enabled=False,
    )
