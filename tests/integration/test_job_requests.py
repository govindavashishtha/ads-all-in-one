"""Manual job trigger queue (migration 0003): the engine poller claims queued requests, runs
whitelisted jobs only, records the outcome, and fails requests orphaned by a dead engine."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from engine.config import Settings
from engine.connectors.factory import ConnectorFactory
from engine.jobs.requests import STALE_RUNNING, process_job_requests
from engine.jobs.runner import JobContext

pytestmark = pytest.mark.integration

CID = "client_jobreq"
NOW = dt.datetime(2026, 11, 11, 6, 0, tzinfo=dt.UTC)


@pytest.fixture
def ctx(migrated_engine: Engine, make_settings: Callable[..., Settings]) -> Iterator[JobContext]:
    with migrated_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO clients (client_id, name, timezone, currency, mode) "
                "VALUES (:c, 'JR', 'Asia/Kolkata', 'INR', 'read_only')"
            ),
            {"c": CID},
        )
    settings = make_settings(client_id=CID)
    yield JobContext(
        settings=settings,
        engine=migrated_engine,
        sessions=sessionmaker(bind=migrated_engine, expire_on_commit=False),
        connectors=ConnectorFactory(settings),
        clock=lambda: NOW,
    )
    with migrated_engine.begin() as c:
        c.execute(text("DELETE FROM job_requests WHERE client_id = :c"), {"c": CID})
        c.execute(text("DELETE FROM clients WHERE client_id = :c"), {"c": CID})


def _queue(engine: Engine, job: str, status: str = "queued", **kw: Any) -> int:
    with engine.begin() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO job_requests (client_id, job, requested_by, status, started_at) "
                    "VALUES (:c, :j, 'admin', :s, :st) RETURNING id"
                ),
                {"c": CID, "j": job, "s": status, "st": kw.get("started_at")},
            ).scalar_one()
        )


def _row(engine: Engine, req_id: int) -> Any:
    with engine.connect() as c:
        return c.execute(
            text("SELECT status, error, started_at, finished_at FROM job_requests WHERE id = :i"),
            {"i": req_id},
        ).one()


def test_runs_whitelisted_jobs_and_records_outcomes(ctx: JobContext) -> None:
    ran: list[str] = []

    def ok(c: JobContext) -> None:
        ran.append("pacing")

    def boom(c: JobContext) -> None:
        raise RuntimeError("kaput")

    jobs = {"pacing": ok, "hourly_pull": boom, "retention": ok}
    a = _queue(ctx.engine, "pacing")
    b = _queue(ctx.engine, "hourly_pull")
    c = _queue(ctx.engine, "retention")  # in SCHEDULE but not manual → rejected, never run
    assert process_job_requests(ctx, jobs) == 3
    assert ran == ["pacing"]
    ra, rb, rc = _row(ctx.engine, a), _row(ctx.engine, b), _row(ctx.engine, c)
    assert ra.status == "success" and ra.started_at == NOW and ra.finished_at == NOW
    assert rb.status == "failed" and "kaput" in rb.error
    assert rc.status == "rejected" and "cannot be triggered" in rc.error
    assert process_job_requests(ctx, jobs) == 0  # queue drained


def test_one_open_request_per_job(ctx: JobContext) -> None:
    _queue(ctx.engine, "pacing")
    with pytest.raises(Exception, match="uq_job_requests_open"):
        _queue(ctx.engine, "pacing")


def test_stale_running_requests_fail(ctx: JobContext) -> None:
    stale = _queue(ctx.engine, "pacing", "running", started_at=NOW - STALE_RUNNING * 2)
    fresh = _queue(ctx.engine, "hourly_pull", "running", started_at=NOW - dt.timedelta(minutes=5))
    process_job_requests(ctx, {})
    assert _row(ctx.engine, stale).status == "failed"
    assert _row(ctx.engine, fresh).status == "running"
