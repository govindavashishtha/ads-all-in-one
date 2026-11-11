"""APScheduler wiring for the ingestion-side jobs (plan §5, §9, §21).

Job defaults come from ``engine.main.JOB_DEFAULTS`` (coalesce, max_instances=1, 3600 s grace);
``pacing`` overrides grace to 300 s. Every job additionally takes a Postgres advisory lock
(``engine.jobs.runner.job_lock``) so two engine processes never run the same job at once.
Schedules use the client ``TIMEZONE``. Failures are caught here so the scheduler survives:
``daily_pipeline`` is retried once after 10 minutes, then a ``critical`` alert is raised;
other jobs raise a ``warn`` alert (``job_failed:<job>``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

import structlog
from apscheduler.schedulers.base import BaseScheduler

from engine.config import Settings
from engine.connectors.factory import COUNTERS, ConnectorFactory
from engine.db.session import make_engine, make_sessionmaker
from engine.enums import Severity
from engine.ingest.alerts import raise_alert
from engine.jobs.daily import daily_pipeline
from engine.jobs.ingestion import health_checks, hourly_pull, retention, search_terms
from engine.jobs.pacing import pacing
from engine.jobs.requests import process_job_requests
from engine.jobs.runner import JobContext
from engine.jobs.weekly import approval_expiry, notify_dispatch, weekly_strategy
from engine.system_status import operations_key, put_status

log = structlog.get_logger(__name__)

RETRY_DELAY = dt.timedelta(minutes=10)  # §9.1
DEFAULT_GRACE_S = 3600  # §5

# job id → (callable, cron trigger kwargs, misfire_grace_time override)
SCHEDULE: dict[str, tuple[Callable[[JobContext], Any], dict[str, Any], int | None]] = {
    "pacing": (pacing, {"minute": "*/15"}, 300),
    "hourly_pull": (hourly_pull, {"minute": 5}, None),
    "health_checks": (health_checks, {"minute": 30}, None),
    "daily_pipeline": (daily_pipeline, {"hour": 6, "minute": 0}, None),
    "search_terms": (search_terms, {"hour": 7, "minute": 0}, None),
    "retention": (retention, {"hour": 3, "minute": 0}, None),
    # Phase 2 (ranking agent)
    "weekly_strategy": (weekly_strategy, {"day_of_week": "mon", "hour": 7, "minute": 0}, None),
    "approval_expiry": (approval_expiry, {"minute": "*/15"}, 300),
    "notify_dispatch": (notify_dispatch, {"minute": "*/5"}, 300),
}
# Not a §9 job: polls the manual-trigger queue (migration 0003) every minute.
REQUESTS_JOB = "job_requests"


def run_job_requests(ctx: JobContext) -> int:
    return process_job_requests(ctx, {name: fn for name, (fn, _, _) in SCHEDULE.items()})


def publish_operation_counts(ctx: JobContext) -> None:
    """Copy this process's per-platform API operation counters to ``system_status`` so
    GET /system can show them (§4.3, §21). Never raises."""
    try:
        with ctx.sessions() as s, s.begin():
            for platform, counter in COUNTERS.items():
                usage = counter.snapshot()
                if usage is None:
                    continue
                put_status(
                    s,
                    ctx.client_id,
                    operations_key(platform.value),
                    {"day": usage.day.isoformat(), "used": usage.used, "limit": usage.limit},
                    ctx.now(),
                )
    except Exception:
        log.exception("job.publish_operations_failed")


def _alert_failure(ctx: JobContext, job: str, exc: BaseException, severity: Severity) -> None:
    try:
        with ctx.sessions() as s, s.begin():
            raise_alert(
                s,
                ctx.client_id,
                dedupe_key=f"job_failed:{job}",
                severity=severity,
                kind="job_failed",
                message=f"job {job} failed: {type(exc).__name__}: {exc}",
            )
    except Exception:
        log.exception("job.alert_failed", job=job)


def make_runner(
    ctx: JobContext, scheduler: BaseScheduler, job: str, fn: Callable[[JobContext], Any]
) -> Callable[[], None]:
    def run() -> None:
        try:
            fn(ctx)
        except Exception as exc:
            log.exception("job.failed", job=job)
            if job == "daily_pipeline":
                scheduler.add_job(
                    make_retry(ctx, job, fn),
                    "date",
                    run_date=ctx.now() + RETRY_DELAY,
                    id=f"{job}_retry",
                    replace_existing=True,
                )
            else:
                _alert_failure(ctx, job, exc, Severity.WARN)
        finally:
            publish_operation_counts(ctx)

    return run


def make_retry(ctx: JobContext, job: str, fn: Callable[[JobContext], Any]) -> Callable[[], None]:
    def retry() -> None:
        try:
            fn(ctx)
        except Exception as exc:
            log.exception("job.retry_failed", job=job)
            _alert_failure(ctx, job, exc, Severity.CRITICAL)

    return retry


def default_context(settings: Settings) -> JobContext:
    """Production context: real SDK connectors via ``get_credentials`` (read access only)."""
    engine = make_engine(settings.database_url)
    return JobContext(
        settings=settings,
        engine=engine,
        sessions=make_sessionmaker(engine),
        connectors=ConnectorFactory(settings),
    )


def register_ingestion_jobs(scheduler: BaseScheduler, ctx: JobContext) -> list[str]:
    ids: list[str] = []
    for job, (fn, cron, grace) in SCHEDULE.items():
        kwargs: dict[str, Any] = {
            "id": job,
            "name": job,
            "replace_existing": True,
            # explicit (= §5 defaults) so they hold even if the scheduler's defaults change
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": grace if grace is not None else DEFAULT_GRACE_S,
        }
        scheduler.add_job(
            make_runner(ctx, scheduler, job, fn),
            "cron",
            timezone=ctx.settings.timezone,
            **cron,
            **kwargs,
        )
        ids.append(job)
    scheduler.add_job(
        make_runner(ctx, scheduler, REQUESTS_JOB, run_job_requests),
        "cron",
        timezone=ctx.settings.timezone,
        minute="*",
        id=REQUESTS_JOB,
        name=REQUESTS_JOB,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    ids.append(REQUESTS_JOB)
    return ids
