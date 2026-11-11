"""DB-level guarantees from §8 that later agents rely on."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from engine.db.models import AuditLog, Client, ConfigVersion, JobRun, MetricsDaily, Proposal

pytestmark = pytest.mark.integration


@pytest.fixture
def client(db: Session) -> str:
    db.add(Client(client_id="c1", name="C", timezone="Asia/Kolkata", currency="INR"))
    db.flush()
    return "c1"


def test_audit_log_is_append_only(db: Session, client: str) -> None:
    row = AuditLog(client_id=client, actor="t", action="x")
    db.add(row)
    db.flush()
    for stmt in ("UPDATE audit_log SET actor='y'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        sp = db.begin_nested()
        with pytest.raises(DBAPIError, match="append-only"):
            db.execute(text(stmt))
        sp.rollback()


def test_one_active_config_version(db: Session, client: str) -> None:
    db.add(
        ConfigVersion(
            client_id=client, kind="rules", version=1, body={}, active=True, changed_by="t"
        )
    )
    db.flush()
    db.add(
        ConfigVersion(
            client_id=client, kind="rules", version=2, body={}, active=False, changed_by="t"
        )
    )
    db.flush()
    sp = db.begin_nested()
    db.add(
        ConfigVersion(
            client_id=client, kind="rules", version=3, body={}, active=True, changed_by="t"
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    sp.rollback()


def test_job_runs_unique_even_without_step(db: Session, client: str) -> None:
    db.add(JobRun(client_id=client, job="pacing", step=None, window_key="k", status="success"))
    db.flush()
    sp = db.begin_nested()
    db.add(JobRun(client_id=client, job="pacing", step=None, window_key="k", status="success"))
    with pytest.raises(IntegrityError):
        db.flush()
    sp.rollback()


def test_proposal_dedupe_on_conflict_do_nothing(db: Session, client: str) -> None:
    values = dict(
        client_id=client,
        dedupe_key="abc",
        as_of=dt.date(2026, 11, 10),
        source="rule",
        entity_type="ad",
        entity_id=1,
        action="pause",
        risk="low",
        spend_direction="reducing",
    )
    for _ in range(2):
        db.execute(
            insert(Proposal).values(**values).on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
    assert db.scalar(select(func.count()).select_from(Proposal)) == 1


def test_metrics_upsert_is_idempotent(db: Session, client: str) -> None:
    row = dict(
        client_id=client,
        platform="meta",
        entity_type="campaign",
        entity_id=7,
        date=dt.date(2026, 11, 10),
        impressions=10,
        clicks=1,
        spend=Decimal("12.34"),
        conversions=Decimal("1.5"),
        conv_value=Decimal("0"),
    )
    for spend in (Decimal("12.34"), Decimal("15.00")):
        stmt = insert(MetricsDaily).values(**{**row, "spend": spend})
        stmt = stmt.on_conflict_do_update(
            index_elements=["client_id", "platform", "entity_type", "entity_id", "date"],
            set_={"spend": stmt.excluded.spend, "pulled_at": func.now()},
        )
        db.execute(stmt)
    rows = db.scalars(select(MetricsDaily)).all()
    assert len(rows) == 1 and rows[0].spend == Decimal("15.00")


def test_check_constraints_reject_unknown_values(db: Session, client: str) -> None:
    sp = db.begin_nested()
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE clients SET mode='turbo' WHERE client_id='c1'"))
    sp.rollback()


def test_timestamps_are_timestamptz(db: Session) -> None:
    rows = db.execute(
        text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND data_type LIKE 'timestamp%'"
        )
    ).all()
    naive = {(t, c) for t, c, d in rows if d == "timestamp without time zone"}
    assert naive == {("metrics_hourly", "ts_hour")}  # account-local hour by design (§0 rule 9)
