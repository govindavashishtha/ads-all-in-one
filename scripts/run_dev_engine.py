"""Development-only engine process: synthetic fixtures + FakeWriteClients.

Refuses to start unless APP_ENV=development. Never loads live platform credentials.
Used so job_requests / write_requests pollers can exercise pause-all, rollback and
execute against in-memory fake platforms on the UI demo database.
"""

from __future__ import annotations

import datetime as dt
import signal
import sys
import threading
from types import FrameType
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.background import BackgroundScheduler

from engine.config import Settings
from engine.connectors.factory import ConnectorFactory
from engine.connectors.fakes import synthetic_fixtures
from engine.db.session import make_engine, make_sessionmaker
from engine.executor.clients import FakeWriteClients
from engine.executor.fake_world import fake_platforms_from_db
from engine.jobs.runner import JobContext
from engine.jobs.schedule import register_ingestion_jobs, startup
from engine.logging import configure_logging
from engine.main import build_scheduler

log = structlog.get_logger(__name__)


def main() -> int:
    configure_logging()
    settings = Settings()
    if settings.app_env != "development":
        print("run_dev_engine is development-only", file=sys.stderr)
        return 2
    tz = ZoneInfo(settings.timezone)
    now = dt.datetime.now(tz)
    fixtures = synthetic_fixtures(now.date(), now.hour, days=90, hourly_days=28)
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)
    with sessions() as session:
        platforms = fake_platforms_from_db(session, settings.client_id)
    ctx = JobContext(
        settings=settings,
        engine=engine,
        sessions=sessions,
        connectors=ConnectorFactory(settings, fixtures=fixtures),
        write_clients=FakeWriteClients(platforms),
    )
    reconciled = startup(ctx)
    log.info(
        "dev_engine.ready",
        client_id=settings.client_id,
        fixtures_today=now.date().isoformat(),
        fake_platforms=sorted(p.value for p in platforms),
        **reconciled,
    )
    scheduler: BackgroundScheduler = build_scheduler(settings)
    register_ingestion_jobs(scheduler, ctx)
    scheduler.start()
    log.info("dev_engine.started", jobs=len(scheduler.get_jobs()))
    stop = threading.Event()

    def _handle(signum: int, _frame: FrameType | None) -> None:
        log.info("dev_engine.stopping", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    stop.wait()
    scheduler.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
