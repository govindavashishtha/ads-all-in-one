"""Contract tests: MetaReadConnector against fixture responses (plan §8.2, §8.4, §9.2, §22)."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from engine.connectors.base import EntityStatus
from engine.connectors.fakes import FakeMetaTransport, FixtureSet, fixture_meta_credentials
from engine.connectors.meta_read import MetaReadConnector, parse_hour_bucket
from engine.connectors.resilience import RetryPolicy
from engine.enums import AutomationScope, BudgetLevel, CampaignKind, EntityType
from engine.errors import MissingInputError

pytestmark = pytest.mark.contract
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "connectors" / "contract"
NOW = dt.datetime(2026, 11, 11, 8, 0, tzinfo=dt.UTC)  # 13:30 IST


def make(conversion: str | None = "offsite_conversion.fb_pixel_purchase", transport=None):
    fx = FixtureSet.from_dir(FIX)
    t = transport or FakeMetaTransport(fx.meta)
    conn = MetaReadConnector(
        fixture_meta_credentials("act_000000000001"),
        conversion_action=conversion,
        transport=t,
        clock=lambda: NOW,
        retry=RetryPolicy(sleep=lambda _s: None),
    )
    return conn, t


def _last_insights(t: FakeMetaTransport) -> tuple[str, dict[str, Any]]:
    path, params = next(c for c in reversed(t.calls) if c[0].endswith("/insights"))
    return str(path), dict(params)


def test_account() -> None:
    a = make()[0].get_account()
    assert (a.external_id, a.currency, a.timezone, a.status) == (
        "act_000000000001",
        "INR",
        "Asia/Kolkata",
        "1",
    )


def test_campaign_normalisation() -> None:
    c = {r.external_id: r for r in make()[0].list_campaigns()}
    assert c["1001"].budget_level is BudgetLevel.CAMPAIGN
    assert c["1001"].daily_budget == Decimal("500.00")  # minor units ÷ 100 (V-3)
    assert c["1001"].kind is CampaignKind.STANDARD
    assert c["1001"].automation_scope is AutomationScope.FULL
    assert c["1002"].budget_level is BudgetLevel.AD_SET and c["1002"].daily_budget is None
    assert c["1002"].status is EntityStatus.PAUSED
    assert c["1003"].kind is CampaignKind.ADVANTAGE_PLUS
    assert c["1003"].automation_scope is AutomationScope.CAMPAIGN_ONLY
    assert c["1003"].lifetime_budget == Decimal("90000.00")
    assert c["1004"].status is EntityStatus.REMOVED and c["1004"].daily_budget is None
    assert all(r.learning is None for r in c.values())  # derived in ingest


def test_ad_set_learning_mapping() -> None:
    g = {r.external_id: r for r in make()[0].list_ad_groups()}
    assert g["2001"].learning is True  # LEARNING
    assert g["2002"].learning is False  # FAIL = learning limited -> false (§8.2)
    assert g["2003"].learning is False  # SUCCESS
    assert g["2004"].learning is None  # unreadable -> NULL (protected)
    assert g["2002"].daily_budget == Decimal("1234.56")


def test_ads() -> None:
    ads = {r.external_id: r for r in make()[0].list_ads()}
    a = ads["3001"]
    assert (a.headline, a.body, a.cta, a.format, a.creative_id) == (
        "Headline",
        "Body",
        "SHOP_NOW",
        "SHARE",
        "4001",
    )
    assert a.first_live_at == dt.datetime(2026, 10, 1, 4, 0, tzinfo=dt.UTC)
    assert ads["3002"].status is EntityStatus.OTHER


def test_daily_metrics_single_action_type_and_bulk_request() -> None:
    conn, t = make()
    rows = conn.daily_metrics(EntityType.ACCOUNT, dt.date(2026, 11, 9), dt.date(2026, 11, 9))
    (r,) = rows
    assert r.clicks == 25  # inline_link_clicks, not the link_click action (§8.4)
    assert r.spend == Decimal("4200.45")  # decimal string, major units (V-4)
    assert r.conversions == Decimal("3")  # only the configured type, never + omni_purchase
    assert r.conv_value == Decimal("2999.50")
    assert r.video_views == 77
    path, params = _last_insights(t)
    assert path == "act_000000000001/insights"
    assert params["level"] == "account" and params["time_increment"] == 1
    assert params["use_unified_attribution_setting"] == "true"  # V-8
    assert json.loads(params["time_range"]) == {"since": "2026-11-09", "until": "2026-11-09"}


def test_metrics_need_conversion_action() -> None:
    conn, _ = make(conversion=None)
    with pytest.raises(MissingInputError) as exc:
        conn.daily_metrics(EntityType.CAMPAIGN, dt.date(2026, 11, 9), dt.date(2026, 11, 9))
    assert exc.value.input_id == "I-1"


def test_hourly_metrics_filter_and_naive_local_hour() -> None:
    conn, t = make()
    rows = conn.hourly_metrics(
        EntityType.ACCOUNT, dt.datetime(2026, 11, 11, 10), dt.datetime(2026, 11, 11, 13)
    )
    assert [(r.ts_hour, r.spend) for r in rows] == [
        (dt.datetime(2026, 11, 11, 13), Decimal("12.34"))
    ]
    assert rows[0].ts_hour.tzinfo is None
    assert _last_insights(t)[1]["breakdowns"] == "hourly_stats_aggregated_by_advertiser_time_zone"
    assert parse_hour_bucket("07:00:00 - 07:59:59") == 7
    with pytest.raises(ValueError, match="account/campaign"):
        conn.hourly_metrics(EntityType.AD, dt.datetime(2026, 11, 11), dt.datetime(2026, 11, 11))


def test_window_metrics_one_non_daily_query() -> None:
    conn, t = make()
    (w,) = conn.window_metrics(EntityType.AD_GROUP, dt.date(2026, 11, 10), 7)
    assert (w.external_id, w.reach, w.frequency, w.window_days) == ("2001", 800, 2.5, 7)
    params = _last_insights(t)[1]
    assert "time_increment" not in params
    assert json.loads(params["time_range"]) == {"since": "2026-11-04", "until": "2026-11-10"}


def test_spend_today_uses_account_local_day() -> None:
    snap = make()[0].spend_today()
    assert snap.account_local_date == dt.date(2026, 11, 11)
    assert snap.campaign_spend == {"1001": Decimal("99.99")}


def test_change_history_maps_tracked_fields() -> None:
    events = make()[0].change_history(dt.datetime(2026, 11, 10, tzinfo=dt.UTC))
    assert [(e.entity_type, e.external_id, e.field, e.old_value, e.new_value) for e in events] == [
        (EntityType.CAMPAIGN, "1001", "status", "active", "paused"),
        (EntityType.AD_GROUP, "2002", "daily_budget", "1000.00", "1234.56"),
    ]
    assert events[0].actor == "Govinda (fixture)"
    assert events[0].changed_at == dt.datetime(2026, 11, 11, 4, 30, tzinfo=dt.UTC)


def test_read_state_and_token_expiry() -> None:
    conn, _ = make()
    st = conn.read_state(EntityType.AD_GROUP, "2002")
    assert st.daily_budget == Decimal("1234.56") and st.extra["learning"] is False
    assert conn.token_expires_at() == dt.datetime.fromtimestamp(1794700000, dt.UTC)


class _RateLimited(Exception):
    def __init__(self, code: int, transient: bool = False) -> None:
        super().__init__(f"code {code}")
        self._code, self._transient = code, transient

    def api_error_code(self) -> int:
        return self._code

    def api_transient_error(self) -> bool:
        return self._transient


class _Flaky(FakeMetaTransport):
    def __init__(self, store, fail_times: int, code: int) -> None:
        super().__init__(store)
        self.fail_times, self.code = fail_times, code

    def get_node(self, path, params):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise _RateLimited(self.code)
        return super().get_node(path, params)


def test_rate_limit_errors_are_retried_others_not() -> None:
    store = FixtureSet.from_dir(FIX).meta
    conn, _ = make(transport=_Flaky(store, fail_times=3, code=17))
    assert conn.get_account().currency == "INR"
    conn2, _ = make(transport=_Flaky(store, fail_times=1, code=100))
    with pytest.raises(_RateLimited):
        conn2.get_account()
    conn3, _ = make(transport=_Flaky(store, fail_times=6, code=17))
    with pytest.raises(_RateLimited):  # max 5 retries (§9.3)
        conn3.get_account()
