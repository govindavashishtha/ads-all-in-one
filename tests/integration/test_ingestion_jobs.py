"""Ingestion end-to-end against synthetic fixtures in Postgres (plan §9, §9.2, §12.4, §23 P1).

Jobs open their own sessions and commit, so these tests use committed data for a dedicated
client id and delete it again afterwards (no audit_log rows are written, so cleanup is
possible and other integration tests see an unchanged database).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import sessionmaker

from engine.bootstrap import DEFAULT_LIMITS_PATH, DEFAULT_RULES_PATH, load_yaml
from engine.config import Settings
from engine.connectors.factory import ConnectorFactory
from engine.connectors.fakes import FixtureSet, synthetic_fixtures
from engine.enums import EntityType
from engine.ingest.manual import last_manual_change_at
from engine.jobs import ingestion
from engine.jobs.daily import daily_pipeline
from engine.jobs.pacing import pacing
from engine.jobs.runner import JobContext, StepState, job_lock

pytestmark = pytest.mark.integration

CID = "client_conn_it"
TODAY = dt.date(2026, 11, 11)
# 22:10 IST on the fixture "today" (fixture hours 0..22 exist)
T0 = dt.datetime(2026, 11, 11, 16, 40, tzinfo=dt.UTC)
DATA_TABLES = (
    "system_status",
    "job_requests",
    "notifications",
    "approvals",
    "proposals",
    "rule_stats",
    "scores",
    "features",
    "metrics_hourly",
    "metrics_daily",
    "metrics_window",
    "search_terms",
    "entity_history",
    "keywords",
    "ads",
    "ad_groups",
    "campaigns",
    "ad_accounts",
    "job_runs",
    "alerts",
    "change_log",
)
COUNTED = (
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
)
BASE_FIX = synthetic_fixtures(TODAY, 22, days=40, hourly_days=16)


class Clock:
    def __init__(self, t: dt.datetime) -> None:
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t


@pytest.fixture(scope="module")
def seeded(migrated_engine: Engine) -> Iterator[Engine]:
    rules = load_yaml(DEFAULT_RULES_PATH)
    limits = load_yaml(DEFAULT_LIMITS_PATH)
    with migrated_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO clients (client_id, name, timezone, currency, mode) "
                "VALUES (:c, 'IT', 'Asia/Kolkata', 'INR', 'read_only')"
            ),
            {"c": CID},
        )
        for kind, body in (("rules", rules), ("limits", limits)):
            c.execute(
                text(
                    "INSERT INTO config_versions (client_id, kind, version, body, active, "
                    "changed_by) VALUES (:c, :k, 1, CAST(:b AS jsonb), true, 'test')"
                ),
                {"c": CID, "k": kind, "b": json.dumps(body)},
            )
    yield migrated_engine
    with migrated_engine.begin() as c:
        _wipe(c)
        c.execute(text("DELETE FROM config_versions WHERE client_id = :c"), {"c": CID})
        c.execute(text("DELETE FROM clients WHERE client_id = :c"), {"c": CID})


def _wipe(c: Any) -> None:
    for t in DATA_TABLES:
        c.execute(text(f"DELETE FROM {t} WHERE client_id = :c"), {"c": CID})


@pytest.fixture
def env(seeded: Engine, make_settings: Callable[..., Settings]) -> Iterator[dict[str, Any]]:
    with seeded.begin() as c:
        _wipe(c)
    fx = copy.deepcopy(BASE_FIX)
    clock = Clock(T0)
    settings = make_settings(client_id=CID, heartbeat_url="https://heartbeat.invalid/ping")
    ctx = JobContext(
        settings=settings,
        engine=seeded,
        sessions=sessionmaker(bind=seeded, expire_on_commit=False),
        connectors=ConnectorFactory(settings, fixtures=fx, clock=clock),
        clock=clock,
    )
    yield {"ctx": ctx, "fx": fx, "clock": clock, "engine": seeded}


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        return {
            t: int(
                c.execute(
                    text(f"SELECT count(*) FROM {t} WHERE client_id = :c"), {"c": CID}
                ).scalar()
                or 0
            )
            for t in COUNTED
        }


def q(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(text(sql), {"c": CID, **params}))


def run_all(ctx: JobContext) -> list[Any]:
    out: list[Any] = []
    out += ingestion.backfill(ctx, days=35, hourly_days=15)
    out += ingestion.hourly_pull(ctx)
    out += daily_pipeline(ctx)
    out.append(ingestion.search_terms(ctx))
    out.append(pacing(ctx, ping=lambda url: None))
    out.append(ingestion.health_checks(ctx))
    out.append(ingestion.retention(ctx))
    return out


# ----------------------------------------------------------------------------- tests


def test_end_to_end_into_postgres_and_idempotent(env: dict[str, Any]) -> None:
    ctx, engine = env["ctx"], env["engine"]
    first = run_all(ctx)
    c1 = counts(engine)
    assert c1["ad_accounts"] == 2 and c1["campaigns"] == 8 and c1["keywords"] == 12
    assert c1["metrics_daily"] > 0 and c1["metrics_hourly"] > 0 and c1["metrics_window"] == 18
    assert c1["search_terms"] > 0 and c1["entity_history"] == 50  # one per entity
    assert {r.state for r in first if r is not None} <= {StepState.RAN, StepState.SKIPPED}

    # values land exactly (money in major units, one conversion action type)
    meta_row = next(
        r for r in BASE_FIX.meta["insights"]["account"] if r["date_start"] == "2026-11-09"
    )
    (got,) = q(
        engine,
        "SELECT spend, clicks, conversions FROM metrics_daily WHERE client_id=:c "
        "AND platform='meta' AND entity_type='account' AND date='2026-11-09'",
    )
    assert got.spend == Decimal(meta_row["spend"])
    assert got.clicks == int(meta_row["inline_link_clicks"])
    conv = next(a for a in meta_row["actions"] if a["action_type"] == BASE_FIX.conversion_action)
    assert got.conversions == Decimal(conv["value"])
    # hourly rows sum to the daily row for a complete day
    (h,) = q(
        engine,
        "SELECT sum(spend) AS s FROM metrics_hourly WHERE client_id=:c AND "
        "platform='google' AND entity_type='account' AND ts_hour::date='2026-11-09'",
    )
    (d,) = q(
        engine,
        "SELECT spend AS s FROM metrics_daily WHERE client_id=:c AND "
        "platform='google' AND entity_type='account' AND date='2026-11-09'",
    )
    assert h.s == d.s
    # Meta campaign learning derived from ad sets; Google from bidding system status
    learning = dict(q(engine, "SELECT external_id, learning FROM campaigns WHERE client_id=:c"))
    assert learning["900001"] is True and learning["900002"] is False
    assert learning["800002"] is True and learning["800001"] is False
    # daily pipeline: ingestion + Phase 2 steps ran; executor/evaluator still skipped,
    # triage (Phase 4) runs with the LLM disabled
    steps = dict(
        q(engine, "SELECT step, status FROM job_runs WHERE client_id=:c AND job='daily_pipeline'")
    )
    assert steps["repull"] == "success" and steps["windows"] == "success"
    assert steps["features"] == "success" and steps["score"] == "success"
    assert steps["rules"] == "success" and steps["digest"] == "success"
    assert steps["triage"] == "success" and steps["execute"] == "skipped"

    # re-run the same windows: every completed step is skipped, nothing changes
    second = run_all(ctx)
    done = [r for r in second if r is not None and r.status is not None]
    assert all(r.state is StepState.SKIPPED for r in done)  # only the not-implemented ones
    assert counts(engine) == c1

    # force a real re-run (job_runs cleared): upserts rewrite the same rows, no duplicates
    with engine.begin() as c:
        c.execute(text("DELETE FROM job_runs WHERE client_id = :c"), {"c": CID})
    run_all(ctx)
    assert counts(engine) == c1

    # next hour, nothing changed on the platform: no new history rows
    env["clock"].t = T0 + dt.timedelta(hours=1)
    ingestion.hourly_pull(ctx)
    assert counts(engine)["entity_history"] == c1["entity_history"]


def test_manual_change_detection(env: dict[str, Any]) -> None:
    ctx, engine, fx, clock = env["ctx"], env["engine"], env["fx"], env["clock"]
    ingestion.hourly_pull(ctx)
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE campaigns SET managed = true WHERE client_id = :c AND "
                "external_id IN ('900001', '900003')"
            ),
            {"c": CID},
        )
    camp = {r["id"]: r for r in fx.meta["campaigns"]}
    camp["900001"]["status"] = "PAUSED"  # managed, by a human
    camp["900002"]["status"] = "PAUSED"  # unmanaged
    camp["900003"]["status"] = "PAUSED"  # managed, by the engine (change_log below)
    fx.meta["activities"].append(
        {
            "event_type": "update_campaign_run_status",
            "event_time": "2026-11-11T22:50:00+0530",
            "actor_name": "Fixture Human",
            "object_id": "900001",
            "object_type": "CAMPAIGN",
            "application_name": "Ads Manager",
            "extra_data": '{"old_value": "ACTIVE", "new_value": "PAUSED"}',
        }
    )
    (c3,) = q(engine, "SELECT id FROM campaigns WHERE client_id=:c AND external_id='900003'")
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO change_log (client_id, platform, entity_type, entity_id, action, "
                "status, after, applied_at, applied_by) VALUES (:c, 'meta', 'campaign', :e, "
                "'pause', 'applied', CAST(:a AS jsonb), :t, 'executor')"
            ),
            {"c": CID, "e": c3.id, "a": '{"status": "paused"}', "t": T0 + dt.timedelta(minutes=30)},
        )
    before = counts(engine)["entity_history"]
    clock.t = T0 + dt.timedelta(hours=1)
    ingestion.hourly_pull(ctx)

    assert counts(engine)["entity_history"] == before + 3
    alerts = q(
        engine,
        "SELECT severity, kind, message, entity_id FROM alerts WHERE "
        "client_id=:c AND kind='manual_change'",
    )
    assert len(alerts) == 1
    (a,) = alerts
    assert a.severity == "info" and "900001" in a.message and "Fixture Human" in a.message
    assert "status active → paused" in a.message
    with sessionmaker(bind=engine)() as s:
        assert last_manual_change_at(s, CID, EntityType.CAMPAIGN, a.entity_id) is not None
        assert last_manual_change_at(s, CID, EntityType.CAMPAIGN, c3.id) is None


def test_advisory_lock_skips_concurrent_run(env: dict[str, Any]) -> None:
    ctx = env["ctx"]
    with job_lock(ctx.engine, CID, "hourly_pull") as got:
        assert got
        assert ingestion.hourly_pull(ctx) == []
    assert len(ingestion.hourly_pull(ctx)) == 2


def test_failed_step_rolls_back_records_error_and_stops_pipeline(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, engine = env["ctx"], env["engine"]
    ingestion.hourly_pull(ctx)
    real_build = ctx.connectors.build

    def broken(platform: Any, *, conversion_action: Any) -> Any:
        conn = real_build(platform, conversion_action=conversion_action)

        def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("platform 500")

        conn.daily_metrics = boom
        return conn

    monkeypatch.setattr(ctx.connectors, "build", broken)
    with pytest.raises(RuntimeError, match="platform 500"):
        daily_pipeline(ctx)
    rows = q(
        engine,
        "SELECT step, status, error FROM job_runs WHERE client_id=:c AND job='daily_pipeline'",
    )
    assert [(r.step, r.status) for r in rows] == [("repull", "failed")]
    assert "platform 500" in rows[0].error
    assert counts(engine)["metrics_daily"] == 0


def test_missing_conversion_action_keeps_entities_and_skips_meta_metrics(
    env: dict[str, Any],
) -> None:
    ctx, engine = env["ctx"], env["engine"]
    fx: FixtureSet = env["fx"]
    fx.conversion_action = None
    res = {r.step: r for r in ingestion.hourly_pull(ctx)}
    assert res["meta"].state is StepState.SKIPPED and "I-1" in (res["meta"].detail or "")
    assert res["google"].state is StepState.RAN
    assert (
        q(engine, "SELECT count(*) AS n FROM campaigns WHERE client_id=:c AND platform='meta'")[0].n
        == 4
    )
    assert (
        q(
            engine,
            "SELECT count(*) AS n FROM metrics_hourly WHERE client_id=:c AND platform='meta'",
        )[0].n
        == 0
    )
    assert q(engine, "SELECT dedupe_key FROM alerts WHERE client_id=:c")[0].dedupe_key == (
        "input_missing:meta:I-1"
    )


def test_real_mode_without_credentials_skips_with_alert(env: dict[str, Any]) -> None:
    ctx = env["ctx"]
    ctx.connectors = ConnectorFactory(ctx.settings)  # no fixtures, no creds in settings
    res = ingestion.hourly_pull(ctx)
    assert [r.state for r in res] == [StepState.SKIPPED, StepState.SKIPPED]
    keys = {
        r.dedupe_key for r in q(env["engine"], "SELECT dedupe_key FROM alerts WHERE client_id=:c")
    }
    assert keys == {"credentials_missing:meta", "credentials_missing:google"}


def test_daily_pipeline_skips_account_before_3am(env: dict[str, Any]) -> None:
    ctx, engine = env["ctx"], env["engine"]
    ingestion.hourly_pull(ctx)
    env["clock"].t = dt.datetime(2026, 11, 11, 20, 0, tzinfo=dt.UTC)  # 01:30 IST next day
    res = {r.step: r for r in daily_pipeline(ctx)}
    assert res["repull"].state is StepState.SKIPPED
    kinds = {r.kind for r in q(engine, "SELECT kind FROM alerts WHERE client_id=:c")}
    assert "data_not_ready" in kinds


def test_pacing_alert_only_with_heartbeat(env: dict[str, Any]) -> None:
    ctx, engine = env["ctx"], env["engine"]
    ingestion.backfill(ctx, days=20, hourly_days=15)
    ingestion.hourly_pull(ctx)
    pings: list[str] = []
    # lower caps in a new limits version (test data) so the emergency condition triggers
    with engine.begin() as c:
        body = c.execute(
            text(
                "SELECT body FROM config_versions WHERE client_id=:c AND kind='limits' AND active"
            ),
            {"c": CID},
        ).scalar()
        low = copy.deepcopy(body)
        low["caps"] = {
            "total": {"daily": 5000, "monthly": 60000},
            "meta": {"daily": 3000, "monthly": 30000},
            "google": {"daily": 3000, "monthly": 30000},
        }
        c.execute(
            text("UPDATE config_versions SET active = false WHERE client_id=:c AND kind='limits'"),
            {"c": CID},
        )
        c.execute(
            text(
                "INSERT INTO config_versions (client_id, kind, version, body, active, "
                "changed_by) VALUES (:c, 'limits', 2, CAST(:b AS jsonb), true, 'test')"
            ),
            {"c": CID, "b": json.dumps(low)},
        )
    try:
        res = pacing(ctx, ping=pings.append)
        assert res is not None and res.status is not None
        assert pings == ["https://heartbeat.invalid/ping"]
        status = {
            r.key: r.value
            for r in q(engine, "SELECT key, value FROM system_status WHERE client_id=:c")
        }
        assert status["heartbeat"]["last_status"] == "ok" and status["heartbeat"]["configured"]
        assert status["pacing:meta"]["local_date"] == "2026-11-11"
        assert status["pacing:meta"]["curve_source"] in ("learned", "linear")
        assert "projected_eod" in status["pacing:google"]
        alerts = {
            r.dedupe_key: r
            for r in q(
                engine, "SELECT dedupe_key, severity, message FROM alerts WHERE client_id=:c"
            )
        }
        assert alerts["emergency:meta:daily_cap:2026-11-11"].severity == "critical"
        assert "alert only" in alerts["emergency:meta:daily_cap:2026-11-11"].message
        assert "emergency:total:daily_cap:2026-11-11" in alerts
        assert alerts["pacing_warn:google:2026-11-11"].severity == "warn"
        # alert-only: nothing was paused or written to the change log
        assert q(engine, "SELECT count(*) AS n FROM change_log WHERE client_id=:c")[0].n == 0
        assert q(engine, "SELECT count(*) AS n FROM emergency_actions WHERE client_id=:c")[0].n == 0
        # same 15-min slot again: already done, no second ping, alerts stay deduped
        pacing(ctx, ping=pings.append)
        assert len(pings) == 1

        # next slot with a failing ping: the job still succeeds, the failure is recorded
        def bad_ping(url: str) -> None:
            raise RuntimeError("monitor down")

        env["clock"].t += dt.timedelta(minutes=15)
        res2 = pacing(ctx, ping=bad_ping)
        assert res2 is not None and res2.status is not None and res2.status.value != "failed"
        [hb] = q(engine, "SELECT value FROM system_status WHERE client_id=:c AND key='heartbeat'")
        assert hb.value["last_status"] == "failed" and "monitor down" in hb.value["error"]
    finally:
        with engine.begin() as c:
            c.execute(
                text(
                    "DELETE FROM config_versions WHERE client_id=:c AND kind='limits' AND version=2"
                ),
                {"c": CID},
            )
            c.execute(
                text(
                    "UPDATE config_versions SET active = true WHERE client_id=:c AND "
                    "kind='limits' AND version=1"
                ),
                {"c": CID},
            )


def test_retention_and_health(env: dict[str, Any]) -> None:
    ctx, engine = env["ctx"], env["engine"]
    ingestion.hourly_pull(ctx)
    (acct,) = q(engine, "SELECT id FROM ad_accounts WHERE client_id=:c AND platform='meta'")
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO metrics_hourly (client_id, platform, entity_type, entity_id, "
                "ts_hour) VALUES (:c, 'meta', 'account', :e, '2026-07-01 10:00')"
            ),
            {"c": CID, "e": acct.id},
        )
    res = ingestion.retention(ctx)
    assert res is not None and res.rows >= 1
    assert (
        q(
            engine,
            "SELECT count(*) AS n FROM metrics_hourly WHERE client_id=:c AND "
            "ts_hour < '2026-08-01'",
        )[0].n
        == 0
    )
    # health: fresh data -> no stale alert; 3 h later -> stale alerts for both platforms
    ingestion.health_checks(ctx)
    assert not q(engine, "SELECT 1 FROM alerts WHERE client_id=:c AND kind='data_stale'")
    env["clock"].t = T0 + dt.timedelta(hours=3)
    ingestion.health_checks(ctx)
    stale = q(engine, "SELECT dedupe_key FROM alerts WHERE client_id=:c AND kind='data_stale'")
    assert {r.dedupe_key for r in stale} == {"data_stale:meta", "data_stale:google"}
