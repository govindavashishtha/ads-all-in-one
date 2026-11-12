"""Seed ads_autopilot_ui_demo from synthetic fixtures (development only).

Requires APP_ENV=development, DATABASE_URL, HARD_* caps, LLM_PROVIDER=fake,
and a bootstrapped client. No network, no live credentials.

Clock and fixtures match the existing score/llm demos: 2026-11-11 22:00 Asia/Kolkata
(synthetic_fixtures default today / now_hour). Search terms are pulled *before* the
daily pipeline so the negative-keyword rule can fire (D-9). Managed-campaign opt-in
is the same synthetic-demo step used in the Phase 2 pipeline tests (INPUT I-3 is
still outstanding for real accounts).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from engine.config import Settings
from engine.connectors.factory import ConnectorFactory
from engine.connectors.fakes import synthetic_fixtures
from engine.db.session import make_engine, make_sessionmaker
from engine.jobs.daily import daily_pipeline
from engine.jobs.ingestion import backfill, health_checks, hourly_pull, search_terms
from engine.jobs.pacing import pacing
from engine.jobs.runner import JobContext
from engine.jobs.weekly import weekly_strategy
from engine.logging import configure_logging

# Same instant as ads_autopilot_score_demo / ads_autopilot_llm_demo.
DEMO_TODAY = dt.date(2026, 11, 11)
DEMO_HOUR = 22


def _summary(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, list):
        return [_summary(r) for r in result]
    if isinstance(result, dict):
        return result
    state = getattr(result, "state", None)
    if state is not None:
        return {
            "job": getattr(result, "job", None),
            "step": getattr(result, "step", None),
            "window": getattr(result, "window_key", None),
            "state": state.value if hasattr(state, "value") else str(state),
            "rows": getattr(result, "rows", None),
        }
    return str(result)


def main() -> int:
    configure_logging("INFO")
    settings = Settings()
    if settings.app_env != "development":
        print("seed_ui_demo is development-only", file=sys.stderr)
        return 2
    tz = ZoneInfo(settings.timezone)
    now = dt.datetime(DEMO_TODAY.year, DEMO_TODAY.month, DEMO_TODAY.day, DEMO_HOUR, 0, tzinfo=tz)
    fixtures = synthetic_fixtures(DEMO_TODAY, DEMO_HOUR, days=90, hourly_days=28)

    def clock() -> dt.datetime:
        return now

    engine = make_engine(settings.database_url)
    ctx = JobContext(
        settings=settings,
        engine=engine,
        sessions=make_sessionmaker(engine),
        connectors=ConnectorFactory(settings, fixtures=fixtures, clock=clock),
        clock=clock,
    )
    out: dict[str, object] = {
        "clock": now.isoformat(),
        "client_id": settings.client_id,
        "llm_provider": settings.llm_provider,
        "backfill": _summary(backfill(ctx, days=90, hourly_days=28)),
        "hourly_pull": _summary(hourly_pull(ctx)),
    }
    with engine.begin() as conn:
        managed = conn.execute(
            text("UPDATE campaigns SET managed = true WHERE client_id = :c"),
            {"c": settings.client_id},
        ).rowcount
        mode = conn.execute(
            text("SELECT mode FROM clients WHERE client_id = :c"),
            {"c": settings.client_id},
        ).scalar()
    out["managed_campaigns"] = int(managed or 0)
    out["mode"] = mode
    # Search-term rules read the previous pull, so this must precede daily_pipeline.
    out["search_terms"] = _summary(search_terms(ctx))
    out["daily_pipeline"] = _summary(daily_pipeline(ctx))
    out["weekly_strategy"] = _summary(weekly_strategy(ctx))
    out["pacing"] = _summary(pacing(ctx))
    out["health_checks"] = _summary(health_checks(ctx))
    with engine.connect() as conn:
        counts = {}
        for table in (
            "campaigns",
            "ad_groups",
            "ads",
            "keywords",
            "metrics_daily",
            "scores",
            "proposals",
            "approvals",
            "llm_calls",
            "search_terms",
            "alerts",
            "change_log",
        ):
            counts[table] = int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)  # noqa: S608
        statuses = {
            f"{row[0]}:{row[1]}": int(row[2])
            for row in conn.execute(
                text("SELECT status, action, count(*) FROM proposals GROUP BY 1, 2")
            )
        }
    out["counts"] = counts
    out["proposals"] = statuses
    json.dump(out, sys.stdout, indent=1, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
