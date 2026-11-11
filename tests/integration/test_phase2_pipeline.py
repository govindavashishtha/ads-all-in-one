"""Phase 2 end-to-end on synthetic fixtures (plan §9.1, §10–§14, §23 P2).

daily_pipeline → features, scores, rules, guardrail, approvals, digest; idempotency;
suggest-mode approvals (request / remind / expire / approve + recheck); log-only notify;
weekly allocation. Uses committed data for a dedicated client id and removes it afterwards
(``decide`` writes append-only audit_log rows, so it runs inside a rolled-back transaction).
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
from sqlalchemy.orm import Session, sessionmaker

from engine.approvals.service import decide, expire_and_remind, recheck_approved
from engine.bootstrap import DEFAULT_LIMITS_PATH, DEFAULT_RULES_PATH, load_yaml
from engine.config import Settings
from engine.connectors.factory import ConnectorFactory
from engine.connectors.fakes import synthetic_fixtures
from engine.guardrails.check import Allow, Block
from engine.jobs import ingestion
from engine.jobs.daily import daily_pipeline
from engine.jobs.runner import JobContext
from engine.jobs.weekly import notify_dispatch, weekly_strategy
from engine.notify.adapters import OutboundMessage, SlackChannel
from engine.notify.dispatch import deliver_pending

pytestmark = pytest.mark.integration

CID = "client_phase2_it"
TODAY = dt.date(2026, 11, 11)
T0 = dt.datetime(2026, 11, 11, 1, 0, tzinfo=dt.UTC)  # 06:30 IST, the morning after as_of
FIX = synthetic_fixtures(TODAY, 22, days=60, hourly_days=3)
TABLES = (
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
PHASE2 = ("features", "scores", "proposals", "approvals", "notifications")
SCORING_STEPS = ("features", "score", "rules", "guardrail", "approvals", "digest")


def _wipe(c: Any) -> None:
    for t in TABLES:
        c.execute(text(f"DELETE FROM {t} WHERE client_id = :c"), {"c": CID})


@pytest.fixture(scope="module")
def env(migrated_engine: Engine) -> Iterator[dict[str, Any]]:
    with migrated_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO clients (client_id, name, timezone, currency, mode) "
                "VALUES (:c, 'P2', 'Asia/Kolkata', 'INR', 'read_only')"
            ),
            {"c": CID},
        )
        for kind, path in (("rules", DEFAULT_RULES_PATH), ("limits", DEFAULT_LIMITS_PATH)):
            c.execute(
                text(
                    "INSERT INTO config_versions (client_id, kind, version, body, active, "
                    "changed_by) VALUES (:c, :k, 1, CAST(:b AS jsonb), true, 'test')"
                ),
                {"c": CID, "k": kind, "b": json.dumps(load_yaml(path))},
            )
    yield {"engine": migrated_engine}
    with migrated_engine.begin() as c:
        _wipe(c)
        c.execute(text("DELETE FROM config_versions WHERE client_id = :c"), {"c": CID})
        c.execute(text("DELETE FROM clients WHERE client_id = :c"), {"c": CID})


class Clock:
    def __init__(self, t: dt.datetime) -> None:
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t


def _ctx(engine: Engine, settings: Settings, clock: Clock) -> JobContext:
    return JobContext(
        settings=settings,
        engine=engine,
        sessions=sessionmaker(bind=engine, expire_on_commit=False),
        connectors=ConnectorFactory(settings, fixtures=copy.deepcopy(FIX), clock=clock),
        clock=clock,
    )


def q(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(text(sql), {"c": CID, **params}))


def q_in(conn: Any, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params).scalar()


def counts(engine: Engine) -> dict[str, int]:
    return {t: q(engine, f"SELECT count(*) FROM {t} WHERE client_id=:c")[0][0] for t in PHASE2}


def ex(engine: Engine, sql: str, **params: Any) -> None:
    with engine.begin() as c:
        c.execute(text(sql), {"c": CID, **params})


def force_rerun(engine: Engine) -> None:
    ex(
        engine,
        "DELETE FROM job_runs WHERE client_id=:c AND job='daily_pipeline' AND step = ANY(:s)",
        s=list(SCORING_STEPS),
    )


def test_phase2_pipeline(env: dict[str, Any], make_settings: Callable[..., Settings]) -> None:
    engine: Engine = env["engine"]
    clock = Clock(T0)
    settings = make_settings(client_id=CID, next_public_base_path="/c/p2")
    ctx = _ctx(engine, settings, clock)
    ingestion.backfill(ctx, days=60, hourly_days=2)
    ingestion.hourly_pull(ctx)
    ingestion.search_terms(ctx)

    # ---- read_only run: everything produced, every actionable proposal blocked -------------
    daily_pipeline(ctx)
    steps = dict(
        q(engine, "SELECT step, status FROM job_runs WHERE client_id=:c AND job='daily_pipeline'")
    )
    for step in SCORING_STEPS:
        assert steps[step] == "success", step
    assert steps["triage"] == "skipped" and steps["execute"] == "skipped"
    c1 = counts(engine)
    assert c1["features"] > 0 and c1["scores"] > 0 and c1["proposals"] > 0
    ranks = q(
        engine,
        "SELECT entity_type, count(*), max(rank_in_peer) FROM scores WHERE client_id=:c GROUP BY 1",
    )
    assert {r[0] for r in ranks} >= {"campaign", "ad", "keyword"}
    statuses = dict(
        q(engine, "SELECT status, count(*) FROM proposals WHERE client_id=:c GROUP BY 1")
    )
    assert set(statuses) <= {"blocked", "escalated"}
    reasons = {
        r[0]
        for r in q(
            engine,
            "SELECT guardrail_result->>'reason' FROM proposals WHERE client_id=:c "
            "AND status='blocked'",
        )
    }
    assert reasons <= {"mode_read_only"}
    (digest,) = q(
        engine,
        "SELECT dedupe_key, body_text, body_html FROM notifications WHERE client_id=:c "
        "AND kind='digest'",
    )
    assert digest[0] == "digest:2026-11-10" and "daily digest" in digest[1]
    assert digest[2].startswith("<!doctype html>") or "<html" in digest[2]
    assert c1["approvals"] == 0

    # ---- idempotency: same window skipped; forced rerun creates nothing new ----------------
    daily_pipeline(ctx)
    assert counts(engine) == c1
    force_rerun(engine)
    daily_pipeline(ctx)
    assert counts(engine) == c1

    # ---- suggest mode with managed campaigns (synthetic opt-in, test only) -----------------
    ex(engine, "UPDATE clients SET mode='suggest' WHERE client_id=:c")
    ex(engine, "UPDATE campaigns SET managed=true WHERE client_id=:c")
    for t in ("notifications", "approvals", "proposals"):
        ex(engine, f"DELETE FROM {t} WHERE client_id=:c")
    force_rerun(engine)
    daily_pipeline(ctx)
    waiting = q(
        engine,
        "SELECT id, expires_at FROM proposals WHERE client_id=:c AND status='awaiting_approval' "
        "ORDER BY id",
    )
    assert waiting, "suggest mode should produce proposals awaiting approval"
    assert all(w[1] == T0 + dt.timedelta(hours=24) for w in waiting)
    n_wait = len(waiting)
    assert q(engine, "SELECT count(*) FROM approvals WHERE client_id=:c")[0][0] == n_wait
    assert (
        q(
            engine,
            "SELECT count(*) FROM notifications WHERE client_id=:c AND kind='approval_request'",
        )[0][0]
        == n_wait
    )
    (chan,) = {r[0] for r in q(engine, "SELECT channel FROM approvals WHERE client_id=:c")}
    assert chan == "dashboard"  # nothing configured → dashboard only
    (body,) = q(
        engine,
        "SELECT body_text FROM notifications WHERE client_id=:c AND kind='approval_request' "
        "ORDER BY id LIMIT 1",
    )
    assert f"/c/p2/approvals/{waiting[0][0]}" in body[0]
    assert (
        q(engine, "SELECT count(*) FROM proposals WHERE client_id=:c AND status='escalated'")[0][0]
        == q(engine, "SELECT count(*) FROM proposals WHERE client_id=:c AND action='escalate'")[0][
            0
        ]
    )
    snapshot = counts(engine)
    force_rerun(engine)
    daily_pipeline(ctx)
    assert counts(engine) == snapshot  # rerun in suggest mode: no duplicate approvals/rows

    # ---- notify: log only, even when Slack is configured but has no transport --------------
    out = notify_dispatch(ctx)
    assert out["failed"] == 0 and out["sent"] == snapshot["notifications"]
    assert {
        tuple(r[0]) for r in q(engine, "SELECT sent_channels FROM notifications WHERE client_id=:c")
    } == {("log",)}
    slack_settings = make_settings(client_id=CID, slack_webhook_url="https://hooks.slack.invalid/x")
    assert not SlackChannel().deliverable(slack_settings)
    seen: list[OutboundMessage] = []
    ex(engine, "UPDATE notifications SET status='queued', sent_channels='{}' WHERE client_id=:c")
    with Session(engine) as s:
        res = deliver_pending(s, slack_settings, T0, channels=[SlackChannel(seen.append)])
        s.commit()
    assert res["sent"] == snapshot["notifications"] and len(seen) == res["sent"]

    # ---- approvals: remind at 12 h, approve one (recheck), expire the rest at 24 h ----------
    first_id = waiting[0][0]
    with Session(engine) as s:
        assert expire_and_remind(s, settings, T0 + dt.timedelta(hours=11))["reminded"] == 0
        assert expire_and_remind(s, settings, T0 + dt.timedelta(hours=13))["reminded"] == n_wait
        assert expire_and_remind(s, settings, T0 + dt.timedelta(hours=14))["reminded"] == 0
        s.commit()
    # decide() writes audit_log (append-only): approve + recheck inside a rolled-back txn
    with engine.connect() as conn:
        outer = conn.begin()
        s2 = Session(bind=conn, join_transaction_mode="create_savepoint")
        p = decide(
            s2,
            client_id=CID,
            proposal_id=first_id,
            decision="approved",
            actor="tester",
            role="owner",
            via="dashboard",
            comment=None,
            now=T0 + dt.timedelta(hours=14),
            ip=None,
        )
        assert p.status == "approved"
        r = recheck_approved(s2, settings, first_id, T0 + dt.timedelta(hours=14))
        assert isinstance(r, Allow | Block)  # suggest + approved: Allow unless state changed
        s2.flush()
        assert q_in(conn, "SELECT status FROM proposals WHERE id=:i", i=first_id) in (
            "approved",
            "blocked",
        )
        s2.close()
        outer.rollback()
    with Session(engine) as s:
        out2 = expire_and_remind(s, settings, T0 + dt.timedelta(hours=25))
        s.commit()
    assert out2["expired"] == n_wait
    st = dict(q(engine, "SELECT status, count(*) FROM proposals WHERE client_id=:c GROUP BY 1"))
    assert st.get("expired") == n_wait
    assert st.get("awaiting_approval", 0) == 0


def test_weekly_allocation(env: dict[str, Any], make_settings: Callable[..., Settings]) -> None:
    """Pool of two managed Google campaigns with healthy pacing → zero-sum budget proposals."""
    engine: Engine = env["engine"]
    clock = Clock(T0)
    ctx = _ctx(engine, make_settings(client_id=CID), clock)
    if not q(engine, "SELECT 1 FROM scores WHERE client_id=:c LIMIT 1"):
        ingestion.backfill(ctx, days=60, hourly_days=2)
        daily_pipeline(ctx)
    ex(engine, "UPDATE clients SET mode='suggest' WHERE client_id=:c")
    pool_ids = [
        r[0]
        for r in q(
            engine,
            "SELECT c.id FROM campaigns c JOIN scores s ON s.entity_type='campaign' "
            "AND s.entity_id=c.id AND s.client_id=c.client_id WHERE c.client_id=:c "
            "AND c.platform='google' AND c.status='active' AND c.budget_level='campaign' "
            "AND NOT coalesce(c.learning, true) AND s.data_sufficient ORDER BY c.id",
        )
    ]
    pool_ids = sorted(set(pool_ids))[:2]
    assert len(pool_ids) == 2
    ex(
        engine,
        "UPDATE campaigns SET managed=true, pool='it_pool' WHERE client_id=:c AND id = ANY(:i)",
        i=pool_ids,
    )
    # healthy pacing so increases are eligible (§10.7 step 5)
    ex(
        engine,
        "UPDATE features SET spend_pace_7d = 1.0 WHERE client_id=:c AND entity_type='campaign' "
        "AND entity_id = ANY(:i)",
        i=pool_ids,
    )
    weekly_strategy(ctx)
    rows = q(
        engine,
        "SELECT entity_id, params, status FROM proposals WHERE client_id=:c "
        "AND source='allocation'",
    )
    budgets = dict(
        q(
            engine,
            "SELECT id, daily_budget FROM campaigns WHERE client_id=:c AND id = ANY(:i)",
            i=pool_ids,
        )
    )
    assert rows, "two pooled campaigns with different posteriors should move budget"
    deltas = [Decimal(str(r[1]["change_pct"])) * budgets[r[0]] / 100 for r in rows]
    assert abs(sum(deltas)) < Decimal("0.05")  # zero-sum up to change_pct rounding
    assert {r[2] for r in rows} <= {"awaiting_approval", "blocked"}
    before = counts(engine)
    weekly_strategy(ctx)
    assert counts(engine) == before  # same week: skipped
