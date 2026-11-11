"""Manual job triggers: API → ``job_requests`` queue → engine (migration 0003, §17 ``POST
/jobs/{name}/run``, §19 ``ads run``).

The API only inserts ``queued`` rows (admin only, audited, names from
``engine.jobs.manual.MANUAL_JOBS``). This poller runs every minute inside the engine
scheduler, claims the oldest queued request with ``FOR UPDATE SKIP LOCKED``, runs the job
with the engine's own context and records the outcome. Jobs keep their own advisory locks
and ``job_runs`` idempotency, so a manual run of a job that is already running (or already
done for its window) is a no-op, never a second execution.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from typing import Any

import structlog
from sqlalchemy import select, update

from engine.db.models import JobRequest
from engine.enums import JobRequestStatus
from engine.jobs.manual import MANUAL_JOBS
from engine.jobs.runner import JobContext

log = structlog.get_logger(__name__)

STALE_RUNNING = dt.timedelta(hours=6)  # a 'running' request older than this = engine died


def process_job_requests(
    ctx: JobContext, jobs: Mapping[str, Callable[[JobContext], Any]], *, max_requests: int = 5
) -> int:
    """Run up to ``max_requests`` queued requests; returns how many were processed."""
    _fail_stale(ctx)
    done = 0
    while done < max_requests:
        with ctx.sessions() as s, s.begin():
            req = s.scalars(
                select(JobRequest)
                .where(
                    JobRequest.client_id == ctx.client_id,
                    JobRequest.status == JobRequestStatus.QUEUED.value,
                )
                .order_by(JobRequest.requested_at, JobRequest.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if req is None:
                return done
            req_id, job = req.id, req.job
            fn = jobs.get(job) if job in MANUAL_JOBS else None
            now = ctx.now()
            if fn is None:
                req.status = JobRequestStatus.REJECTED.value
                req.finished_at = now
                req.error = f"job {job!r} cannot be triggered manually"
                done += 1
                continue
            req.status = JobRequestStatus.RUNNING.value
            req.started_at = now
        log.info("job_request.start", request_id=req_id, job=job)
        status, error = JobRequestStatus.SUCCESS, None
        try:
            fn(ctx)
        except Exception as exc:
            log.exception("job_request.failed", request_id=req_id, job=job)
            status, error = JobRequestStatus.FAILED, f"{type(exc).__name__}: {exc}"[:2000]
        with ctx.sessions() as s, s.begin():
            s.execute(
                update(JobRequest)
                .where(JobRequest.id == req_id)
                .values(status=status.value, finished_at=ctx.now(), error=error)
            )
        log.info("job_request.done", request_id=req_id, job=job, status=status.value)
        done += 1
    return done


def _fail_stale(ctx: JobContext) -> None:
    with ctx.sessions() as s, s.begin():
        s.execute(
            update(JobRequest)
            .where(
                JobRequest.client_id == ctx.client_id,
                JobRequest.status == JobRequestStatus.RUNNING.value,
                JobRequest.started_at < ctx.now() - STALE_RUNNING,
            )
            .values(
                status=JobRequestStatus.FAILED.value,
                finished_at=ctx.now(),
                error="engine stopped while the request was running",
            )
        )
