"""Operator / developer entry point for the ingestion jobs.

    uv run python -m engine.jobs <job> [--fixtures synthetic|DIR] [--now ISO8601] [--days N]
    uv run python -m engine.jobs counts

Jobs: hourly_pull, daily_pipeline, backfill, search_terms, retention, health_checks, pacing,
all (backfill → hourly_pull → daily_pipeline → search_terms → pacing → health_checks).

``--fixtures`` replays fixtures through fake transports (no credentials, no network); it is
refused unless APP_ENV=development. Without it the real SDK connectors are used with the read
credentials from ``get_credentials`` (missing ones are skipped with a warn alert).
``--now`` pins the clock (UTC if no offset), e.g. to match the synthetic fixtures' "today".
Uses DATABASE_URL; run ``make migrate`` and ``make bootstrap`` first.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

from engine.config import get_settings
from engine.connectors.factory import ConnectorFactory
from engine.connectors.fakes import FixtureSet, synthetic_fixtures
from engine.db.session import make_engine, make_sessionmaker
from engine.jobs.daily import daily_pipeline
from engine.jobs.ingestion import backfill, health_checks, hourly_pull, retention, search_terms
from engine.jobs.pacing import pacing
from engine.jobs.runner import JobContext
from engine.logging import configure_logging

COUNT_TABLES = (
    "ad_accounts",
    "campaigns",
    "ad_groups",
    "ads",
    "keywords",
    "entity_history",
    "metrics_hourly",
    "metrics_daily",
    "metrics_window",
    "search_terms",
    "job_runs",
    "alerts",
)


def build_context(fixtures: str | None, now: str | None) -> JobContext:
    settings = get_settings()
    fx: FixtureSet | None = None
    if fixtures:
        if settings.app_env != "development":
            raise SystemExit("--fixtures is only allowed with APP_ENV=development")
        fx = (
            synthetic_fixtures() if fixtures == "synthetic" else FixtureSet.from_dir(Path(fixtures))
        )
    pinned: dt.datetime | None = None
    if now:
        pinned = dt.datetime.fromisoformat(now)
        if pinned.tzinfo is None:
            pinned = pinned.replace(tzinfo=dt.UTC)
    engine = make_engine(settings.database_url)

    def clock() -> dt.datetime:
        return pinned if pinned is not None else dt.datetime.now(dt.UTC)

    return JobContext(
        settings=settings,
        engine=engine,
        sessions=make_sessionmaker(engine),
        connectors=ConnectorFactory(settings, fixtures=fx, clock=clock),
        clock=clock,
    )


def counts(ctx: JobContext) -> dict[str, int]:
    with ctx.engine.connect() as conn:
        return {
            t: int(conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() or 0)  # noqa: S608
            for t in COUNT_TABLES
        }


def _summary(result: Any) -> Any:
    if isinstance(result, list):
        return [_summary(r) for r in result]
    if result is None:
        return None
    return {
        "job": result.job,
        "step": result.step,
        "window": result.window_key,
        "state": result.state.value,
        "rows": result.rows,
        "detail": result.detail,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m engine.jobs")
    parser.add_argument(
        "job",
        choices=[
            "hourly_pull",
            "daily_pipeline",
            "backfill",
            "search_terms",
            "retention",
            "health_checks",
            "pacing",
            "all",
            "counts",
        ],
    )
    parser.add_argument("--fixtures", help="'synthetic' or a fixture directory")
    parser.add_argument("--now", help="pin the clock, ISO 8601")
    parser.add_argument("--days", type=int, default=90, help="backfill days (default 90)")
    args = parser.parse_args(argv)
    configure_logging("WARNING")
    ctx = build_context(args.fixtures, args.now)
    runs: dict[str, Any] = {
        "hourly_pull": lambda: hourly_pull(ctx),
        "daily_pipeline": lambda: daily_pipeline(ctx),
        "backfill": lambda: backfill(ctx, days=args.days),
        "search_terms": lambda: search_terms(ctx),
        "retention": lambda: retention(ctx),
        "health_checks": lambda: health_checks(ctx),
        "pacing": lambda: pacing(ctx),
    }
    out: dict[str, Any] = {}
    if args.job == "all":
        for job in (
            "backfill",
            "hourly_pull",
            "daily_pipeline",
            "search_terms",
            "pacing",
            "health_checks",
        ):
            out[job] = _summary(runs[job]())
    elif args.job != "counts":
        out[args.job] = _summary(runs[args.job]())
    out["counts"] = counts(ctx)
    json.dump(out, sys.stdout, indent=1, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
