"""Job plumbing (plan §5, §9, §21): Postgres advisory locks, ``job_runs`` idempotency,
step timing metrics.

- ``job_lock``: session-level ``pg_try_advisory_lock`` on a dedicated connection, keyed by
  ``(client_id, job)``. A second process/thread that can't get the lock skips the run (together
  with APScheduler ``max_instances=1`` this gives one run at a time per job).
- ``run_step``: one ``job_runs`` row per ``(client_id, job, step, window_key)``. A step already
  ``success`` for that window is skipped (§9.1). Otherwise the row is marked ``running`` (own
  commit), the step's writes and the ``success`` mark commit in ONE transaction, and a failure
  rolls the writes back and records ``failed`` + error. ``skipped`` rows are re-run next time.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog
from prometheus_client import Counter, Histogram
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from engine.config import Settings
from engine.connectors.factory import ConnectorFactory
from engine.enums import JobStatus

log = structlog.get_logger(__name__)

JOB_SECONDS = Histogram("ads_job_step_seconds", "Job step duration", ["job", "step", "status"])
ROWS_INGESTED = Counter("ads_rows_ingested_total", "Rows written by ingestion", ["job", "step"])


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class JobContext:
    settings: Settings
    engine: Engine
    sessions: sessionmaker[Session]
    connectors: ConnectorFactory
    clock: Callable[[], dt.datetime] = _utcnow
    # Phase 3: write-side client provider for the executor (engine.executor.clients). None =
    # production (``RealWriteClients``: write credential via the credentials provider). Typed
    # loosely so job plumbing never imports the executor / write connectors.
    write_clients: Any = None

    @property
    def client_id(self) -> str:
        return self.settings.client_id

    def now(self) -> dt.datetime:
        return self.clock().astimezone(dt.UTC)


class StepState(StrEnum):
    RAN = "ran"
    ALREADY_DONE = "already_done"
    SKIPPED = "skipped"
    LOCKED = "locked"


@dataclass
class StepOutcome:
    """Returned by a step body: rows written and an optional 'skipped' reason."""

    rows: int = 0
    skipped_reason: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StepResult:
    job: str
    step: str | None
    window_key: str
    state: StepState
    rows: int = 0
    status: JobStatus | None = None
    detail: str | None = None


def advisory_key(client_id: str, job: str) -> int:
    """Stable signed 64-bit key from (client_id, job)."""
    digest = hashlib.sha256(f"ads-autopilot:{client_id}:{job}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def job_lock(engine: Engine, client_id: str, job: str) -> Iterator[bool]:
    key = advisory_key(client_id, job)
    conn = engine.connect()
    try:
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
        conn.commit()
        try:
            yield got
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
    finally:
        conn.close()


_SELECT = text(
    "SELECT status FROM job_runs WHERE client_id = :c AND job = :j "
    "AND step IS NOT DISTINCT FROM :s AND window_key = :w"
)
_MARK_RUNNING = text(
    """
    INSERT INTO job_runs (client_id, job, step, window_key, started_at, status)
    VALUES (:c, :j, :s, :w, :now, 'running')
    ON CONFLICT (client_id, job, step, window_key)
    DO UPDATE SET status = 'running', started_at = :now, finished_at = NULL, error = NULL,
                  rows_ingested = NULL
    """
)
_FINISH = text(
    "UPDATE job_runs SET status = :status, finished_at = :now, rows_ingested = :rows, "
    "error = :error WHERE client_id = :c AND job = :j AND step IS NOT DISTINCT FROM :s "
    "AND window_key = :w"
)


def run_step(
    ctx: JobContext,
    job: str,
    step: str | None,
    window_key: str,
    body: Callable[[Session], StepOutcome],
) -> StepResult:
    keys = {"c": ctx.client_id, "j": job, "s": step, "w": window_key}
    with ctx.sessions() as s, s.begin():
        if s.execute(_SELECT, keys).scalar() == JobStatus.SUCCESS.value:
            return StepResult(job, step, window_key, StepState.ALREADY_DONE)
        s.execute(_MARK_RUNNING, {**keys, "now": ctx.now()})
    started = time.monotonic()
    bound = log.bind(job=job, step=step, window_key=window_key, client_id=ctx.client_id)
    try:
        with ctx.sessions() as s, s.begin():
            outcome = body(s)
            status = JobStatus.SKIPPED if outcome.skipped_reason else JobStatus.SUCCESS
            s.execute(
                _FINISH,
                {
                    **keys,
                    "status": status.value,
                    "now": ctx.now(),
                    "rows": outcome.rows,
                    "error": outcome.skipped_reason,
                },
            )
    except Exception as exc:
        with ctx.sessions() as s, s.begin():
            s.execute(
                _FINISH,
                {
                    **keys,
                    "status": JobStatus.FAILED.value,
                    "now": ctx.now(),
                    "rows": None,
                    "error": f"{type(exc).__name__}: {exc}"[:4000],
                },
            )
        JOB_SECONDS.labels(job=job, step=step or "", status="failed").observe(
            time.monotonic() - started
        )
        bound.error("job.step.failed", error=str(exc))
        raise
    JOB_SECONDS.labels(job=job, step=step or "", status=status.value).observe(
        time.monotonic() - started
    )
    ROWS_INGESTED.labels(job=job, step=step or "").inc(outcome.rows)
    bound.info("job.step.done", status=status.value, rows=outcome.rows, **outcome.details)
    state = StepState.SKIPPED if outcome.skipped_reason else StepState.RAN
    return StepResult(job, step, window_key, state, outcome.rows, status, outcome.skipped_reason)
