"""Shared fixtures. Integration tests need TEST_DATABASE_URL (a disposable Postgres 16 DB)."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from engine.config import Settings

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Settings built only from explicit kwargs (ignores the developer's .env / env vars)."""

    def _make(**overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "app_env": "development",
            "client_id": "client_test",
            "client_name": "Test Client",
            "timezone": "Asia/Kolkata",
            "hard_daily_cap": "100000",  # test values, not business inputs
            "hard_monthly_cap": "3000000",
        }
        base.update(overrides)
        return Settings(_env_file=None, **base)

    return _make


# ----------------------------------------------------------------------- integration


def _alembic_config(url: str) -> Any:
    from alembic.config import Config

    cfg = Config(str(ROOT / "db" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "db" / "migrations"))
    return cfg


def run_alembic(url: str, command: str, revision: str) -> None:
    from alembic import command as alembic_command

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            cfg = _alembic_config(url)
            cfg.attributes["connection"] = conn
            getattr(alembic_command, command)(cfg, revision)
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def pg_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set (integration tests need Postgres 16)")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_engine(pg_url: str) -> Iterator[Engine]:
    """Fresh schema for the session: downgrade to base, then upgrade to head."""
    run_alembic(pg_url, "downgrade", "base")
    run_alembic(pg_url, "upgrade", "head")
    engine = create_engine(pg_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Session]:
    """Session inside an outer transaction that is always rolled back."""
    conn = migrated_engine.connect()
    outer = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        conn.close()
