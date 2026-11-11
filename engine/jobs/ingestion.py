"""Ingestion jobs (plan §9, §9.1 steps 1–2, §9.2, §21): ``hourly_pull``, daily ``repull`` and
``windows`` steps, ``backfill``, ``search_terms`` (pull only), ``retention``, ``health_checks``.

Per platform: a platform whose read credentials or required inputs are missing is skipped
with a deduped ``warn`` alert; the step is then recorded ``skipped`` (not ``success``) so the
next run retries it. Writes that did happen are idempotent upserts, so a retry is harmless.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, cast
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import CursorResult, func, select, text
from sqlalchemy.orm import Session

from engine.connectors.base import ReadConnector
from engine.db.models import AdAccount, MetricsHourly
from engine.enums import EntityType, Platform, Severity
from engine.errors import CredentialsUnavailableError, MissingInputError
from engine.ingest.alerts import raise_alert
from engine.ingest.entities import sync_entities
from engine.ingest.metrics import (
    EntityIndex,
    upsert_daily,
    upsert_hourly,
    upsert_search_terms,
    upsert_window,
)
from engine.jobs.common import (
    client_window_key_hour,
    client_yesterday,
    enabled_connectors,
    hour_floor,
    local_now,
)
from engine.jobs.runner import JobContext, StepOutcome, StepResult, job_lock, run_step

log = structlog.get_logger(__name__)

HOURLY_LOOKBACK_HOURS = 3  # current + previous 3 hours (§9)
REPULL_DAYS = 7  # §9.1 step 1
WINDOW_DAYS = 7  # §8.3 metrics_window
WINDOW_LEVELS = (EntityType.AD_GROUP, EntityType.AD)  # §9.1 step 2: per ad set and ad
BACKFILL_DAYS = 90  # §9
BACKFILL_HOURLY_DAYS = 28
BACKFILL_CHUNK_DAYS = 30
SEARCH_TERM_DAYS = 30
HOURLY_RETENTION_DAYS = 90  # §8.3
STALE_AFTER = dt.timedelta(hours=2)  # §21
NOT_READY_LOCAL_HOUR = 3  # §9.1
DATA_NOT_READY = "data-not-ready"


def daily_levels(platform: Platform) -> tuple[EntityType, ...]:
    base = (EntityType.ACCOUNT, EntityType.CAMPAIGN, EntityType.AD_GROUP, EntityType.AD)
    return (*base, EntityType.KEYWORD) if platform is Platform.GOOGLE else base


def _input_missing(ctx: JobContext, s: Session, platform: Platform, exc: Exception) -> str:
    input_id = getattr(exc, "input_id", "input")
    raise_alert(
        s,
        ctx.client_id,
        dedupe_key=f"input_missing:{platform.value}:{input_id}",
        severity=Severity.WARN,
        kind="data_not_ready" if input_id == DATA_NOT_READY else "input_missing",
        message=f"{platform.value}: {exc}",
    )
    return f"{platform.value}: {exc}"


PlatformBody = Callable[[Session, ReadConnector], int]


def _per_platform(
    ctx: JobContext,
    s: Session,
    body: PlatformBody,
    only: Platform | None = None,
    *,
    sync: bool = False,
) -> StepOutcome:
    """Run ``body`` per enabled platform, each in its own savepoint. With ``sync=True`` the
    entity sync runs first in a separate savepoint, so it is kept even when the metrics part
    is skipped for a missing input (e.g. I-1 Meta conversion action)."""
    enabled = enabled_connectors(ctx, s)
    rows = 0
    skipped = [f"{p.value}: {why}" for p, why in enabled.skipped.items() if only in (None, p)]
    details: dict[str, object] = {}
    for pc in enabled.connectors:
        if only is not None and pc.platform is not only:
            continue
        n = 0
        if sync:
            with s.begin_nested():
                n += sync_entities(s, ctx.client_id, pc.connector, ctx.now()).rows
        try:
            with s.begin_nested():
                n += body(s, pc.connector)
        except (MissingInputError, CredentialsUnavailableError) as exc:
            skipped.append(_input_missing(ctx, s, pc.platform, exc))
            continue
        rows += n
        details[pc.platform.value] = n
    return StepOutcome(rows=rows, skipped_reason="; ".join(skipped) or None, details=details)


# ============================================================================ hourly_pull


def hourly_pull(ctx: JobContext) -> list[StepResult]:
    now = ctx.now()
    window = client_window_key_hour(now, ctx.settings.timezone)
    with job_lock(ctx.engine, ctx.client_id, "hourly_pull") as got:
        if not got:
            log.info("job.locked", job="hourly_pull")
            return []
        return [
            run_step(
                ctx,
                "hourly_pull",
                p.value,
                window,
                partial(_per_platform, ctx, body=_hourly_body(ctx, now), only=p, sync=True),
            )
            for p in (Platform.META, Platform.GOOGLE)
        ]


def _hourly_body(ctx: JobContext, now: dt.datetime) -> PlatformBody:
    def body(s: Session, conn: ReadConnector) -> int:
        tz = conn.get_account().timezone
        end = hour_floor(local_now(now, tz))
        start = end - dt.timedelta(hours=HOURLY_LOOKBACK_HOURS)
        index = EntityIndex.load(s, ctx.client_id, conn.platform)
        n = 0
        for level in (EntityType.ACCOUNT, EntityType.CAMPAIGN):
            n += upsert_hourly(
                s, ctx.client_id, index, conn.hourly_metrics(level, start, end), now=now
            ).written
        _check_operations(ctx, s, conn, now)
        return n

    return body


def _check_operations(ctx: JobContext, s: Session, conn: ReadConnector, now: dt.datetime) -> None:
    counter = getattr(conn, "counter", None)
    if counter is None:
        return
    day = local_now(now, conn.get_account().timezone).date()
    usage = counter.usage(day)
    if usage.over_alert_threshold:
        raise_alert(
            s,
            ctx.client_id,
            dedupe_key=f"api_ops_80:{conn.platform.value}:{day.isoformat()}",
            severity=Severity.WARN,
            kind="api_operations",
            message=(
                f"{conn.platform.value} API operations today {usage.used}/{usage.limit} "
                f"(≥ 80% of the access-level limit, §4.3)"
            ),
        )


# ============================================================================ daily steps


@dataclass(frozen=True, slots=True)
class AccountDay:
    as_of: dt.date
    ready: bool


def account_as_of(now: dt.datetime, tz: str) -> AccountDay:
    """as_of = yesterday in the account timezone; not ready before 03:00 local (§9.1)."""
    local = local_now(now, tz)
    return AccountDay(local.date() - dt.timedelta(days=1), local.hour >= NOT_READY_LOCAL_HOUR)


def _not_ready(conn: ReadConnector, as_of: dt.date) -> None:
    """Raised inside the platform savepoint; ``_per_platform`` records the alert (§9.1)."""
    raise MissingInputError(
        DATA_NOT_READY,
        f"{conn.platform.value} account local time is before 03:00; skipped for {as_of}",
    )


def repull_step(ctx: JobContext, s: Session) -> StepOutcome:
    now = ctx.now()

    def body(s: Session, conn: ReadConnector) -> int:
        day = account_as_of(now, conn.get_account().timezone)
        if not day.ready:
            _not_ready(conn, day.as_of)
        n = 0
        index = EntityIndex.load(s, ctx.client_id, conn.platform)
        start = day.as_of - dt.timedelta(days=REPULL_DAYS - 1)
        local_today = day.as_of + dt.timedelta(days=1)
        for level in daily_levels(conn.platform):
            rows = conn.daily_metrics(level, start, day.as_of)
            n += upsert_daily(
                s, ctx.client_id, index, rows, now=now, local_today=local_today
            ).written
        return n

    return _per_platform(ctx, s, body, sync=True)


def windows_step(ctx: JobContext, s: Session) -> StepOutcome:
    now = ctx.now()

    def body(s: Session, conn: ReadConnector) -> int:
        if conn.platform is not Platform.META:
            return 0
        day = account_as_of(now, conn.get_account().timezone)
        if not day.ready:
            _not_ready(conn, day.as_of)
        index = EntityIndex.load(s, ctx.client_id, conn.platform)
        n = 0
        for level in WINDOW_LEVELS:
            n += upsert_window(
                s, ctx.client_id, index, conn.window_metrics(level, day.as_of, WINDOW_DAYS)
            ).written
        return n

    return _per_platform(ctx, s, body)


# ============================================================================ backfill


def backfill(
    ctx: JobContext, days: int = BACKFILL_DAYS, hourly_days: int = BACKFILL_HOURLY_DAYS
) -> list[StepResult]:
    """Manual, once in Phase 1: 90 days of metrics_daily (all levels) + 28 days hourly (§9)."""
    now = ctx.now()
    end = client_yesterday(now, ctx.settings.timezone)
    window = f"{(end - dt.timedelta(days=days - 1)).isoformat()}..{end.isoformat()}"
    results: list[StepResult] = []
    with job_lock(ctx.engine, ctx.client_id, "backfill") as got:
        if not got:
            return []
        for p in (Platform.META, Platform.GOOGLE):
            results.append(
                run_step(
                    ctx,
                    "backfill",
                    f"{p.value}:daily",
                    window,
                    partial(
                        _per_platform, ctx, body=_backfill_daily(ctx, now, days), only=p, sync=True
                    ),
                )
            )
            results.append(
                run_step(
                    ctx,
                    "backfill",
                    f"{p.value}:hourly",
                    window,
                    partial(
                        _per_platform, ctx, body=_backfill_hourly(ctx, now, hourly_days), only=p
                    ),
                )
            )
    return results


def _backfill_daily(ctx: JobContext, now: dt.datetime, days: int) -> PlatformBody:
    def body(s: Session, conn: ReadConnector) -> int:
        n = 0
        index = EntityIndex.load(s, ctx.client_id, conn.platform)
        local_today = local_now(now, conn.get_account().timezone).date()
        end = local_today - dt.timedelta(days=1)
        start = end - dt.timedelta(days=days - 1)
        for level in daily_levels(conn.platform):
            chunk_start = start
            while chunk_start <= end:
                chunk_end = min(end, chunk_start + dt.timedelta(days=BACKFILL_CHUNK_DAYS - 1))
                rows = conn.daily_metrics(level, chunk_start, chunk_end)
                n += upsert_daily(
                    s, ctx.client_id, index, rows, now=now, local_today=local_today
                ).written
                chunk_start = chunk_end + dt.timedelta(days=1)
        return n

    return body


def _backfill_hourly(ctx: JobContext, now: dt.datetime, hourly_days: int) -> PlatformBody:
    def body(s: Session, conn: ReadConnector) -> int:
        index = EntityIndex.load(s, ctx.client_id, conn.platform)
        local = local_now(now, conn.get_account().timezone)
        end = hour_floor(local)
        n = 0
        day = local.date() - dt.timedelta(days=hourly_days - 1)
        while day <= local.date():  # one day per request keeps responses small
            start_h = dt.datetime.combine(day, dt.time(0))
            end_h = min(end, dt.datetime.combine(day, dt.time(23)))
            for level in (EntityType.ACCOUNT, EntityType.CAMPAIGN):
                n += upsert_hourly(
                    s, ctx.client_id, index, conn.hourly_metrics(level, start_h, end_h), now=now
                ).written
            day += dt.timedelta(days=1)
        return n

    return body


# ============================================================================ search_terms


def search_terms(ctx: JobContext) -> StepResult | None:
    """Pull + upsert the last 30 days (§9). The negative-keyword rule is the rules agent's."""
    now = ctx.now()
    as_of = client_yesterday(now, ctx.settings.timezone)

    def body(s: Session, conn: ReadConnector) -> int:
        end = local_now(now, conn.get_account().timezone).date() - dt.timedelta(days=1)
        start = end - dt.timedelta(days=SEARCH_TERM_DAYS - 1)
        index = EntityIndex.load(s, ctx.client_id, conn.platform)
        return upsert_search_terms(s, ctx.client_id, index, conn.search_terms(start, end)).written

    with job_lock(ctx.engine, ctx.client_id, "search_terms") as got:
        if not got:
            return None
        return run_step(
            ctx,
            "search_terms",
            "pull",
            as_of.isoformat(),
            partial(_per_platform, ctx, body=body, only=Platform.GOOGLE),
        )


# ============================================================================ retention


def _rowcount(s: Session, stmt: Any, params: dict[str, Any]) -> int:
    return int(cast(CursorResult[Any], s.execute(stmt, params)).rowcount)


def retention(ctx: JobContext) -> StepResult | None:
    now = ctx.now()
    day = local_now(now, ctx.settings.timezone).date()

    def body(s: Session) -> StepOutcome:
        deleted = _rowcount(
            s,
            text("DELETE FROM metrics_hourly WHERE client_id = :c AND ts_hour < :cutoff"),
            {
                "c": ctx.client_id,
                "cutoff": dt.datetime.combine(day, dt.time(0))
                - dt.timedelta(days=HOURLY_RETENTION_DAYS),
            },
        )
        finalised = _rowcount(
            s,
            text(
                "UPDATE metrics_daily SET is_final = true WHERE client_id = :c "
                "AND NOT is_final AND date < :cutoff"
            ),
            {"c": ctx.client_id, "cutoff": day - dt.timedelta(days=30)},
        )
        return StepOutcome(
            rows=deleted + finalised,
            details={"hourly_deleted": deleted, "daily_finalised": finalised},
        )

    with job_lock(ctx.engine, ctx.client_id, "retention") as got:
        if not got:
            return None
        return run_step(ctx, "retention", None, day.isoformat(), body)


# ============================================================================ health_checks


def health_checks(ctx: JobContext) -> StepResult | None:
    """Credential validity / expiry (≤ 7 days), data staleness (> 2 h), API ops ≥ 80% (§21)."""
    now = ctx.now()
    window = client_window_key_hour(now, ctx.settings.timezone)

    def body(s: Session) -> StepOutcome:
        enabled = enabled_connectors(ctx, s)
        n_alerts = 0
        for pc in enabled.connectors:
            p = pc.platform.value
            try:
                pc.connector.get_account()
            except Exception as exc:
                raise_alert(
                    s,
                    ctx.client_id,
                    dedupe_key=f"credential_invalid:{p}",
                    severity=Severity.CRITICAL,
                    kind="credential_invalid",
                    message=f"{p} read credentials failed: {type(exc).__name__}: {exc}",
                )
                n_alerts += 1
                continue
            expires = _token_expiry(pc.connector)
            if expires is not None and expires - now <= dt.timedelta(days=7):
                raise_alert(
                    s,
                    ctx.client_id,
                    dedupe_key=f"credential_expiring:{p}",
                    severity=Severity.WARN,
                    kind="credential_expiring",
                    message=f"{p} token expires at {expires.isoformat()} (≤ 7 days)",
                )
                n_alerts += 1
            _check_operations(ctx, s, pc.connector, now)
        for platform, last in _last_pulls(s, ctx.client_id).items():
            if last is None or now - last > STALE_AFTER:
                raise_alert(
                    s,
                    ctx.client_id,
                    dedupe_key=f"data_stale:{platform.value}",
                    severity=Severity.WARN,
                    kind="data_stale",
                    message=f"{platform.value} data stale: last pull "
                    f"{last.isoformat() if last else 'never'} (> 2 h)",
                )
                n_alerts += 1
        return StepOutcome(rows=n_alerts, details={"alerts": n_alerts})

    with job_lock(ctx.engine, ctx.client_id, "health_checks") as got:
        if not got:
            return None
        return run_step(ctx, "health_checks", None, window, body)


def _token_expiry(conn: ReadConnector) -> dt.datetime | None:
    fn = getattr(conn, "token_expires_at", None)
    if fn is None:
        return None
    try:
        value = fn()
    except Exception as exc:
        log.warning("health.token_expiry_failed", platform=conn.platform.value, error=str(exc))
        return None
    return value if isinstance(value, dt.datetime) else None


def _last_pulls(s: Session, client_id: str) -> dict[Platform, dt.datetime | None]:
    out: dict[Platform, dt.datetime | None] = {}
    accounts = s.execute(
        select(AdAccount.platform, func.max(AdAccount.last_synced_at))
        .where(AdAccount.client_id == client_id)
        .group_by(AdAccount.platform)
    ).all()
    for platform, synced in accounts:
        pulled = s.scalar(
            select(func.max(MetricsHourly.pulled_at)).where(
                MetricsHourly.client_id == client_id, MetricsHourly.platform == platform
            )
        )
        candidates = [t for t in (synced, pulled) if t is not None]
        out[Platform(platform)] = min(candidates) if candidates else None
    return out


def account_timezone(s: Session, client_id: str, platform: Platform) -> ZoneInfo | None:
    tz = s.scalar(
        select(AdAccount.timezone).where(
            AdAccount.client_id == client_id, AdAccount.platform == platform
        )
    )
    return None if tz is None else ZoneInfo(tz)
