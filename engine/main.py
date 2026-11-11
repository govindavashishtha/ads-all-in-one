"""Engine entry point (plan §3, §5, §15).

Phase 0: configure logging, run bootstrap (§7.1), start an empty APScheduler. Later agents
register jobs in ``register_jobs`` and add the startup reconciler (§15) before the scheduler.
"""

from __future__ import annotations

import signal
import threading
from types import FrameType

import structlog
from apscheduler.schedulers.background import BackgroundScheduler

from engine.bootstrap import bootstrap
from engine.config import Settings, get_settings
from engine.db.session import make_engine, make_sessionmaker
from engine.logging import configure_logging

log = structlog.get_logger(__name__)

JOB_DEFAULTS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}  # §5


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    return BackgroundScheduler(timezone=settings.timezone, job_defaults=JOB_DEFAULTS)


def register_jobs(scheduler: BackgroundScheduler, settings: Settings) -> None:
    """Jobs from §9 are added here by their owners (pacing/approval_expiry use 300 s grace)."""
    from engine.jobs.schedule import default_context, register_ingestion_jobs

    register_ingestion_jobs(scheduler, default_context(settings))


def main() -> None:
    configure_logging()
    settings = get_settings()
    factory = make_sessionmaker(make_engine(settings.database_url))
    with factory() as session, session.begin():
        result = bootstrap(session, settings)
    log.info("engine.bootstrapped", client_id=result.client_id, created=result.created)

    scheduler = build_scheduler(settings)
    register_jobs(scheduler, settings)
    scheduler.start()
    log.info("engine.started", jobs=len(scheduler.get_jobs()))

    stop = threading.Event()

    def _handle(signum: int, _frame: FrameType | None) -> None:
        log.info("engine.stopping", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    stop.wait()
    scheduler.shutdown(wait=True)


if __name__ == "__main__":
    main()
