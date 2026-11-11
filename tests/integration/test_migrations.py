"""Alembic upgrade/downgrade on a real Postgres + model/migration drift check (§8)."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect

from engine.db.base import Base
from engine.db.models import ALL_TABLES
from tests.conftest import run_alembic

pytestmark = pytest.mark.integration


def test_head_has_every_plan_table(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names())
    assert set(ALL_TABLES) <= tables
    # §8.3 + notifications (D-17) + job_requests, system_status (0003) + write_requests (0004)
    assert len(ALL_TABLES) == 29


def test_models_match_migrations(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff


def test_downgrade_then_upgrade(pg_url: str, migrated_engine: Engine) -> None:
    run_alembic(pg_url, "downgrade", "base")
    eng = create_engine(pg_url)
    try:
        assert set(inspect(eng).get_table_names()) <= {"alembic_version"}
        with eng.connect() as conn:
            types = conn.exec_driver_sql(
                "SELECT typname FROM pg_type WHERE typname IN ('platform','entity_type')"
            ).all()
        assert types == []
    finally:
        eng.dispose()
        run_alembic(pg_url, "upgrade", "head")
    eng = create_engine(pg_url)
    try:
        assert set(ALL_TABLES) <= set(inspect(eng).get_table_names())
    finally:
        eng.dispose()
