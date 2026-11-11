# ruff: noqa: S608  (GAQL strings built from typed ints/dates; not SQL)
"""Google Ads ReadConnector (google-ads SDK, GAQL) — plan §4.1, §4.3, §8.2, §8.4, §9.2, §9.3.

Design:
- All requests go through a ``GoogleTransport``: ``search(name, query, params)`` returns rows as
  plain dicts (protobuf JSON with snake_case field names; int64 values arrive as strings, enums
  as names). ``SdkGoogleTransport`` uses the official client's ``GoogleAdsService.SearchStream``;
  ``engine.connectors.fakes`` replays fixtures keyed by ``name`` (``params`` carries the date
  range so fakes can filter).
- Each search request counts as one API operation (§4.3; VERIFY-P0 V-1). The counter alerts at
  80% of ``daily_operation_limit`` (default 2,880 = Explorer access, the plan's fallback level).
- Ids: Google ad and keyword ids are only unique inside their ad group, so the normalised
  ``external_id`` is ``"{ad_group_id}~{ad_id}"`` / ``"{ad_group_id}~{criterion_id}"`` (the same
  suffix the API uses in ``adGroupAds`` / ``adGroupCriteria`` resource names).
- Money: micros via ``engine.connectors.money`` only.

GAQL field names below were checked against the installed SDK's v26 protos (field exists) but
their semantics are VERIFY-P0 (docs/VERIFIED.md V-5, V-7, V-9, V-21..V-24).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
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
from engine.credentials.base import GoogleCredentials, PlatformCredentials
from engine.enums import (
    AdGroupKind,
    AutomationScope,
    BudgetLevel,
    CampaignKind,
    EntityType,
    Platform,
)

GOOGLE_ADS_API_VERSION = "v26"  # newest in google-ads==35.0.0; VERIFY-P0 (docs/VERSIONS.md)
EXPLORER_DAILY_OPERATIONS = 2_880  # §4.3, VERIFY-P0 V-1

# bidding_strategy_system_status values that mean "learning" (§8.2, V-7).
LEARNING_VALUES = frozenset(
    {
        "LEARNING_NEW",
        "LEARNING_SETTING_CHANGE",
        "LEARNING_BUDGET_CHANGE",
        "LEARNING_COMPOSITION_CHANGE",
        "LEARNING_CONVERSION_TYPE_CHANGE",
        "LEARNING_CONVERSION_SETTING_CHANGE",
        "MULTIPLE_LEARNING",
    }
)
UNKNOWN_VALUES = frozenset({"", "UNSPECIFIED", "UNKNOWN", "UNAVAILABLE"})
# advertising_channel_type → campaigns.kind (§8.2). V-21.
STANDARD_CHANNELS = frozenset({"SEARCH", "DISPLAY", "SHOPPING", "VIDEO"})
PMAX_CHANNELS = frozenset({"PERFORMANCE_MAX"})

METRIC_FIELDS = (
    "metrics.impressions",
    "metrics.clicks",
    "metrics.cost_micros",
    "metrics.conversions",
    "metrics.conversions_value",
)
VIDEO_FIELD = "metrics.video_trueview_views"  # V-22 (renamed from video_views in recent API)

# level → (FROM resource, id select fields)
LEVEL_QUERY: dict[EntityType, tuple[str, tuple[str, ...]]] = {
    EntityType.ACCOUNT: ("customer", ("customer.id",)),
    EntityType.CAMPAIGN: ("campaign", ("campaign.id",)),
    EntityType.AD_GROUP: ("ad_group", ("ad_group.id",)),
    EntityType.AD: ("ad_group_ad", ("ad_group.id", "ad_group_ad.ad.id")),
    EntityType.KEYWORD: ("keyword_view", ("ad_group.id", "ad_group_criterion.criterion_id")),
}

Q_CUSTOMER = (
    "SELECT customer.id, customer.descriptive_name, customer.currency_code, "
    "customer.time_zone, customer.status FROM customer"
)
Q_CAMPAIGNS = (
    "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
    "campaign.advertising_channel_sub_type, campaign.bidding_strategy_type, "
    "campaign.bidding_strategy_system_status, campaign.campaign_budget, campaign_budget.id, "
    "campaign_budget.amount_micros, campaign_budget.total_amount_micros, "
    "campaign_budget.explicitly_shared, campaign_budget.period FROM campaign"
)
Q_AD_GROUPS = (
    "SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.type, campaign.id FROM ad_group"
)
Q_ADS = (
    "SELECT ad_group.id, ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, "
    "ad_group_ad.ad.type, ad_group_ad.ad.responsive_search_ad.headlines, "
    "ad_group_ad.ad.responsive_search_ad.descriptions FROM ad_group_ad"
)
Q_KEYWORDS = (
    "SELECT ad_group.id, ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
    "ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
    "ad_group_criterion.negative FROM ad_group_criterion "
    "WHERE ad_group_criterion.type = 'KEYWORD'"
)
Q_SEARCH_TERMS = (
    "SELECT ad_group.id, search_term_view.search_term, segments.date, "
    + ", ".join(METRIC_FIELDS)
    + " FROM search_term_view WHERE segments.date BETWEEN '{start}' AND '{end}'"
)
Q_CHANGE_EVENTS = (
    "SELECT change_event.change_date_time, change_event.change_resource_type, "
    "change_event.changed_fields, change_event.client_type, change_event.user_email, "
    "change_event.old_resource, change_event.new_resource, change_event.campaign, "
    "change_event.ad_group, change_event.change_resource_name, "
    "change_event.resource_change_operation FROM change_event "
    "WHERE change_event.change_date_time >= '{since}' "
    "AND change_event.change_date_time <= '{until}' "
    "ORDER BY change_event.change_date_time LIMIT 10000"
)  # V-9: LIMIT is mandatory; lookback limited (≤ 30 days)


class GoogleTransport(Protocol):
    def search(self, name: str, query: str, params: dict[str, Any]) -> list[dict[str, Any]]: ...


def _is_retryable_google(exc: BaseException) -> bool:
    """RESOURCE_EXHAUSTED / INTERNAL / UNAVAILABLE / DEADLINE_EXCEEDED (V-23)."""
    code = getattr(exc, "error", None)
    code_fn = getattr(code, "code", None)
    if callable(code_fn):
        try:
            name = getattr(code_fn(), "name", "")
        except Exception:
            name = ""
        if name in {"RESOURCE_EXHAUSTED", "INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED"}:
            return True
    failure = getattr(exc, "failure", None)
    for err in getattr(failure, "errors", []) or []:
        quota = getattr(getattr(err, "error_code", None), "quota_error", 0)
        internal = getattr(getattr(err, "error_code", None), "internal_error", 0)
        if quota or internal:
            return True
    return False


class SdkGoogleTransport:
    """Real transport over ``google.ads.googleads.client.GoogleAdsClient``. Never used in tests."""

    def __init__(self, credentials: GoogleCredentials) -> None:
        from google.ads.googleads.client import GoogleAdsClient

        config: dict[str, Any] = {
            "client_id": credentials.oauth_client_id,
            "client_secret": credentials.oauth_client_secret.get_secret_value(),
            "refresh_token": credentials.refresh_token.get_secret_value(),
            "use_proto_plus": False,
            # No developer_token: optional in google-ads 35.0.0 (plan §4.3, VERIFY-P0 V-1).
        }
        if credentials.login_customer_id:
            config["login_customer_id"] = credentials.login_customer_id.replace("-", "")
        self._client = GoogleAdsClient.load_from_dict(config, version=GOOGLE_ADS_API_VERSION)
        self._service = self._client.get_service("GoogleAdsService")
        self._customer_id = credentials.customer_id.replace("-", "")

    def search(self, name: str, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]

        out: list[dict[str, Any]] = []
        stream = self._service.search_stream(customer_id=self._customer_id, query=query)
        for batch in stream:
            for row in batch.results:
                out.append(
                    MessageToDict(
                        row,
                        preserving_proto_field_name=True,
                        always_print_fields_with_no_presence=True,
                    )
                )
        return out


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class GoogleReadConnector(ReadConnector):
    platform: ClassVar[Platform] = Platform.GOOGLE

    def __init__(
        self,
        credentials: PlatformCredentials,
        *,
        transport: GoogleTransport | None = None,
        clock: Callable[[], dt.datetime] = _utcnow,
        retry: RetryPolicy | None = None,
        bucket: TokenBucket | None = None,
        counter: OperationCounter | None = None,
        daily_operation_limit: int = EXPLORER_DAILY_OPERATIONS,
    ) -> None:
        super().__init__(credentials)
        if not isinstance(credentials, GoogleCredentials):
            raise TypeError("GoogleReadConnector needs GoogleCredentials")
        self.creds: GoogleCredentials = credentials
        self._transport = transport if transport is not None else SdkGoogleTransport(credentials)
        self._clock = clock
        self._retry = retry or RetryPolicy()
        self._bucket = bucket or TokenBucket(rate=2.0, capacity=10.0)
        self.counter = counter or OperationCounter(Platform.GOOGLE, daily_operation_limit)
        self._account: AccountRecord | None = None

    # ------------------------------------------------------------------ plumbing

    def _search(
        self, name: str, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self._bucket.acquire()
        API_CALLS.labels(platform="google", kind="read").inc()
        rows = call_with_retries(
            lambda: self._transport.search(name, query, params or {}),
            is_retryable=_is_retryable_google,
            policy=self._retry,
            platform=Platform.GOOGLE,
        )
        tz = self._account.timezone if self._account else "UTC"
        self.counter.add(self._clock().astimezone(ZoneInfo(tz)).date())
        return rows

    def _local_now(self) -> dt.datetime:
        return self._clock().astimezone(ZoneInfo(self.get_account().timezone))

    # ------------------------------------------------------------------ entities

    def get_account(self) -> AccountRecord:
        if self._account is None:
            rows = self._search("customer", Q_CUSTOMER)
            if not rows:
                raise RuntimeError("Google Ads: customer query returned no row")
            c = rows[0]["customer"]
            self._account = AccountRecord(
                platform=Platform.GOOGLE,
                external_id=str(c.get("id") or self.creds.customer_id.replace("-", "")),
                name=c.get("descriptive_name"),
                status=c.get("status"),
                currency=str(c["currency_code"]).upper(),
                timezone=str(c["time_zone"]),
            )
        return self._account

    def list_campaigns(self) -> list[CampaignRecord]:
        account = self.get_account()
        out: list[CampaignRecord] = []
        for row in self._search("campaigns", Q_CAMPAIGNS):
            c = row["campaign"]
            b = row.get("campaign_budget") or {}
            channel = str(c.get("advertising_channel_type") or "")
            kind = (
                CampaignKind.PMAX
                if channel in PMAX_CHANNELS
                else CampaignKind.STANDARD
                if channel in STANDARD_CHANNELS
                else CampaignKind.OTHER
            )
            shared = bool(b.get("explicitly_shared"))
            period = str(b.get("period") or "")
            amount = _micros(b.get("amount_micros"))
            total = _micros(b.get("total_amount_micros"))
            out.append(
                CampaignRecord(
                    platform=Platform.GOOGLE,
                    external_id=str(c["id"]),
                    account_external_id=account.external_id,
                    name=c.get("name"),
                    objective=channel or None,
                    kind=kind,
                    automation_scope=(
                        AutomationScope.FULL
                        if kind is CampaignKind.STANDARD
                        else AutomationScope.CAMPAIGN_ONLY
                    ),
                    status=map_status(c.get("status")),
                    budget_level=BudgetLevel.SHARED if shared else BudgetLevel.CAMPAIGN,
                    daily_budget=amount if period in ("DAILY", "") and amount else None,
                    lifetime_budget=total or None,
                    shared_budget_external_id=(
                        None if not shared or b.get("id") is None else str(b["id"])
                    ),
                    bid_strategy=c.get("bidding_strategy_type"),
                    learning=map_learning(c.get("bidding_strategy_system_status")),
                    raw=row,
                )
            )
        return out

    def list_ad_groups(self) -> list[AdGroupRecord]:
        out: list[AdGroupRecord] = []
        for row in self._search("ad_groups", Q_AD_GROUPS):
            g = row["ad_group"]
            out.append(
                AdGroupRecord(
                    platform=Platform.GOOGLE,
                    external_id=str(g["id"]),
                    campaign_external_id=str(row["campaign"]["id"]),
                    kind=AdGroupKind.AD_GROUP,
                    name=g.get("name"),
                    status=map_status(g.get("status")),
                    daily_budget=None,  # Google budgets live on the campaign (§8.2)
                    optimization_goal=g.get("type"),
                    learning=None,  # learning is a campaign-level signal on Google (§8.2)
                    raw=row,
                )
            )
        return out

    def list_ads(self) -> list[AdRecord]:
        out: list[AdRecord] = []
        for row in self._search("ads", Q_ADS):
            ag = str(row["ad_group"]["id"])
            aga = row["ad_group_ad"]
            ad = aga.get("ad") or {}
            rsa = ad.get("responsive_search_ad") or {}
            out.append(
                AdRecord(
                    platform=Platform.GOOGLE,
                    external_id=f"{ag}~{ad['id']}",
                    ad_group_external_id=ag,
                    name=ad.get("name") or None,
                    status=map_status(aga.get("status")),
                    format=ad.get("type"),
                    creative_id=str(ad["id"]),
                    headline=_first_text(rsa.get("headlines")),
                    body=_first_text(rsa.get("descriptions")),
                    cta=None,
                    first_live_at=None,
                    raw=row,
                )
            )
        return out

    def list_keywords(self) -> list[KeywordRecord]:
        out: list[KeywordRecord] = []
        for row in self._search("keywords", Q_KEYWORDS):
            ag = str(row["ad_group"]["id"])
            crit = row["ad_group_criterion"]
            kw = crit.get("keyword") or {}
            out.append(
                KeywordRecord(
                    external_id=f"{ag}~{crit['criterion_id']}",
                    ad_group_external_id=ag,
                    text=str(kw.get("text", "")),
                    match_type=kw.get("match_type"),
                    status=map_status(crit.get("status")),
                    is_negative=bool(crit.get("negative")),
                )
            )
        return out

    # ------------------------------------------------------------------ metrics

    def _metric_query(
        self, level: EntityType, start: dt.date, end: dt.date, *, hourly: bool
    ) -> tuple[str, str]:
        resource, ids = LEVEL_QUERY[level]
        seg = ["segments.date", "segments.hour"] if hourly else ["segments.date"]
        fields = [*ids, *seg, *METRIC_FIELDS]
        if not hourly and level is not EntityType.ACCOUNT:
            fields.append(VIDEO_FIELD)
        name = f"{'hourly' if hourly else 'daily'}_{level.value}"
        query = (
            f"SELECT {', '.join(fields)} FROM {resource} "
            f"WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
        )
        return name, query

    def _external_id(self, level: EntityType, row: dict[str, Any]) -> str:
        if level is EntityType.ACCOUNT:
            return self.get_account().external_id
        if level is EntityType.CAMPAIGN:
            return str(row["campaign"]["id"])
        if level is EntityType.AD_GROUP:
            return str(row["ad_group"]["id"])
        if level is EntityType.AD:
            return f"{row['ad_group']['id']}~{row['ad_group_ad']['ad']['id']}"
        return f"{row['ad_group']['id']}~{row['ad_group_criterion']['criterion_id']}"

    def daily_metrics(
        self, level: EntityType, start: dt.date, end: dt.date
    ) -> list[DailyMetricRow]:
        if level not in LEVEL_QUERY:
            return []
        self.get_account()
        name, query = self._metric_query(level, start, end, hourly=False)
        out: list[DailyMetricRow] = []
        for row in self._search(name, query, {"start": start, "end": end}):
            m = row.get("metrics") or {}
            views = m.get("video_trueview_views")
            out.append(
                DailyMetricRow(
                    platform=Platform.GOOGLE,
                    entity_type=level,
                    external_id=self._external_id(level, row),
                    date=dt.date.fromisoformat(row["segments"]["date"]),
                    impressions=_int(m.get("impressions")),
                    clicks=_int(m.get("clicks")),
                    spend=_micros(m.get("cost_micros")) or Decimal("0.00"),
                    conversions=_dec(m.get("conversions")),
                    conv_value=money.quantize_major(_dec(m.get("conversions_value"))),
                    video_views=None if views in (None, "", "0", 0) else _int(views),
                )
            )
        return out

    def hourly_metrics(
        self, level: EntityType, start_hour: dt.datetime, end_hour: dt.datetime
    ) -> list[HourlyMetricRow]:
        if level not in (EntityType.ACCOUNT, EntityType.CAMPAIGN):
            raise ValueError("hourly metrics are account/campaign level only (§8.3)")
        self.get_account()
        name, query = self._metric_query(level, start_hour.date(), end_hour.date(), hourly=True)
        out: list[HourlyMetricRow] = []
        params = {"start": start_hour.date(), "end": end_hour.date()}
        for row in self._search(name, query, params):
            seg = row["segments"]
            ts = dt.datetime.combine(dt.date.fromisoformat(seg["date"]), dt.time(int(seg["hour"])))
            if not start_hour <= ts <= end_hour:
                continue
            m = row.get("metrics") or {}
            out.append(
                HourlyMetricRow(
                    platform=Platform.GOOGLE,
                    entity_type=level,
                    external_id=self._external_id(level, row),
                    ts_hour=ts,
                    impressions=_int(m.get("impressions")),
                    clicks=_int(m.get("clicks")),
                    spend=_micros(m.get("cost_micros")) or Decimal("0.00"),
                    conversions=_dec(m.get("conversions")),
                    conv_value=money.quantize_major(_dec(m.get("conversions_value"))),
                )
            )
        return out

    def window_metrics(
        self, level: EntityType, as_of: dt.date, window_days: int
    ) -> list[WindowMetricRow]:
        return []  # reach/frequency are Meta-only in v1 (§8.3)

    def spend_today(self) -> SpendSnapshot:
        today = self._local_now().date()
        d = today.isoformat()
        acct = self._search(
            "spend_today_account",
            f"SELECT metrics.cost_micros FROM customer WHERE segments.date = '{d}'",
            {"start": today, "end": today},
        )
        camps = self._search(
            "spend_today_campaign",
            f"SELECT campaign.id, metrics.cost_micros FROM campaign WHERE segments.date = '{d}'",
            {"start": today, "end": today},
        )
        return SpendSnapshot(
            platform=Platform.GOOGLE,
            account_local_date=today,
            account_spend=sum(
                (_micros((r.get("metrics") or {}).get("cost_micros")) or Decimal(0) for r in acct),
                Decimal("0.00"),
            ),
            campaign_spend={
                str(r["campaign"]["id"]): _micros((r.get("metrics") or {}).get("cost_micros"))
                or Decimal("0.00")
                for r in camps
            },
            observed_at=self._clock().astimezone(dt.UTC),
        )

    def search_terms(self, start: dt.date, end: dt.date) -> list[SearchTermRow]:
        self.get_account()
        rows = self._search(
            "search_terms",
            Q_SEARCH_TERMS.format(start=start.isoformat(), end=end.isoformat()),
            {"start": start, "end": end},
        )
        out: list[SearchTermRow] = []
        for row in rows:
            m = row.get("metrics") or {}
            out.append(
                SearchTermRow(
                    ad_group_external_id=str(row["ad_group"]["id"]),
                    term=str(row["search_term_view"]["search_term"]),
                    date=dt.date.fromisoformat(row["segments"]["date"]),
                    impressions=_int(m.get("impressions")),
                    clicks=_int(m.get("clicks")),
                    cost=_micros(m.get("cost_micros")) or Decimal("0.00"),
                    conversions=_dec(m.get("conversions")),
                    conv_value=money.quantize_major(_dec(m.get("conversions_value"))),
                )
            )
        return out

    # ------------------------------------------------------------------ changes / state

    def change_history(self, since: dt.datetime) -> list[ChangeEvent]:
        tz = ZoneInfo(self.get_account().timezone)
        fmt = "%Y-%m-%d %H:%M:%S"
        until = self._clock().astimezone(tz)
        rows = self._search(
            "change_events",
            Q_CHANGE_EVENTS.format(
                since=since.astimezone(tz).strftime(fmt), until=until.strftime(fmt)
            ),
            {"since": since, "until": until},
        )
        out: list[ChangeEvent] = []
        for row in rows:
            out.extend(_change_events(row, tz))
        return out

    def read_state(self, entity_type: EntityType, external_id: str) -> EntityState:
        if entity_type is EntityType.CAMPAIGN:
            rows = self._search(
                "state_campaign",
                "SELECT campaign.id, campaign.status, campaign.bidding_strategy_system_status, "
                "campaign_budget.amount_micros, campaign_budget.explicitly_shared FROM campaign "
                f"WHERE campaign.id = {int(external_id)}",
                {"id": external_id},
            )
            r = _one(rows, entity_type, external_id)
            return EntityState(
                platform=Platform.GOOGLE,
                entity_type=entity_type,
                external_id=external_id,
                status=map_status(r["campaign"].get("status")),
                daily_budget=_micros((r.get("campaign_budget") or {}).get("amount_micros")),
                extra={
                    "learning": map_learning(r["campaign"].get("bidding_strategy_system_status"))
                },
            )
        if entity_type is EntityType.AD_GROUP:
            rows = self._search(
                "state_ad_group",
                f"SELECT ad_group.id, ad_group.status FROM ad_group "
                f"WHERE ad_group.id = {int(external_id)}",
                {"id": external_id},
            )
            r = _one(rows, entity_type, external_id)
            status = r["ad_group"].get("status")
        elif entity_type is EntityType.AD:
            ag, ad = external_id.split("~", 1)
            rows = self._search(
                "state_ad",
                "SELECT ad_group.id, ad_group_ad.ad.id, ad_group_ad.status FROM ad_group_ad "
                f"WHERE ad_group.id = {int(ag)} AND ad_group_ad.ad.id = {int(ad)}",
                {"id": external_id},
            )
            status = _one(rows, entity_type, external_id)["ad_group_ad"].get("status")
        elif entity_type is EntityType.KEYWORD:
            ag, crit = external_id.split("~", 1)
            rows = self._search(
                "state_keyword",
                "SELECT ad_group.id, ad_group_criterion.criterion_id, ad_group_criterion.status "
                f"FROM ad_group_criterion WHERE ad_group.id = {int(ag)} "
                f"AND ad_group_criterion.criterion_id = {int(crit)}",
                {"id": external_id},
            )
            status = _one(rows, entity_type, external_id)["ad_group_criterion"].get("status")
        else:
            raise ValueError(f"read_state not supported for {entity_type}")
        return EntityState(
            platform=Platform.GOOGLE,
            entity_type=entity_type,
            external_id=external_id,
            status=map_status(status),
            daily_budget=None,
        )


# ----------------------------------------------------------------------- pure helpers


def map_status(value: Any) -> EntityStatus:
    v = str(value or "").upper()
    if v == "ENABLED":
        return EntityStatus.ACTIVE
    if v == "PAUSED":
        return EntityStatus.PAUSED
    if v == "REMOVED":
        return EntityStatus.REMOVED
    return EntityStatus.OTHER


def map_learning(value: Any) -> bool | None:
    v = str(value or "").upper()
    if v in UNKNOWN_VALUES:
        return None
    return v in LEARNING_VALUES


def _int(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(Decimal(str(value)))


def _dec(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal(0)
    return Decimal(str(value))


def _micros(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return money.google_micros_to_major(int(str(value)))


def _first_text(assets: Any) -> str | None:
    for a in assets or []:
        text = a.get("text") if isinstance(a, dict) else None
        if text:
            return str(text)
    return None


def _one(rows: list[dict[str, Any]], entity_type: EntityType, external_id: str) -> dict[str, Any]:
    if len(rows) != 1:
        raise LookupError(f"google {entity_type} {external_id}: expected 1 row, got {len(rows)}")
    return rows[0]


def _resource_id(resource_name: Any) -> str | None:
    """``customers/1/campaigns/123`` → ``123``."""
    if not resource_name:
        return None
    return str(resource_name).rsplit("/", 1)[-1]


# change_event resource type → (entity type, old/new_resource key, {proto field: tracked field})
_CHANGE_MAP: dict[str, tuple[EntityType, str, dict[str, str]]] = {
    "CAMPAIGN": (EntityType.CAMPAIGN, "campaign", {"status": "status"}),
    "CAMPAIGN_BUDGET": (
        EntityType.CAMPAIGN,
        "campaign_budget",
        {"amount_micros": "daily_budget", "amountMicros": "daily_budget"},
    ),
    "AD_GROUP": (EntityType.AD_GROUP, "ad_group", {"status": "status"}),
    "AD_GROUP_AD": (EntityType.AD, "ad_group_ad", {"status": "status"}),
}


def _change_events(row: dict[str, Any], tz: ZoneInfo) -> list[ChangeEvent]:
    ce = row.get("change_event") or {}
    mapped = _CHANGE_MAP.get(str(ce.get("change_resource_type")))
    if mapped is None:
        return []
    entity_type, key, fields = mapped
    changed = ce.get("changed_fields") or ""
    changed_list = changed.split(",") if isinstance(changed, str) else list(changed)
    if entity_type is EntityType.AD:
        suffix = _resource_id(ce.get("change_resource_name"))  # adGroupAds/{ag}~{ad}
        external_id = suffix
    elif key == "campaign_budget" or entity_type is EntityType.CAMPAIGN:
        external_id = _resource_id(ce.get("campaign"))
    else:
        external_id = _resource_id(ce.get("ad_group"))
    if external_id is None:
        return []
    changed_at = (
        dt.datetime.fromisoformat(str(ce["change_date_time"])).replace(tzinfo=tz).astimezone(dt.UTC)
    )
    old = (ce.get("old_resource") or {}).get(key) or {}
    new = (ce.get("new_resource") or {}).get(key) or {}
    out: list[ChangeEvent] = []
    for proto_field in changed_list:
        tracked = fields.get(proto_field.strip())
        if tracked is None:
            continue
        pf = proto_field.strip()
        snake = "amount_micros" if pf == "amountMicros" else pf

        def conv(v: Any, _tracked: str = tracked) -> str | None:
            if v in (None, ""):
                return None
            if _tracked == "daily_budget":
                return str(money.google_micros_to_major(int(str(v))))
            return map_status(v).value

        out.append(
            ChangeEvent(
                platform=Platform.GOOGLE,
                entity_type=entity_type,
                external_id=external_id,
                field=tracked,
                old_value=conv(old.get(snake)),
                new_value=conv(new.get(snake)),
                changed_at=changed_at,
                actor=ce.get("user_email"),
                client_type=ce.get("client_type"),
            )
        )
    return out
