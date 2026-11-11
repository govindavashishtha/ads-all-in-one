"""Contract tests: GoogleReadConnector against fixture rows (plan §8.2, §8.4, §9.2, §22)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from engine.connectors.base import EntityStatus
from engine.connectors.fakes import FakeGoogleTransport, FixtureSet, fixture_google_credentials
from engine.connectors.google_read import GoogleReadConnector, map_learning
from engine.connectors.resilience import OperationCounter, RetryPolicy
from engine.enums import AutomationScope, BudgetLevel, CampaignKind, EntityType, Platform

pytestmark = pytest.mark.contract
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "connectors" / "contract"
NOW = dt.datetime(2026, 11, 11, 8, 0, tzinfo=dt.UTC)


def make(limit: int = 2880):
    fx = FixtureSet.from_dir(FIX)
    t = FakeGoogleTransport(fx.google)
    conn = GoogleReadConnector(
        fixture_google_credentials("0000000001"),
        transport=t,
        clock=lambda: NOW,
        retry=RetryPolicy(sleep=lambda _s: None),
        counter=OperationCounter(Platform.GOOGLE, limit),
    )
    return conn, t


def test_campaigns() -> None:
    c = {r.external_id: r for r in make()[0].list_campaigns()}
    assert c["5001"].daily_budget == Decimal("500.00")  # micros (V-5)
    assert c["5001"].budget_level is BudgetLevel.CAMPAIGN
    assert c["5001"].learning is True  # LEARNING_NEW (V-7)
    assert c["5001"].kind is CampaignKind.STANDARD
    assert c["5002"].budget_level is BudgetLevel.SHARED
    assert c["5002"].shared_budget_external_id == "6002"
    assert c["5002"].daily_budget == Decimal("1234.56") and c["5002"].learning is False
    assert c["5003"].kind is CampaignKind.PMAX and c["5003"].learning is None
    assert c["5003"].automation_scope is AutomationScope.CAMPAIGN_ONLY
    assert c["5004"].kind is CampaignKind.OTHER and c["5004"].status is EntityStatus.REMOVED
    assert c["5004"].daily_budget is None


def test_learning_values() -> None:
    assert map_learning("MULTIPLE_LEARNING") is True
    assert map_learning("LIMITED_BY_BUDGET") is False
    assert map_learning("UNKNOWN") is None and map_learning(None) is None


def test_children_use_composite_ids() -> None:
    conn, _ = make()
    (ag,) = conn.list_ad_groups()
    assert (ag.external_id, ag.campaign_external_id, ag.daily_budget) == ("7001", "5001", None)
    (ad,) = conn.list_ads()
    assert (ad.external_id, ad.headline, ad.body) == ("7001~8001", "H1", "D1")
    kws = {k.external_id: k for k in conn.list_keywords()}
    assert kws["7001~9001"].match_type == "PHRASE" and not kws["7001~9001"].is_negative
    assert kws["7001~9002"].is_negative


def test_daily_metrics() -> None:
    conn, t = make()
    (r,) = conn.daily_metrics(EntityType.ACCOUNT, dt.date(2026, 11, 9), dt.date(2026, 11, 9))
    assert r.spend == Decimal("4200.45") and r.clicks == 40
    assert r.conversions == Decimal("2.5") and r.conv_value == Decimal("1234.57")
    name, query, _ = t.calls[-1]
    assert name == "daily_account" and "FROM customer" in query
    assert "segments.date BETWEEN '2026-11-09' AND '2026-11-09'" in query
    (kw,) = conn.daily_metrics(EntityType.KEYWORD, dt.date(2026, 11, 9), dt.date(2026, 11, 9))
    assert kw.external_id == "7001~9001" and kw.spend == Decimal("0.99")
    (ad,) = conn.daily_metrics(EntityType.AD, dt.date(2026, 11, 9), dt.date(2026, 11, 9))
    assert ad.external_id == "7001~8001" and ad.video_views is None


def test_hourly_and_spend_today() -> None:
    conn, _ = make()
    (h,) = conn.hourly_metrics(
        EntityType.ACCOUNT, dt.datetime(2026, 11, 11, 10), dt.datetime(2026, 11, 11, 13)
    )
    assert h.ts_hour == dt.datetime(2026, 11, 11, 13) and h.spend == Decimal("12.34")
    snap = conn.spend_today()
    assert snap.account_local_date == dt.date(2026, 11, 11)
    assert snap.campaign_spend == {"5001": Decimal("1.50")}


def test_search_terms() -> None:
    (st,) = make()[0].search_terms(dt.date(2026, 11, 1), dt.date(2026, 11, 10))
    assert (st.ad_group_external_id, st.term, st.cost) == (
        "7001",
        "cheap running shoes",
        Decimal("0.45"),
    )


def test_change_events() -> None:
    conn, t = make()
    events = conn.change_history(dt.datetime(2026, 11, 10, tzinfo=dt.UTC))
    got = [(e.entity_type, e.external_id, e.field, e.old_value, e.new_value) for e in events]
    assert got == [
        (EntityType.CAMPAIGN, "5001", "status", "paused", "active"),
        (EntityType.CAMPAIGN, "5001", "daily_budget", "400.00", "500.00"),
        (EntityType.AD, "7001~8001", "status", "active", "paused"),
    ]
    assert events[0].changed_at == dt.datetime(2026, 11, 11, 4, 30, 0, 123456, tzinfo=dt.UTC)
    assert events[0].client_type == "GOOGLE_ADS_WEB_CLIENT"
    assert "LIMIT 10000" in t.calls[-1][1]


def test_read_state() -> None:
    conn, _ = make()
    st = conn.read_state(EntityType.CAMPAIGN, "5001")
    assert st.status is EntityStatus.ACTIVE and st.daily_budget == Decimal("500.00")
    assert conn.read_state(EntityType.AD, "7001~8001").status is EntityStatus.ACTIVE
    assert conn.read_state(EntityType.KEYWORD, "7001~9002").status is EntityStatus.ACTIVE


def test_operation_counter_threshold() -> None:
    conn, _ = make(limit=5)
    conn.get_account()
    conn.list_campaigns()
    conn.list_ad_groups()
    usage = conn.counter.usage(dt.date(2026, 11, 11))
    assert usage.used == 3 and not usage.over_alert_threshold
    conn.list_ads()
    assert conn.counter.usage(dt.date(2026, 11, 11)).over_alert_threshold  # 4/5 = 80%
