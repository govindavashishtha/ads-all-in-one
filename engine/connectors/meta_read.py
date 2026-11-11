"""Meta ReadConnector (facebook-business SDK, Graph API) — plan §4.1, §8.2, §8.4, §9.2, §9.3.

Design:
- All HTTP goes through a ``MetaTransport``. ``SdkMetaTransport`` uses the official SDK
  (``FacebookAdsApi.call``) and follows cursor paging; ``engine.connectors.fakes`` provides a
  fixture-backed transport so everything is testable without credentials.
- Insights are always requested in bulk with ``level`` + ``time_range`` (one call per level,
  never per entity, §9.3).
- Clicks = ``inline_link_clicks``; conversions / conv_value = exactly one configured action
  type from ``actions`` / ``action_values`` (§8.4). Never sums several action types.
- Money is converted only via ``engine.connectors.money``.

Every Graph field / parameter / enum value used here is VERIFY-P0 (docs/VERIFIED.md V-3, V-4,
V-6, V-8, V-10, V-13..V-19). Constants are grouped at the top so a doc check changes one place.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Protocol
from zoneinfo import ZoneInfo

from engine.connectors import money
from engine.connectors.base import (
    AccountRecord,
    AdGroupRecord,
    AdRecord,
    CampaignRecord,
    ChangeEvent,
    DailyMetricRow,
    EntityState,
    EntityStatus,
    HourlyMetricRow,
    KeywordRecord,
    ReadConnector,
    SearchTermRow,
    SpendSnapshot,
    WindowMetricRow,
)
from engine.connectors.resilience import (
    API_CALLS,
    OperationCounter,
    RetryPolicy,
    TokenBucket,
    call_with_retries,
)
from engine.credentials.base import MetaCredentials, PlatformCredentials
from engine.enums import (
    AdGroupKind,
    AutomationScope,
    BudgetLevel,
    CampaignKind,
    EntityType,
    Platform,
)
from engine.errors import MissingInputError

# ----------------------------------------------------------------------- VERIFY-P0 constants

ACCOUNT_FIELDS = ("id", "name", "account_status", "currency", "timezone_name")  # V-13
CAMPAIGN_FIELDS = (
    "id",
    "name",
    "objective",
    "status",
    "effective_status",
    "daily_budget",
    "lifetime_budget",
    "bid_strategy",
    "buying_type",
    "smart_promotion_type",
)  # V-14
ADSET_FIELDS = (
    "id",
    "name",
    "campaign_id",
    "status",
    "effective_status",
    "daily_budget",
    "lifetime_budget",
    "optimization_goal",
    "learning_stage_info",
)  # V-6, V-14
AD_FIELDS = (
    "id",
    "name",
    "adset_id",
    "status",
    "effective_status",
    "created_time",
    "creative{id,title,body,call_to_action_type,object_type}",
)  # V-15
INSIGHT_METRIC_FIELDS = (
    "impressions",
    "inline_link_clicks",
    "spend",
    "actions",
    "action_values",
)  # §8.4, V-4
WINDOW_FIELDS = ("reach", "frequency")  # V-16
HOURLY_BREAKDOWN = "hourly_stats_aggregated_by_advertiser_time_zone"  # V-17
ACTIVITY_FIELDS = (
    "event_type",
    "event_time",
    "actor_name",
    "object_id",
    "object_type",
    "extra_data",
    "application_name",
)  # V-10
VIDEO_VIEW_ACTION = "video_view"  # V-18: 3-second video views as an action type
# Advantage+ campaigns (§8.2 kind=advantage_plus). V-19.
ADVANTAGE_PLUS_PROMOTION_TYPES = frozenset({"AUTOMATED_SHOPPING_ADS", "SMART_APP_PROMOTION"})
# learning_stage_info.status values (§8.2, V-6): only LEARNING means learning.
LEARNING_STATUS_TRUE = frozenset({"LEARNING"})
LEARNING_STATUS_FALSE = frozenset({"SUCCESS", "FAIL"})
# activities edge event types that touch tracked fields (§9.2, V-10).
ACTIVITY_EVENTS: dict[str, tuple[EntityType, str]] = {
    "update_campaign_run_status": (EntityType.CAMPAIGN, "status"),
    "update_campaign_budget": (EntityType.CAMPAIGN, "daily_budget"),
    "update_ad_set_run_status": (EntityType.AD_GROUP, "status"),
    "update_ad_set_budget": (EntityType.AD_GROUP, "daily_budget"),
    "update_ad_run_status": (EntityType.AD, "status"),
}
# Error codes Meta documents as throttling (V-20) — retried with backoff.
RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80000, 80003, 80004, 80014})

LEVELS: dict[EntityType, str] = {
    EntityType.ACCOUNT: "account",
    EntityType.CAMPAIGN: "campaign",
    EntityType.AD_GROUP: "adset",
    EntityType.AD: "ad",
}
ID_FIELD: dict[EntityType, str] = {
    EntityType.ACCOUNT: "account_id",
    EntityType.CAMPAIGN: "campaign_id",
    EntityType.AD_GROUP: "adset_id",
    EntityType.AD: "ad_id",
}
PAGE_LIMIT = 500


class MetaTransport(Protocol):
    """GET one Graph node or a whole (paged) edge. ``path`` has no leading slash or version."""

    def get_node(self, path: str, params: dict[str, Any]) -> dict[str, Any]: ...

    def get_edge(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]: ...


def _is_retryable_meta(exc: BaseException) -> bool:
    code = getattr(exc, "api_error_code", None)
    transient = getattr(exc, "api_transient_error", None)
    try:
        code_v = code() if callable(code) else code
        transient_v = transient() if callable(transient) else transient
    except Exception:
        return False
    return bool(transient_v) or code_v in RATE_LIMIT_CODES


class SdkMetaTransport:
    """Real transport over ``facebook_business.api.FacebookAdsApi``. Never used in tests."""

    def __init__(self, credentials: MetaCredentials) -> None:
        from facebook_business.api import FacebookAdsApi

        self._api = FacebookAdsApi.init(
            app_id=credentials.app_id,
            app_secret=credentials.app_secret.get_secret_value(),
            access_token=credentials.token.get_secret_value(),
            api_version=credentials.api_version,
            crash_log=False,
        )

    def get_node(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = self._api.call("GET", (path,), params=params)
        body: dict[str, Any] = resp.json()
        return body

    def get_edge(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page_params = dict(params)
        while True:
            body = self.get_node(path, page_params)
            out.extend(body.get("data", []))
            after = body.get("paging", {}).get("cursors", {}).get("after")
            if not after or not body.get("paging", {}).get("next"):
                return out
            page_params = {**params, "after": after}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class MetaReadConnector(ReadConnector):
    platform: ClassVar[Platform] = Platform.META

    def __init__(
        self,
        credentials: PlatformCredentials,
        *,
        conversion_action: str | None,
        transport: MetaTransport | None = None,
        clock: Callable[[], dt.datetime] = _utcnow,
        retry: RetryPolicy | None = None,
        bucket: TokenBucket | None = None,
        counter: OperationCounter | None = None,
    ) -> None:
        super().__init__(credentials)
        if not isinstance(credentials, MetaCredentials):
            raise TypeError("MetaReadConnector needs MetaCredentials")
        self.creds: MetaCredentials = credentials
        self.conversion_action = conversion_action or None
        self._transport = transport if transport is not None else SdkMetaTransport(credentials)
        self._clock = clock
        self._retry = retry or RetryPolicy()
        self._bucket = bucket or TokenBucket(rate=2.0, capacity=10.0)
        self.counter = counter or OperationCounter(Platform.META, daily_limit=None)
        self._account: AccountRecord | None = None

    # ------------------------------------------------------------------ plumbing

    @property
    def act(self) -> str:
        return self.creds.ad_account_id

    def _local_today(self) -> dt.date:
        return self._clock().astimezone(ZoneInfo(self.get_account().timezone)).date()

    def _count(self) -> None:
        tz = self._account.timezone if self._account else "UTC"
        self.counter.add(self._clock().astimezone(ZoneInfo(tz)).date())

    def _node(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._bucket.acquire()
        API_CALLS.labels(platform="meta", kind="read").inc()
        result = call_with_retries(
            lambda: self._transport.get_node(path, params),
            is_retryable=_is_retryable_meta,
            policy=self._retry,
            platform=Platform.META,
        )
        self._count()
        return result

    def _edge(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self._bucket.acquire()
        API_CALLS.labels(platform="meta", kind="read").inc()
        result = call_with_retries(
            lambda: self._transport.get_edge(path, params),
            is_retryable=_is_retryable_meta,
            policy=self._retry,
            platform=Platform.META,
        )
        self._count()
        return result

    def _require_conversion_action(self) -> str:
        if not self.conversion_action:
            raise MissingInputError(
                "I-1", "rules targets.meta_conversion_action is empty (exact Meta action_type)"
            )
        return self.conversion_action

    # ------------------------------------------------------------------ entities

    def get_account(self) -> AccountRecord:
        if self._account is None:
            raw = self._node(self.act, {"fields": ",".join(ACCOUNT_FIELDS)})
            self._account = AccountRecord(
                platform=Platform.META,
                external_id=str(raw.get("id") or self.act),
                name=raw.get("name"),
                status=None if raw.get("account_status") is None else str(raw["account_status"]),
                currency=str(raw["currency"]).upper(),
                timezone=str(raw["timezone_name"]),
            )
        return self._account

    def _budget(self, raw: dict[str, Any], key: str) -> Decimal | None:
        value = raw.get(key)
        if value in (None, "", "0", 0):
            return None
        return money.meta_minor_to_major(value, self.get_account().currency)

    def list_campaigns(self) -> list[CampaignRecord]:
        account = self.get_account()
        rows = self._edge(
            f"{self.act}/campaigns", {"fields": ",".join(CAMPAIGN_FIELDS), "limit": PAGE_LIMIT}
        )
        out: list[CampaignRecord] = []
        for r in rows:
            kind = (
                CampaignKind.ADVANTAGE_PLUS
                if r.get("smart_promotion_type") in ADVANTAGE_PLUS_PROMOTION_TYPES
                else CampaignKind.STANDARD
            )
            daily = self._budget(r, "daily_budget")
            lifetime = self._budget(r, "lifetime_budget")
            out.append(
                CampaignRecord(
                    platform=Platform.META,
                    external_id=str(r["id"]),
                    account_external_id=account.external_id,
                    name=r.get("name"),
                    objective=r.get("objective"),
                    kind=kind,
                    automation_scope=(
                        AutomationScope.FULL
                        if kind is CampaignKind.STANDARD
                        else AutomationScope.CAMPAIGN_ONLY
                    ),
                    status=map_status(r.get("status")),
                    budget_level=(
                        BudgetLevel.CAMPAIGN
                        if daily is not None or lifetime is not None
                        else BudgetLevel.AD_SET
                    ),
                    daily_budget=daily,
                    lifetime_budget=lifetime,
                    shared_budget_external_id=None,
                    bid_strategy=r.get("bid_strategy"),
                    learning=None,  # Meta learning is per ad set; ingest derives the campaign value
                    raw=r,
                )
            )
        return out

    def list_ad_groups(self) -> list[AdGroupRecord]:
        rows = self._edge(
            f"{self.act}/adsets", {"fields": ",".join(ADSET_FIELDS), "limit": PAGE_LIMIT}
        )
        return [
            AdGroupRecord(
                platform=Platform.META,
                external_id=str(r["id"]),
                campaign_external_id=str(r["campaign_id"]),
                kind=AdGroupKind.AD_SET,
                name=r.get("name"),
                status=map_status(r.get("status")),
                daily_budget=self._budget(r, "daily_budget"),
                optimization_goal=r.get("optimization_goal"),
                learning=map_learning(r.get("learning_stage_info")),
                raw=r,
            )
            for r in rows
        ]

    def list_ads(self) -> list[AdRecord]:
        rows = self._edge(f"{self.act}/ads", {"fields": ",".join(AD_FIELDS), "limit": PAGE_LIMIT})
        out: list[AdRecord] = []
        for r in rows:
            creative = r.get("creative") or {}
            out.append(
                AdRecord(
                    platform=Platform.META,
                    external_id=str(r["id"]),
                    ad_group_external_id=str(r["adset_id"]),
                    name=r.get("name"),
                    status=map_status(r.get("status")),
                    format=creative.get("object_type"),
                    creative_id=None if creative.get("id") is None else str(creative["id"]),
                    headline=creative.get("title"),
                    body=creative.get("body"),
                    cta=creative.get("call_to_action_type"),
                    first_live_at=parse_meta_time(r.get("created_time")),
                    raw=r,
                )
            )
        return out

    def list_keywords(self) -> list[KeywordRecord]:
        return []

    # ------------------------------------------------------------------ insights

    def _insights(
        self,
        level: EntityType,
        since: dt.date,
        until: dt.date,
        fields: Iterable[str],
        *,
        daily: bool,
        breakdowns: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "level": LEVELS[level],
            "fields": ",".join([ID_FIELD[level], *fields]),
            "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
            "use_unified_attribution_setting": "true",  # §8.4, V-8
            "limit": PAGE_LIMIT,
        }
        if daily:
            params["time_increment"] = 1
        if breakdowns:
            params["breakdowns"] = breakdowns
        return self._edge(f"{self.act}/insights", params)

    def _external_id(self, level: EntityType, row: dict[str, Any]) -> str:
        if level is EntityType.ACCOUNT:
            return self.get_account().external_id
        return str(row[ID_FIELD[level]])

    def _conv(self, row: dict[str, Any]) -> tuple[Decimal, Decimal]:
        action = self._require_conversion_action()
        return (
            _action_count(row.get("actions"), action),
            _action_value(row.get("action_values"), action),
        )

    def daily_metrics(
        self, level: EntityType, start: dt.date, end: dt.date
    ) -> list[DailyMetricRow]:
        if level not in LEVELS:
            return []
        self._require_conversion_action()
        rows = self._insights(level, start, end, INSIGHT_METRIC_FIELDS, daily=True)
        out: list[DailyMetricRow] = []
        for r in rows:
            conv, value = self._conv(r)
            views = _action_count(r.get("actions"), VIDEO_VIEW_ACTION)
            out.append(
                DailyMetricRow(
                    platform=Platform.META,
                    entity_type=level,
                    external_id=self._external_id(level, r),
                    date=dt.date.fromisoformat(r["date_start"]),
                    impressions=_int(r.get("impressions")),
                    clicks=_int(r.get("inline_link_clicks")),
                    spend=money.meta_spend_to_major(str(r.get("spend", "0"))),
                    conversions=conv,
                    conv_value=value,
                    video_views=int(views) if views else None,
                )
            )
        return out

    def hourly_metrics(
        self, level: EntityType, start_hour: dt.datetime, end_hour: dt.datetime
    ) -> list[HourlyMetricRow]:
        if level not in (EntityType.ACCOUNT, EntityType.CAMPAIGN):
            raise ValueError("hourly metrics are account/campaign level only (§8.3)")
        self._require_conversion_action()
        rows = self._insights(
            level,
            start_hour.date(),
            end_hour.date(),
            INSIGHT_METRIC_FIELDS,
            daily=True,
            breakdowns=HOURLY_BREAKDOWN,
        )
        out: list[HourlyMetricRow] = []
        for r in rows:
            ts = dt.datetime.combine(
                dt.date.fromisoformat(r["date_start"]),
                dt.time(parse_hour_bucket(str(r[HOURLY_BREAKDOWN]))),
            )
            if not start_hour <= ts <= end_hour:
                continue
            conv, value = self._conv(r)
            out.append(
                HourlyMetricRow(
                    platform=Platform.META,
                    entity_type=level,
                    external_id=self._external_id(level, r),
                    ts_hour=ts,
                    impressions=_int(r.get("impressions")),
                    clicks=_int(r.get("inline_link_clicks")),
                    spend=money.meta_spend_to_major(str(r.get("spend", "0"))),
                    conversions=conv,
                    conv_value=value,
                )
            )
        return out

    def window_metrics(
        self, level: EntityType, as_of: dt.date, window_days: int
    ) -> list[WindowMetricRow]:
        """One windowed (non-daily) query: reach/frequency are not additive (§8.3)."""
        if level not in (EntityType.CAMPAIGN, EntityType.AD_GROUP, EntityType.AD):
            return []
        since = as_of - dt.timedelta(days=window_days - 1)
        rows = self._insights(level, since, as_of, WINDOW_FIELDS, daily=False)
        return [
            WindowMetricRow(
                platform=Platform.META,
                entity_type=level,
                external_id=self._external_id(level, r),
                window_days=window_days,
                as_of=as_of,
                reach=None if r.get("reach") is None else _int(r["reach"]),
                frequency=None if r.get("frequency") is None else float(r["frequency"]),
            )
            for r in rows
        ]

    def spend_today(self) -> SpendSnapshot:
        today = self._local_today()
        acct = self._insights(EntityType.ACCOUNT, today, today, ("spend",), daily=False)
        camps = self._insights(EntityType.CAMPAIGN, today, today, ("spend",), daily=False)
        return SpendSnapshot(
            platform=Platform.META,
            account_local_date=today,
            account_spend=sum(
                (money.meta_spend_to_major(str(r.get("spend", "0"))) for r in acct),
                Decimal("0.00"),
            ),
            campaign_spend={
                str(r["campaign_id"]): money.meta_spend_to_major(str(r.get("spend", "0")))
                for r in camps
            },
            observed_at=self._clock().astimezone(dt.UTC),
        )

    def search_terms(self, start: dt.date, end: dt.date) -> list[SearchTermRow]:
        return []

    # ------------------------------------------------------------------ changes / state

    def change_history(self, since: dt.datetime) -> list[ChangeEvent]:
        rows = self._edge(
            f"{self.act}/activities",
            {
                "fields": ",".join(ACTIVITY_FIELDS),
                "since": int(since.timestamp()),
                "limit": PAGE_LIMIT,
            },
        )
        out: list[ChangeEvent] = []
        for r in rows:
            mapped = ACTIVITY_EVENTS.get(str(r.get("event_type")))
            if mapped is None or r.get("object_id") is None:
                continue
            entity_type, field_name = mapped
            extra = _json_obj(r.get("extra_data"))
            changed_at = parse_meta_time(r.get("event_time"))
            if changed_at is None:
                continue
            out.append(
                ChangeEvent(
                    platform=Platform.META,
                    entity_type=entity_type,
                    external_id=str(r["object_id"]),
                    field=field_name,
                    old_value=self._change_value(field_name, extra.get("old_value")),
                    new_value=self._change_value(field_name, extra.get("new_value")),
                    changed_at=changed_at,
                    actor=r.get("actor_name"),
                    client_type=r.get("application_name"),
                )
            )
        return out

    def token_expires_at(self) -> dt.datetime | None:
        """``debug_token`` ``data.expires_at`` (unix s; 0 = never expires). VERIFY-P0 V-25."""
        raw = self._node(
            "debug_token",
            {
                "input_token": self.creds.token.get_secret_value(),
                "access_token": f"{self.creds.app_id}|{self.creds.app_secret.get_secret_value()}",
            },
        )
        expires = (raw.get("data") or {}).get("expires_at")
        if not expires:
            return None
        return dt.datetime.fromtimestamp(int(expires), dt.UTC)

    def _change_value(self, field_name: str, value: Any) -> str | None:
        """Budgets in activities are assumed minor units like other budget fields (V-10)."""
        if value is None:
            return None
        if field_name == "daily_budget":
            try:
                return str(money.meta_minor_to_major(str(value), self.get_account().currency))
            except money.MoneyConversionError:
                return str(value)
        if field_name == "status":
            return map_status(value).value
        return str(value)

    def read_state(self, entity_type: EntityType, external_id: str) -> EntityState:
        fields = ["status"]
        if entity_type in (EntityType.CAMPAIGN, EntityType.AD_GROUP):
            fields.append("daily_budget")
        if entity_type is EntityType.AD_GROUP:
            fields.append("learning_stage_info")
        raw = self._node(external_id, {"fields": ",".join(fields)})
        extra: dict[str, Any] = {}
        if entity_type is EntityType.AD_GROUP:
            extra["learning"] = map_learning(raw.get("learning_stage_info"))
        return EntityState(
            platform=Platform.META,
            entity_type=entity_type,
            external_id=external_id,
            status=map_status(raw.get("status")),
            daily_budget=self._budget(raw, "daily_budget"),
            extra=extra,
        )


# ----------------------------------------------------------------------- pure helpers


def map_status(value: Any) -> EntityStatus:
    """Configured ``status`` (what a human sets), not ``effective_status`` (kept in raw)."""
    v = str(value or "").upper()
    if v == "ACTIVE":
        return EntityStatus.ACTIVE
    if v == "PAUSED":
        return EntityStatus.PAUSED
    if v in ("DELETED", "ARCHIVED"):
        return EntityStatus.REMOVED
    return EntityStatus.OTHER


def map_learning(info: Any) -> bool | None:
    if not isinstance(info, dict):
        return None
    status = str(info.get("status") or "").upper()
    if status in LEARNING_STATUS_TRUE:
        return True
    if status in LEARNING_STATUS_FALSE:
        return False
    return None


def parse_meta_time(value: Any) -> dt.datetime | None:
    """Graph timestamps look like ``2026-08-01T10:00:00+0530`` → aware UTC datetime."""
    if not value:
        return None
    s = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return dt.datetime.strptime(s, fmt).astimezone(dt.UTC)
        except ValueError:
            continue
    return dt.datetime.fromisoformat(s).astimezone(dt.UTC)


def parse_hour_bucket(value: str) -> int:
    """``"13:00:00 - 13:59:59"`` → 13."""
    return int(value.split(":", 1)[0])


def _int(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(Decimal(str(value)))


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)


def _action_count(actions: Any, action_type: str) -> Decimal:
    for a in actions or []:
        if a.get("action_type") == action_type:
            return _dec(a.get("value", "0"))
    return Decimal(0)


def _action_value(values: Any, action_type: str) -> Decimal:
    for a in values or []:
        if a.get("action_type") == action_type:
            return money.meta_spend_to_major(str(a.get("value", "0")))
    return Decimal("0.00")


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}
