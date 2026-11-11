"""Phase 4 LLM layer on Postgres with synthetic fixtures (plan §9.1 step 7, §13, §23 P4):

- the full daily_pipeline completes with the LLM disabled (escalations → flag_human);
- with the deterministic FakeLLM, triage turns escalations into proposals that the guardrail
  step then judges, the analyst path works, and every call is on the ``llm_calls`` ledger;
- the three caps stop calls (hard stop + one alert), the 24 h prompt cache, provider errors;
- weekly creative drafts become high-risk ``new_ad`` proposals that need a human.
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

from engine.bootstrap import DEFAULT_LIMITS_PATH, DEFAULT_RULES_PATH, load_yaml
from engine.config import Settings
from engine.connectors.factory import ConnectorFactory
from engine.connectors.fakes import synthetic_fixtures
from engine.enums import LLMTier
from engine.jobs import ingestion
from engine.jobs.daily import daily_pipeline
from engine.jobs.runner import JobContext
from engine.jobs.scoring_steps import guardrail_pending
from engine.llm import steps as llm_steps
from engine.llm.client import LLMClient, LLMMessage, LLMRequest, LLMResponse
from engine.llm.fake import FakeLLM, _analyst
from engine.llm.gateway import LLMGateway

pytestmark = pytest.mark.integration

CID = "client_llm_it"
TODAY = dt.date(2026, 11, 11)
AS_OF = dt.date(2026, 11, 10)
T0 = dt.datetime(2026, 11, 11, 1, 0, tzinfo=dt.UTC)  # 06:30 IST
FIX = synthetic_fixtures(TODAY, 22, days=60, hourly_days=3)
TABLES = (
    "llm_calls",
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
AFTER_RULES = ["triage", "guardrail", "execute", "approvals", "digest"]
# test-only prices (USD per MTok); real ones are INPUT I-8
PRICES = {
    "llm_small_in_price_per_mtok": "2",
    "llm_small_out_price_per_mtok": "10",
    "llm_large_in_price_per_mtok": "20",
    "llm_large_out_price_per_mtok": "100",
}


class Clock:
    def __init__(self, t: dt.datetime) -> None:
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t


def q(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as c:
        return list(c.execute(text(sql), {"c": CID, **params}))


def ex(engine: Engine, sql: str, **params: Any) -> None:
    with engine.begin() as c:
        c.execute(text(sql), {"c": CID, **params})


@pytest.fixture(scope="module")
def env(migrated_engine: Engine) -> Iterator[dict[str, Any]]:
    with migrated_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO clients (client_id, name, timezone, currency, mode) "
                "VALUES (:c, 'LLM', 'Asia/Kolkata', 'INR', 'suggest')"
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
        for t in TABLES:
            c.execute(text(f"DELETE FROM {t} WHERE client_id = :c"), {"c": CID})
        c.execute(text("DELETE FROM config_versions WHERE client_id = :c"), {"c": CID})
        c.execute(text("DELETE FROM clients WHERE client_id = :c"), {"c": CID})


def _ctx(engine: Engine, settings: Settings, clock: Clock) -> JobContext:
    return JobContext(
        settings=settings,
        engine=engine,
        sessions=sessionmaker(bind=engine, expire_on_commit=False),
        connectors=ConnectorFactory(settings, fixtures=copy.deepcopy(FIX), clock=clock),
        clock=clock,
    )


def _reset_triage(engine: Engine) -> None:
    """Forget triage results so the same escalations can be triaged again."""
    ex(engine, "DELETE FROM notifications WHERE client_id=:c")
    ex(engine, "DELETE FROM approvals WHERE client_id=:c")
    ex(engine, "DELETE FROM proposals WHERE client_id=:c AND source <> 'rule'")
    ex(engine, "UPDATE proposals SET params = params - 'triage' WHERE client_id=:c")
    ex(engine, "DELETE FROM alerts WHERE client_id=:c AND kind IN ('flag_human', 'llm_cap')")
    ex(
        engine,
        "DELETE FROM job_runs WHERE client_id=:c AND job='daily_pipeline' AND step = ANY(:s)",
        s=AFTER_RULES,
    )


def _steps(engine: Engine) -> dict[str, str]:
    return dict(
        q(engine, "SELECT step, status FROM job_runs WHERE client_id=:c AND job='daily_pipeline'")
    )


def _escalations(engine: Engine) -> list[Any]:
    return q(
        engine,
        "SELECT id, entity_type, entity_id, params->'triage' FROM proposals "
        "WHERE client_id=:c AND status='escalated' AND as_of=:d ORDER BY id",
        d=AS_OF,
    )


def _add_escalation(engine: Engine, rule: str, entity_type: str, entity_id: int) -> int:
    with engine.begin() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO proposals (client_id, dedupe_key, as_of, source, rule_key, "
                    "entity_type, entity_id, action, params, reason, risk, spend_direction, "
                    "status) VALUES (:c, :k, :d, 'rule', :r, :et, :eid, 'escalate', '{}', "
                    "'test escalation', 'medium', 'neutral', 'escalated') RETURNING id"
                ),
                {
                    "c": CID,
                    "k": f"test:{rule}:{entity_type}:{entity_id}",
                    "d": AS_OF,
                    "r": rule,
                    "et": entity_type,
                    "eid": entity_id,
                },
            ).scalar_one()
        )


@pytest.fixture(scope="module")
def loaded(env: dict[str, Any], migrated_engine: Engine) -> dict[str, Any]:
    from engine.config import Settings as S

    settings = S(
        _env_file=None,
        app_env="development",
        client_id=CID,
        timezone="Asia/Kolkata",
        hard_daily_cap="100000",
        hard_monthly_cap="3000000",
    )
    ctx = _ctx(migrated_engine, settings, Clock(T0))
    ingestion.backfill(ctx, days=60, hourly_days=2)
    ingestion.hourly_pull(ctx)
    ingestion.search_terms(ctx)
    ex(migrated_engine, "UPDATE campaigns SET managed=true WHERE client_id=:c")
    # a budget-unit ad group escalation (exercises budget_change / analyst paths)
    (unit,) = q(
        migrated_engine,
        "SELECT g.id FROM ad_groups g JOIN campaigns c ON c.id = g.campaign_id "
        "WHERE g.client_id=:c AND c.budget_level='ad_set' AND g.status='active' "
        "AND c.status='active' ORDER BY g.id LIMIT 1",
    )[0:1]
    return {"engine": migrated_engine, "unit_ad_group": int(unit[0])}


def test_pipeline_completes_with_llm_disabled(
    loaded: dict[str, Any], make_settings: Callable[..., Settings]
) -> None:
    engine = loaded["engine"]
    ctx = _ctx(engine, make_settings(client_id=CID), Clock(T0))  # LLM_PROVIDER empty
    daily_pipeline(ctx)
    steps = _steps(engine)
    for s in ("features", "score", "rules", "triage", "guardrail", "approvals", "digest"):
        assert steps[s] == "success", s
    _add_escalation(engine, "cpa_spike_anomaly", "ad_group", loaded["unit_ad_group"])
    _reset_triage(engine)
    daily_pipeline(ctx)
    escs = _escalations(engine)
    assert len(escs) >= 2
    assert all(e[3]["outcome"] == "flag_human" and "disabled" in e[3]["cause"] for e in escs)
    assert q(engine, "SELECT count(*) FROM llm_calls WHERE client_id=:c")[0][0] == 0
    flagged = q(engine, "SELECT count(*) FROM alerts WHERE client_id=:c AND kind='flag_human'")
    assert flagged[0][0] == len(escs)
    assert _steps(engine)["digest"] == "success"


def test_fake_llm_triage_creates_guardrailed_proposals_and_ledger(
    loaded: dict[str, Any], make_settings: Callable[..., Settings]
) -> None:
    engine = loaded["engine"]
    _reset_triage(engine)
    settings = make_settings(client_id=CID, llm_provider="fake", **PRICES)
    daily_pipeline(_ctx(engine, settings, Clock(T0)))
    assert _steps(engine)["triage"] == "success"
    escs = _escalations(engine)
    outcomes = {e[3]["outcome"] for e in escs}
    assert outcomes <= {"proposal", "noop", "flag_human"} and "proposal" in outcomes
    llm_props = q(
        engine,
        "SELECT id, source, action, status, risk, params, guardrail_result FROM proposals "
        "WHERE client_id=:c AND source IN ('llm_triage', 'llm_analyst') ORDER BY id",
    )
    assert llm_props
    for p in llm_props:
        assert p.status in {"awaiting_approval", "blocked"}  # judged by the guardrail step
        assert p.guardrail_result is not None and p.risk == "medium"
        assert p.params["escalation_id"] in {e[0] for e in escs}
    ledger = q(
        engine,
        "SELECT tier, model, purpose, input_tokens, output_tokens, cost_usd, status, job, "
        "created_at FROM llm_calls WHERE client_id=:c ORDER BY id",
    )
    assert ledger and all(r.status == "ok" and r.job == "daily_pipeline" for r in ledger)
    assert all(r.input_tokens > 0 and r.output_tokens > 0 and r.cost_usd > 0 for r in ledger)
    assert ledger[0].model == "fake-small" and ledger[0].created_at == T0
    (digest,) = q(
        engine,
        "SELECT body_text FROM notifications WHERE client_id=:c AND kind='digest' "
        "AND dedupe_key=:k",
        k=f"digest:{AS_OF.isoformat()}",
    )
    assert "LLM cost yesterday" in digest[0]
    # triage calls are logged on the run day (T0), so they show under "today so far"
    today_cost = sum(r.cost_usd for r in ledger)
    assert f"today so far (incl. this run's triage): ${today_cost:.5f}" in digest[0]


def test_analyst_path_via_escalate_large(
    loaded: dict[str, Any], make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = loaded["engine"]
    _reset_triage(engine)
    ex(engine, "DELETE FROM llm_calls WHERE client_id=:c")

    def script(req: LLMRequest) -> str:
        payload = json.loads(req.messages[-1].content)
        if req.purpose == "triage":
            return json.dumps(
                {
                    "results": [
                        {
                            "case_id": c["id"],
                            "action": "escalate_large",
                            "confidence": 0.9,
                            "reason": "needs root cause",
                        }
                        for c in payload["cases"]
                    ]
                }
            )
        return json.dumps(_analyst(payload))

    settings = make_settings(client_id=CID, llm_provider="fake", **PRICES)
    monkeypatch.setattr(llm_steps, "client_factory", lambda s: (FakeLLM(s, script), "fake"))
    daily_pipeline(_ctx(engine, settings, Clock(T0)))
    tiers = [r[0] for r in q(engine, "SELECT tier FROM llm_calls WHERE client_id=:c ORDER BY id")]
    assert tiers == ["small", "large"]
    unit = loaded["unit_ad_group"]
    (row,) = q(
        engine,
        "SELECT source, action, params->>'change_pct', status FROM proposals "
        "WHERE client_id=:c AND source='llm_analyst' AND entity_id=:e",
        e=unit,
    )
    assert row[0:3] == ("llm_analyst", "budget_change", "-10") and row[3] != "pending"
    escs = {e[0]: e[3] for e in _escalations(engine)}
    assert all(t["tier"] == "analyst" for t in escs.values())


def test_token_cap_is_a_hard_stop(
    loaded: dict[str, Any], make_settings: Callable[..., Settings]
) -> None:
    engine = loaded["engine"]
    _reset_triage(engine)
    ex(engine, "DELETE FROM llm_calls WHERE client_id=:c")  # no 24 h cache hits to fall back on
    before = 0
    settings = make_settings(client_id=CID, llm_provider="fake", llm_daily_token_cap=50, **PRICES)
    daily_pipeline(_ctx(engine, settings, Clock(T0)))
    assert _steps(engine)["digest"] == "success"  # pipeline still completes
    assert q(engine, "SELECT count(*) FROM llm_calls WHERE client_id=:c")[0][0] == before
    escs = _escalations(engine)
    assert all(e[3]["outcome"] == "flag_human" and "daily_tokens" in e[3]["cause"] for e in escs)
    (alert,) = q(
        engine, "SELECT dedupe_key, severity FROM alerts WHERE client_id=:c AND kind='llm_cap'"
    )
    assert alert == ("llm_cap:2026-11-11", "warn")


class Boom(LLMClient):
    def model_for(self, tier: LLMTier) -> str:
        return "boom"

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("provider down")


def _gw(engine: Engine, settings: Settings, client: LLMClient, t: dt.datetime) -> LLMGateway:
    return LLMGateway(
        settings,
        client,
        engine=engine,
        sessions=sessionmaker(bind=engine, expire_on_commit=False),
        clock=Clock(t),
        job="test",
    )


def _req(purpose: str = "triage", n: int = 1) -> LLMRequest:
    return LLMRequest(
        tier=LLMTier.SMALL,
        purpose=purpose,
        system="sys",
        messages=(LLMMessage("user", json.dumps({"cases": [], "n": n})),),
        max_output_tokens=50,
    )


def test_cost_caps_cache_and_errors(
    loaded: dict[str, Any], make_settings: Callable[..., Settings]
) -> None:
    engine = loaded["engine"]
    ex(engine, "DELETE FROM llm_calls WHERE client_id=:c")
    ex(engine, "DELETE FROM alerts WHERE client_id=:c AND kind='llm_cap'")
    s = make_settings(client_id=CID, llm_provider="fake", **PRICES)
    t = dt.datetime(2026, 11, 20, 6, 0, tzinfo=dt.UTC)
    gw = _gw(engine, s, FakeLLM(s, script=lambda _r: "{}"), t)

    first = gw.call(_req())
    assert first.status == "ok" and first.cost_usd > 0
    again = gw.call(_req())
    assert again.status == "cache_hit" and again.llm_call_id == first.llm_call_id
    assert q(engine, "SELECT count(*) FROM llm_calls WHERE client_id=:c")[0][0] == 1
    # invalid outputs are kept on the ledger but never served from cache
    gw.mark_invalid(first.llm_call_id, "test")
    assert gw.call(_req()).status == "ok"
    # 24 h later the cache has expired
    later = _gw(engine, s, FakeLLM(s, script=lambda _r: "{}"), t + dt.timedelta(hours=25))
    assert later.call(_req(n=1)).status == "ok"

    def add_cost(at: dt.datetime, usd: str) -> None:
        ex(
            engine,
            "INSERT INTO llm_calls (client_id, created_at, job, tier, model, purpose, "
            "input_tokens, output_tokens, cost_usd, status) VALUES "
            "(:c, :t, 'seed', 'small', 'seed', 'triage', 0, 0, :u, 'ok')",
            t=at,
            u=Decimal(usd),
        )

    add_cost(t, "0.9999")  # today: daily cost cap ($1.00) is now effectively used up
    r = gw.call(_req(n=2))
    assert r.status == "skipped_budget" and r.detail.startswith("daily_cost")
    next_day = t + dt.timedelta(days=1)
    gw2 = _gw(engine, s, FakeLLM(s, script=lambda _r: "{}"), next_day)
    assert gw2.call(_req(n=3)).status == "ok"  # new client-tz day, daily caps reset
    add_cost(dt.datetime(2026, 11, 2, tzinfo=dt.UTC), "19.0")  # month: $20 cap almost used
    r = gw2.call(_req(n=4))
    assert r.status == "skipped_budget" and r.detail.startswith("monthly_cost")
    keys = {
        k[0]
        for k in q(engine, "SELECT dedupe_key FROM alerts WHERE client_id=:c AND kind='llm_cap'")
    }
    assert keys == {"llm_cap:2026-11-20", "llm_cap:2026-11-21"}  # one per day

    ex(engine, "DELETE FROM llm_calls WHERE client_id=:c")
    err = _gw(engine, s, Boom(s), t).call(_req(n=5))
    assert err.status == "error" and not err.has_text
    assert q(engine, "SELECT status, input_tokens FROM llm_calls WHERE client_id=:c")[0] == (
        "error",
        0,
    )


def test_real_provider_without_prices_never_calls(
    loaded: dict[str, Any], make_settings: Callable[..., Settings]
) -> None:
    from engine.llm.providers import build_client

    s = make_settings(
        client_id=CID,
        llm_provider="anthropic",
        llm_api_key="k",
        llm_small_model="m1",
        llm_large_model="m2",
    )
    choice = build_client(s)
    assert not choice.enabled and "I-8" in choice.reason


def test_weekly_creative_drafts_need_a_human(
    loaded: dict[str, Any], make_settings: Callable[..., Settings]
) -> None:
    engine = loaded["engine"]
    ads = q(
        engine,
        "SELECT a.id, a.ad_group_id FROM ads a JOIN ad_groups g ON g.id=a.ad_group_id "
        "JOIN campaigns c ON c.id=g.campaign_id JOIN features f ON f.client_id=a.client_id "
        "AND f.entity_type='ad' AND f.entity_id=a.id AND f.as_of=:d "
        "WHERE a.client_id=:c AND a.status='active' AND g.status='active' "
        "AND c.status='active' AND c.automation_scope='full' ORDER BY a.id LIMIT 2",
        d=AS_OF,
    )
    assert ads
    for ad_id, _g in ads:
        _add_escalation(engine, "creative_fatigue", "ad", ad_id)
    settings = make_settings(client_id=CID, llm_provider="fake", **PRICES)
    ctx = _ctx(engine, settings, Clock(T0))
    with ctx.sessions() as s, s.begin():
        out = llm_steps.creative_step(ctx, AS_OF, s)
        guardrail_pending(s, ctx, T0, AS_OF)
    assert out.rows >= 1, out.details
    rows = q(
        engine,
        "SELECT entity_type, entity_id, action, risk, status, params FROM proposals "
        "WHERE client_id=:c AND action='new_ad'",
    )
    assert {r.entity_id for r in rows} == {g for _a, g in ads}
    for r in rows:
        assert r.entity_type == "ad_group" and r.risk == "high"
        assert r.status in {"awaiting_approval", "blocked"}  # never auto-approved
        assert r.params["create_status"] == "PAUSED"
    llm = q(
        engine, "SELECT tier, purpose, job FROM llm_calls WHERE client_id=:c AND purpose='creative'"
    )
    assert llm and llm[0] == ("large", "creative", "weekly_strategy")
    # idempotent: annotated escalations are not drafted again
    with ctx.sessions() as s, s.begin():
        again = llm_steps.creative_step(ctx, AS_OF, s)
    assert again.rows == 0
    assert isinstance(Session, type)
