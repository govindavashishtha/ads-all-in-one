"""APScheduler wiring (§5, §9): cadences, grace times, advisory-lock keys."""

from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from engine.connectors.factory import ConnectorFactory
from engine.jobs.daily import STEPS, register_step
from engine.jobs.runner import JobContext, advisory_key
from engine.jobs.schedule import register_ingestion_jobs
from engine.main import JOB_DEFAULTS


def test_jobs_registered_with_plan_cadences(make_settings) -> None:
    settings = make_settings()
    engine = create_engine("postgresql+psycopg://u:p@127.0.0.1:1/none")  # never connected
    ctx = JobContext(settings, engine, sessionmaker(bind=engine), ConnectorFactory(settings))
    sched = BackgroundScheduler(timezone=settings.timezone, job_defaults=JOB_DEFAULTS)
    ids = register_ingestion_jobs(sched, ctx)
    assert set(ids) == {
        "pacing",
        "hourly_pull",
        "health_checks",
        "daily_pipeline",
        "search_terms",
        "retention",
        "weekly_strategy",
        "approval_expiry",
        "notify_dispatch",
        "job_requests",  # manual-trigger queue poller (migration 0003)
    }
    jobs = {j.id: j for j in sched.get_jobs()}
    assert str(jobs["pacing"].trigger.fields[6]) == "*/15"  # minute field
    assert jobs["pacing"].misfire_grace_time == 300
    assert jobs["hourly_pull"].misfire_grace_time == 3600
    assert str(jobs["hourly_pull"].trigger.fields[6]) == "5"
    daily = jobs["daily_pipeline"].trigger
    assert (str(daily.fields[5]), str(daily.fields[6])) == ("6", "0")
    weekly = jobs["weekly_strategy"].trigger
    assert (str(weekly.fields[4]), str(weekly.fields[5]), str(weekly.fields[6])) == (
        "mon",
        "7",
        "0",
    )
    assert jobs["approval_expiry"].misfire_grace_time == 300  # §5
    assert str(jobs["approval_expiry"].trigger.fields[6]) == "*/15"
    assert all(j.max_instances == 1 and j.coalesce for j in jobs.values())


def test_manual_jobs_are_scheduled_jobs() -> None:
    from engine.jobs.manual import MANUAL_JOBS
    from engine.jobs.schedule import SCHEDULE

    assert set(MANUAL_JOBS) <= set(SCHEDULE)
    assert "retention" not in MANUAL_JOBS and "backfill" not in MANUAL_JOBS


def test_daily_steps_fixed_order_and_registration() -> None:
    assert STEPS[:2] == ("repull", "windows") and STEPS[-1] == "digest" and len(STEPS) == 11
    try:
        register_step("not_a_step", lambda ctx, s: None)  # type: ignore[arg-type,return-value]
    except ValueError:
        pass
    else:
        raise AssertionError("unknown step accepted")


def test_advisory_key_stable_and_distinct() -> None:
    assert advisory_key("c", "pacing") == advisory_key("c", "pacing")
    assert advisory_key("c", "pacing") != advisory_key("c", "hourly_pull")
    assert -(2**63) <= advisory_key("c", "x") < 2**63
