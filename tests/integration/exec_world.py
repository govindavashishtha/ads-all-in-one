"""A small committed world for executor / evaluator / emergency / API write-path tests.

Meta: account act_1 (Asia/Kolkata, INR); campaigns C1, C2 (managed, campaign budgets 1000)
and C3 (unmanaged); ad set AS1 in C1 with ads AD1–AD3. Google: customer 123; campaign G1
(managed, budget 2000), ad group AG2 with ad AG2~AD5 and search term "free stuff".
Nothing here talks to a platform; the fake platforms are derived from these rows.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from engine.bootstrap import DEFAULT_LIMITS_PATH, DEFAULT_RULES_PATH, load_yaml
from engine.config import Settings
from engine.connectors.factory import ConnectorFactory
from engine.connectors.fakes_write import FakePlatform
from engine.db.models import (
    Ad,
    AdAccount,
    AdGroup,
    Campaign,
    EntityHistory,
    Proposal,
    SearchTerm,
)
from engine.enums import Access, Action, EntityType, Platform
from engine.executor.apply import WriteEnv
from engine.executor.clients import FakeWriteClients
from engine.executor.fake_world import fake_platforms_from_db
from engine.jobs.runner import JobContext
from engine.proposals import spend_direction

T0 = dt.datetime(2026, 11, 11, 6, 30, tzinfo=dt.UTC)  # 12:00 IST
TABLES = (
    "write_requests",
    "evaluations",
    "emergency_actions",
    "change_log",
    "notifications",
    "approvals",
    "rule_stats",
    "proposals",
    "metrics_daily",
    "metrics_hourly",
    "search_terms",
    "entity_history",
    "keywords",
    "ads",
    "ad_groups",
    "campaigns",
    "ad_accounts",
    "job_runs",
    "job_requests",
    "alerts",
    "system_status",
    "sessions",
    "config_versions",
)


class Clock:
    def __init__(self, t: dt.datetime) -> None:
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t = self.t + dt.timedelta(**kw)


@dataclass
class World:
    engine: Engine
    settings: Settings
    sessions: sessionmaker[Session]
    clock: Clock
    ids: dict[str, int]
    fakes: dict[Platform, FakePlatform]
    clients: FakeWriteClients

    @property
    def cid(self) -> str:
        return self.settings.client_id

    @property
    def meta(self) -> FakePlatform:
        return self.fakes[Platform.META]

    @property
    def google(self) -> FakePlatform:
        return self.fakes[Platform.GOOGLE]

    def env(self) -> WriteEnv:
        return WriteEnv(self.settings, self.sessions, self.clients, self.clock)

    def ctx(self) -> JobContext:
        return JobContext(
            self.settings,
            self.engine,
            self.sessions,
            ConnectorFactory(self.settings),
            self.clock,
            write_clients=self.clients,
        )

    def q(self, sql: str, **params: Any) -> list[Any]:
        with self.engine.connect() as c:
            return list(c.execute(text(sql), {"c": self.cid, **params}))

    def one(self, sql: str, **params: Any) -> Any:
        rows = self.q(sql, **params)
        return rows[0][0] if rows else None

    def ex(self, sql: str, **params: Any) -> None:
        with self.engine.begin() as c:
            c.execute(text(sql), {"c": self.cid, **params})

    def set_client(self, **values: Any) -> None:
        sets = ", ".join(f"{k} = :{k}" for k in values)
        self.ex(f"UPDATE clients SET {sets} WHERE client_id = :c", **values)

    def proposal(
        self,
        entity: str,
        action: Action,
        *,
        status: str = "approved",
        params: dict[str, Any] | None = None,
        risk: str = "low",
        rule_key: str | None = "test_rule",
        entity_type: EntityType | None = None,
    ) -> int:
        et = entity_type or _TYPE[entity[:2]]
        with self.sessions() as s, s.begin():
            p = Proposal(
                client_id=self.cid,
                dedupe_key=f"t:{uuid.uuid4().hex}",
                as_of=(T0 - dt.timedelta(days=1)).date(),
                source="rule",
                rule_key=rule_key,
                entity_type=et,
                entity_id=self.ids[entity],
                action=action.value,
                params=params or {},
                reason="test",
                risk=risk,
                spend_direction=spend_direction(action, params or {}).value,
                status=status,
            )
            s.add(p)
            s.flush()
            return int(p.id)


_TYPE = {
    "C1": EntityType.CAMPAIGN,
    "C2": EntityType.CAMPAIGN,
    "C3": EntityType.CAMPAIGN,
    "G1": EntityType.CAMPAIGN,
    "AS": EntityType.AD_GROUP,
    "AG": EntityType.AD_GROUP,
    "AD": EntityType.AD,
    "ST": EntityType.SEARCH_TERM,
}


def wipe(engine: Engine, cid: str) -> None:
    with engine.begin() as c:
        for t in TABLES:
            c.execute(text(f"DELETE FROM {t} WHERE client_id = :c"), {"c": cid})


def build_world(
    engine: Engine,
    settings: Settings,
    *,
    mode: str = "suggest",
    execution_enabled: bool = True,
    access: Access = Access.WRITE,
) -> World:
    cid = settings.client_id
    wipe(engine, cid)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO clients (client_id, name, timezone, currency, mode, "
                "execution_enabled) VALUES (:c, 'Exec', 'Asia/Kolkata', 'INR', :m, :e) "
                "ON CONFLICT (client_id) DO UPDATE SET mode = :m, execution_enabled = :e, "
                "kill_switch = false"
            ),
            {"c": cid, "m": mode, "e": execution_enabled},
        )
        for kind, path in (("rules", DEFAULT_RULES_PATH), ("limits", DEFAULT_LIMITS_PATH)):
            c.execute(
                text(
                    "INSERT INTO config_versions (client_id, kind, version, body, active, "
                    "changed_by) VALUES (:c, :k, 1, CAST(:b AS jsonb), true, 'test')"
                ),
                {"c": cid, "k": kind, "b": json.dumps(load_yaml(path))},
            )
    ids: dict[str, int] = {}
    with sessions() as s, s.begin():
        meta = AdAccount(
            client_id=cid,
            platform=Platform.META,
            external_id="act_1",
            currency="INR",
            timezone="Asia/Kolkata",
        )
        goog = AdAccount(
            client_id=cid,
            platform=Platform.GOOGLE,
            external_id="123",
            currency="INR",
            timezone="Asia/Kolkata",
        )
        s.add_all([meta, goog])
        s.flush()

        def camp(key: str, acct: AdAccount, ext: str, budget: str, managed: bool) -> Campaign:
            c = Campaign(
                client_id=cid,
                ad_account_id=acct.id,
                platform=acct.platform,
                external_id=ext,
                name=key,
                kind="standard",
                automation_scope="full",
                status="active",
                budget_level="campaign",
                daily_budget=Decimal(budget),
                learning=False,
                managed=managed,
            )
            s.add(c)
            s.flush()
            ids[key] = int(c.id)
            return c

        c1 = camp("C1", meta, "c1", "1000", True)
        camp("C2", meta, "c2", "1000", True)
        camp("C3", meta, "c3", "1000", False)
        g1 = camp("G1", goog, "g1", "2000", True)
        as1 = AdGroup(
            client_id=cid,
            campaign_id=c1.id,
            platform=Platform.META,
            external_id="as1",
            kind="ad_set",
            name="AS1",
            status="active",
            learning=False,
        )
        ag2 = AdGroup(
            client_id=cid,
            campaign_id=g1.id,
            platform=Platform.GOOGLE,
            external_id="ag2",
            kind="ad_group",
            name="AG2",
            status="active",
        )
        s.add_all([as1, ag2])
        s.flush()
        ids["AS1"], ids["AG2"] = int(as1.id), int(ag2.id)
        for i in (1, 2, 3):
            a = Ad(
                client_id=cid,
                ad_group_id=as1.id,
                platform=Platform.META,
                external_id=f"ad{i}",
                name=f"AD{i}",
                status="active",
            )
            s.add(a)
            s.flush()
            ids[f"AD{i}"] = int(a.id)
        a5 = Ad(
            client_id=cid,
            ad_group_id=ag2.id,
            platform=Platform.GOOGLE,
            external_id="ag2~ad5",
            name="AD5",
            status="active",
        )
        s.add(a5)
        st = SearchTerm(
            client_id=cid,
            ad_group_id=ag2.id,
            term="free stuff",
            date=(T0 - dt.timedelta(days=2)).date(),
            cost=Decimal("2000"),
        )
        s.add(st)
        s.flush()
        ids["AD5"], ids["ST1"] = int(a5.id), int(st.id)
        for key in ("C1", "C2", "C3", "G1"):
            s.add(
                EntityHistory(
                    client_id=cid,
                    entity_type=EntityType.CAMPAIGN,
                    entity_id=ids[key],
                    observed_at=T0 - dt.timedelta(days=2),
                    status="active",
                    daily_budget=Decimal("2000" if key == "G1" else "1000"),
                    learning=False,
                    source="hourly_pull",
                )
            )
    with sessions() as s:
        fakes = fake_platforms_from_db(s, cid)
    return World(
        engine, settings, sessions, Clock(T0), ids, fakes, FakeWriteClients(fakes, access=access)
    )
