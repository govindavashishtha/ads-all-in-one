"""Account-local day helpers (§0 rule 9). Metric dates are in the ad account's timezone."""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.db.models import AdAccount, Client
from engine.enums import Platform

_RANGE = re.compile(r"^(\d{1,3})d$")
MAX_RANGE_DAYS = 365


def parse_days(value: str, name: str = "range") -> int:
    """``"7d"`` -> 7. 1..365 days."""
    m = _RANGE.match(value.strip())
    days = int(m.group(1)) if m else 0
    if not 1 <= days <= MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=422, detail=f"{name} must look like '7d' (1-{MAX_RANGE_DAYS} days)"
        )
    return days


def account_zones(db: Session, client: Client) -> dict[Platform, ZoneInfo]:
    """Timezone per platform (first ad account), falling back to the client timezone."""
    zones = {p: ZoneInfo(client.timezone) for p in Platform}
    rows = db.execute(
        select(AdAccount.platform, AdAccount.timezone)
        .where(AdAccount.client_id == client.client_id)
        .order_by(AdAccount.id.desc())
    ).all()
    for platform, tz in rows:
        zones[Platform(platform)] = ZoneInfo(tz)
    return zones


def local_now(now: dt.datetime, zone: ZoneInfo) -> dt.datetime:
    return now.astimezone(zone)


def complete_days_window(today: dt.date, days: int) -> tuple[dt.date, dt.date]:
    """The last ``days`` complete account days, ending yesterday (inclusive bounds)."""
    return today - dt.timedelta(days=days), today - dt.timedelta(days=1)
