"""API/CLI test fixtures: real Postgres (TEST_DATABASE_URL), app wired to the rolled-back
test session, controllable clock, env admin with a known test password."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.deps import get_db
from api.main import create_app
from engine.bootstrap import bootstrap
from engine.config import Settings
from engine.db import models as m
from engine.enums import Platform

PASSWORD = "test-only-password"
PASSWORD_HASH = PasswordHasher().hash(PASSWORD)
SECRET = "s" * 40
METRICS_TOKEN = "metrics-test-token"
BASE = "https://testserver"
CID = "client_test"
NOW = dt.datetime(2026, 11, 11, 6, 30, tzinfo=dt.UTC)  # 12:00 in Asia/Kolkata


@dataclass
class Clock:
    now: dt.datetime = NOW

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += dt.timedelta(**kw)


@pytest.fixture
def api_settings_factory(make_settings: Callable[..., Settings]) -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        base: dict[str, Any] = {
            "admin_user": "admin",
            "admin_password_hash": PASSWORD_HASH,
            "secret_key": SECRET,
            "metrics_token": METRICS_TOKEN,
            "session_ttl_minutes": 60,
        }
        base.update(overrides)
        return make_settings(**base)

    return _make


@pytest.fixture
def api_settings(api_settings_factory: Callable[..., Settings]) -> Settings:
    return api_settings_factory()


@pytest.fixture
def seeded(db: Session, api_settings: Settings) -> Session:
    bootstrap(db, api_settings)
    db.flush()
    return db


@pytest.fixture
def clock() -> Clock:
    return Clock()


def build_app(db: Session, settings: Settings, clock: Clock) -> FastAPI:
    app = create_app(settings=settings, session_factory=sessionmaker(), clock=clock)

    def _db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _db
    return app


@pytest.fixture
def app(seeded: Session, api_settings: Settings, clock: Clock) -> FastAPI:
    return build_app(seeded, api_settings, clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url=BASE) as c:
        yield c


def browser_login(client: TestClient) -> str:
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return str(r.json()["csrf_token"])


def bearer(client: TestClient, role: str | None = None) -> dict[str, str]:
    body: dict[str, Any] = {"username": "admin", "password": PASSWORD}
    if role:
        body["role"] = role
    r = client.post("/api/v1/auth/token", json=body)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def admin_h(client: TestClient) -> dict[str, str]:
    return bearer(client)


@pytest.fixture
def viewer_h(client: TestClient) -> dict[str, str]:
    return bearer(client, "viewer")


@pytest.fixture
def approver_h(client: TestClient) -> dict[str, str]:
    return bearer(client, "approver")


# --------------------------------------------------------------------------- world


@dataclass
class World:
    meta_account: int = 0
    google_account: int = 0
    meta_campaign: int = 0
    google_campaign: int = 0
    meta_adset: int = 0
    google_adgroup: int = 0
    meta_ads: list[int] = field(default_factory=list)
    keyword: int = 0
    proposal_waiting: int = 0
    proposal_waiting2: int = 0
    proposal_expired: int = 0
    proposal_pending: int = 0
    change: int = 0
    alert: int = 0
    emergency: int = 0


def _proposal(db: Session, key: str, status: str, entity_id: int, **kw: Any) -> int:
    p = m.Proposal(
        client_id=CID,
        dedupe_key=key,
        as_of=dt.date(2026, 11, 10),
        source="rule",
        rule_key="scale_winner",
        entity_type="campaign",
        entity_id=entity_id,
        action="budget_change",
        params={"change_pct": 15},
        reason="test",
        risk="low",
        spend_direction="increasing",
        status=status,
        **kw,
    )
    db.add(p)
    db.flush()
    return p.id


@pytest.fixture
def world(seeded: Session) -> World:
    """A small, hand-checkable data set (test values only, not business inputs)."""
    db = seeded
    w = World()
    tz = "Asia/Kolkata"
    accts = {}
    for platform, ext in ((Platform.META, "act_1"), (Platform.GOOGLE, "123")):
        a = m.AdAccount(
            client_id=CID,
            platform=platform,
            external_id=ext,
            name=f"{platform} acct",
            status="active",
            currency="INR",
            timezone=tz,
            last_synced_at=NOW - dt.timedelta(minutes=30),
        )
        db.add(a)
        db.flush()
        accts[platform] = a.id
    w.meta_account, w.google_account = accts[Platform.META], accts[Platform.GOOGLE]

    def campaign(platform: Platform, ext: str, budget_level: str, kind: str = "standard") -> int:
        c = m.Campaign(
            client_id=CID,
            ad_account_id=accts[platform],
            platform=platform,
            external_id=ext,
            name=f"{platform}-{ext}",
            objective="sales",
            kind=kind,
            automation_scope="full",
            status="active",
            budget_level=budget_level,
            daily_budget=Decimal("1000.00"),
            learning=False,
        )
        db.add(c)
        db.flush()
        return c.id

    w.meta_campaign = campaign(Platform.META, "c1", "ad_set")
    w.google_campaign = campaign(Platform.GOOGLE, "g1", "campaign")
    adset = m.AdGroup(
        client_id=CID,
        campaign_id=w.meta_campaign,
        platform=Platform.META,
        external_id="as1",
        kind="ad_set",
        name="adset 1",
        status="active",
        daily_budget=Decimal("500.00"),
        learning=None,
    )
    adgroup = m.AdGroup(
        client_id=CID,
        campaign_id=w.google_campaign,
        platform=Platform.GOOGLE,
        external_id="ag1",
        kind="ad_group",
        name="adgroup 1",
        status="active",
    )
    db.add_all([adset, adgroup])
    db.flush()
    w.meta_adset, w.google_adgroup = adset.id, adgroup.id
    for i in range(2):
        ad = m.Ad(
            client_id=CID,
            ad_group_id=adset.id,
            platform=Platform.META,
            external_id=f"ad{i}",
            name=f"ad {i}",
            status="active",
            headline=f"h{i}",
        )
        db.add(ad)
        db.flush()
        w.meta_ads.append(ad.id)
    kw = m.Keyword(
        client_id=CID,
        ad_group_id=adgroup.id,
        platform=Platform.GOOGLE,
        external_id="k1",
        text="running shoes",
        match_type="exact",
        status="enabled",
    )
    db.add(kw)
    db.flush()
    w.keyword = kw.id

    # metrics_daily: account level, Nov 1..11 for meta (100/day, 1 conv/day, value 300)
    for day in range(1, 12):
        d = dt.date(2026, 11, day)
        db.add(
            m.MetricsDaily(
                client_id=CID,
                platform=Platform.META,
                entity_type="account",
                entity_id=w.meta_account,
                date=d,
                impressions=1000,
                clicks=50,
                spend=Decimal("100.00"),
                conversions=Decimal("1"),
                conv_value=Decimal("300.00"),
            )
        )
        db.add(
            m.MetricsDaily(
                client_id=CID,
                platform=Platform.GOOGLE,
                entity_type="account",
                entity_id=w.google_account,
                date=d,
                impressions=500,
                clicks=25,
                spend=Decimal("50.00"),
                conversions=Decimal("0"),
                conv_value=Decimal("0"),
            )
        )
        db.add(
            m.MetricsDaily(
                client_id=CID,
                platform=Platform.META,
                entity_type="campaign",
                entity_id=w.meta_campaign,
                date=d,
                impressions=1000,
                clicks=50,
                spend=Decimal("100.00"),
                conversions=Decimal("1"),
                conv_value=Decimal("300.00"),
            )
        )
    # today (Nov 11) hourly for meta: 4 hours x 10 = 40 (overrides the daily 'today' row)
    for h in range(4):
        db.add(
            m.MetricsHourly(
                client_id=CID,
                platform=Platform.META,
                entity_type="account",
                entity_id=w.meta_account,
                ts_hour=dt.datetime(2026, 11, 11, h),
                impressions=100,
                clicks=5,
                spend=Decimal("10.00"),
            )
        )
    for i, ad_id in enumerate(w.meta_ads):
        db.add(
            m.Score(
                client_id=CID,
                entity_type="ad",
                entity_id=ad_id,
                as_of=dt.date(2026, 11, 10),
                cpa_shrunk=Decimal("400.00") + i,
                cpa_p10=Decimal("300.00"),
                cpa_p90=Decimal("500.00"),
                rank_in_peer=i + 1,
                peer_size=2,
                peer_group="as1",
                rank_metric=1.0 - i / 10,
                data_sufficient=True,
            )
        )
    # an older score must be ignored
    db.add(
        m.Score(
            client_id=CID,
            entity_type="ad",
            entity_id=w.meta_ads[0],
            as_of=dt.date(2026, 11, 1),
            cpa_shrunk=Decimal("999.00"),
            data_sufficient=False,
        )
    )

    w.proposal_waiting = _proposal(
        db, "k1", "awaiting_approval", w.meta_campaign, expires_at=NOW + dt.timedelta(hours=20)
    )
    db.add(m.Approval(client_id=CID, proposal_id=w.proposal_waiting, channel="slack"))
    w.proposal_waiting2 = _proposal(
        db, "k2", "awaiting_approval", w.google_campaign, expires_at=NOW + dt.timedelta(hours=20)
    )
    w.proposal_expired = _proposal(
        db, "k3", "awaiting_approval", w.meta_campaign, expires_at=NOW - dt.timedelta(minutes=1)
    )
    w.proposal_pending = _proposal(db, "k4", "pending", w.meta_campaign)

    ch = m.ChangeLog(
        client_id=CID,
        proposal_id=None,
        platform=Platform.META,
        entity_type="campaign",
        entity_id=w.meta_campaign,
        action="pause",
        status="applied",
        before={"status": "ACTIVE"},
        intended={"status": "PAUSED"},
        started_at=NOW - dt.timedelta(days=8),
        applied_at=NOW - dt.timedelta(days=8),
    )
    db.add(ch)
    db.flush()
    w.change = ch.id
    db.add_all(
        [
            m.Evaluation(
                client_id=CID,
                change_id=ch.id,
                horizon_days=3,
                due_at=NOW - dt.timedelta(days=5),
                done_at=NOW - dt.timedelta(days=5),
                unit_entity_type="campaign",
                unit_entity_id=w.meta_campaign,
                action_taken="none",
            ),
            m.Evaluation(
                client_id=CID,
                change_id=ch.id,
                horizon_days=7,
                due_at=NOW - dt.timedelta(days=1),
                done_at=NOW - dt.timedelta(days=1),
                unit_entity_type="campaign",
                unit_entity_id=w.meta_campaign,
                verdict="better",
                p_better=0.8,
                p_worse=0.05,
                action_taken="none",
            ),
        ]
    )
    al = m.Alert(
        client_id=CID,
        dedupe_key="pace:meta:2026-11-11",
        severity="warn",
        kind="pacing_warn",
        message="spend pace high",
    )
    db.add(al)
    db.add(
        m.Alert(
            client_id=CID,
            dedupe_key="old",
            severity="critical",
            kind="x",
            message="acked",
            acked_at=NOW - dt.timedelta(days=1),
            acked_by="admin",
        )
    )
    em = m.EmergencyAction(
        client_id=CID,
        platform=Platform.META,
        trigger="daily_cap",
        campaign_ids=[w.meta_campaign],
        restore_policy="auto_next_day",
    )
    db.add(em)
    db.add_all(
        [
            m.JobRun(
                client_id=CID,
                job="hourly_pull",
                step=None,
                window_key="2026-11-11T11",
                status="success",
                started_at=NOW - dt.timedelta(minutes=25),
                finished_at=NOW - dt.timedelta(minutes=24),
                rows_ingested=10,
            ),
            m.JobRun(
                client_id=CID,
                job="hourly_pull",
                step=None,
                window_key="2026-11-11T10",
                status="failed",
                started_at=NOW - dt.timedelta(minutes=85),
                error="boom",
            ),
            m.LLMCall(
                client_id=CID,
                job="daily_pipeline",
                tier="small",
                model="m-small",
                purpose="triage",
                input_tokens=1000,
                output_tokens=100,
                cost_usd=Decimal("0.00120"),
                status="ok",
                created_at=NOW - dt.timedelta(hours=1),
            ),
            m.LLMCall(
                client_id=CID,
                job="weekly_strategy",
                tier="large",
                model="m-large",
                purpose="analyst",
                input_tokens=10000,
                output_tokens=1000,
                cost_usd=Decimal("0.05000"),
                status="ok",
                created_at=NOW - dt.timedelta(days=3),
            ),
        ]
    )
    db.flush()
    w.alert, w.emergency = al.id, em.id
    return w
