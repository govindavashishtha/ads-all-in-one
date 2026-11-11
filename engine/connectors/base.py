"""Connector interfaces + the normalised entity model (plan §3, §8.2, §9.3, §15).

Contract for implementers (connectors agent):
- Connectors return these dataclasses with money ALREADY converted to ``Decimal`` major units
  via ``engine.connectors.money`` (§0 rule 4) and statuses mapped to ``EntityStatus``.
- Records carry platform ``external_id`` strings; ingest maps them to internal ids.
- Metric ``date`` / ``ts_hour`` are in the AD ACCOUNT's timezone (§0 rule 9); ``ts_hour`` is a
  naive datetime at the start of the local hour.
- Insights are requested in bulk (level + filtering), never one call per entity (§9.3);
  rate limiting = per-platform token bucket + exponential backoff with jitter, max 5 retries.
- ``ReadConnector`` and ``WriteConnector`` are separate classes. Only ``engine/executor`` and
  ``engine/pacing/emergency.py`` may import ``WriteConnector`` (§0 rule 3, test-enforced).
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from engine.credentials.base import PlatformCredentials
from engine.enums import (
    Access,
    AdGroupKind,
    AutomationScope,
    BudgetLevel,
    CampaignKind,
    EntityType,
    Platform,
)


class EntityStatus(StrEnum):
    """Normalised delivery status. Connectors map platform values; raw stays in ``raw``."""

    ACTIVE = "active"
    PAUSED = "paused"
    REMOVED = "removed"  # deleted / removed / archived
    OTHER = "other"  # anything else (e.g. in review, disapproved); never auto-enabled


# ----------------------------------------------------------------------------- read models


@dataclass(frozen=True, slots=True)
class AccountRecord:
    platform: Platform
    external_id: str
    name: str | None
    status: str | None
    currency: str  # ISO 4217
    timezone: str  # IANA, read from the platform


@dataclass(frozen=True, slots=True)
class CampaignRecord:
    platform: Platform
    external_id: str
    account_external_id: str
    name: str | None
    objective: str | None
    kind: CampaignKind
    automation_scope: AutomationScope  # campaign_only for pmax / advantage_plus / other (§8.2)
    status: EntityStatus
    budget_level: BudgetLevel
    daily_budget: Decimal | None
    lifetime_budget: Decimal | None
    shared_budget_external_id: str | None
    bid_strategy: str | None
    learning: bool | None  # None = unreadable -> treated as true by the guardrail
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdGroupRecord:
    platform: Platform
    external_id: str
    campaign_external_id: str
    kind: AdGroupKind
    name: str | None
    status: EntityStatus
    daily_budget: Decimal | None
    optimization_goal: str | None
    learning: bool | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdRecord:
    platform: Platform
    external_id: str
    ad_group_external_id: str
    name: str | None
    status: EntityStatus
    format: str | None
    creative_id: str | None
    headline: str | None
    body: str | None
    cta: str | None
    first_live_at: dt.datetime | None  # UTC
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KeywordRecord:
    external_id: str
    ad_group_external_id: str
    text: str
    match_type: str | None
    status: EntityStatus
    is_negative: bool
    platform: Platform = Platform.GOOGLE


@dataclass(frozen=True, slots=True)
class DailyMetricRow:
    platform: Platform
    entity_type: EntityType
    external_id: str
    date: dt.date  # account-local
    impressions: int
    clicks: int  # §8.4 definition
    spend: Decimal
    conversions: Decimal  # §8.4: exactly one configured Meta action type
    conv_value: Decimal
    video_views: int | None = None


@dataclass(frozen=True, slots=True)
class HourlyMetricRow:
    platform: Platform
    entity_type: EntityType  # account or campaign only
    external_id: str
    ts_hour: dt.datetime  # naive, account-local start of hour
    impressions: int
    clicks: int
    spend: Decimal
    conversions: Decimal
    conv_value: Decimal


@dataclass(frozen=True, slots=True)
class WindowMetricRow:
    platform: Platform
    entity_type: EntityType
    external_id: str
    window_days: int
    as_of: dt.date
    reach: int | None
    frequency: float | None


@dataclass(frozen=True, slots=True)
class SearchTermRow:
    ad_group_external_id: str
    term: str
    date: dt.date
    impressions: int
    clicks: int
    cost: Decimal
    conversions: Decimal
    conv_value: Decimal


@dataclass(frozen=True, slots=True)
class SpendSnapshot:
    """Today's spend for pacing (§12.4), account-local day."""

    platform: Platform
    account_local_date: dt.date
    account_spend: Decimal
    campaign_spend: dict[str, Decimal]  # campaign external_id -> spend today
    observed_at: dt.datetime  # UTC


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """Platform change history entry (§9.2). VERIFY-P0: Google change_event, Meta activities."""

    platform: Platform
    entity_type: EntityType
    external_id: str
    field: str
    old_value: str | None
    new_value: str | None
    changed_at: dt.datetime  # UTC
    actor: str | None
    client_type: str | None


@dataclass(frozen=True, slots=True)
class EntityState:
    """Tracked fields read live from the platform (executor before/after, reconciler)."""

    platform: Platform
    entity_type: EntityType
    external_id: str
    status: EntityStatus
    daily_budget: Decimal | None
    extra: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------- write models


@dataclass(frozen=True, slots=True)
class AdDraft:
    """A new ad, always created PAUSED (§2 non-goals, §15 step 5)."""

    platform: Platform
    ad_group_external_id: str
    headline: str | None
    body: str | None
    cta: str | None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WriteResult:
    ok: bool
    validate_only: bool
    platform_response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_external_id: str | None = None


# ----------------------------------------------------------------------------- interfaces


class _ConnectorBase(abc.ABC):
    platform: ClassVar[Platform]
    required_access: ClassVar[Access]

    def __init__(self, credentials: PlatformCredentials) -> None:
        if credentials.access is not self.required_access:
            raise ValueError(
                f"{type(self).__name__} needs {self.required_access} credentials, "
                f"got {credentials.access}"
            )
        self.credentials = credentials


class ReadConnector(_ConnectorBase):
    """Read-only access to one ad account. Never mutates platform state."""

    required_access: ClassVar[Access] = Access.READ

    @abc.abstractmethod
    def get_account(self) -> AccountRecord: ...

    @abc.abstractmethod
    def list_campaigns(self) -> list[CampaignRecord]: ...

    @abc.abstractmethod
    def list_ad_groups(self) -> list[AdGroupRecord]: ...

    @abc.abstractmethod
    def list_ads(self) -> list[AdRecord]: ...

    @abc.abstractmethod
    def list_keywords(self) -> list[KeywordRecord]:
        """Google only; Meta returns []."""

    @abc.abstractmethod
    def daily_metrics(
        self, level: EntityType, start: dt.date, end: dt.date
    ) -> list[DailyMetricRow]:
        """Inclusive account-local date range, all entities at ``level`` in bulk."""

    @abc.abstractmethod
    def hourly_metrics(
        self, level: EntityType, start_hour: dt.datetime, end_hour: dt.datetime
    ) -> list[HourlyMetricRow]:
        """``level`` in {account, campaign}; naive account-local hours, inclusive."""

    @abc.abstractmethod
    def window_metrics(
        self, level: EntityType, as_of: dt.date, window_days: int
    ) -> list[WindowMetricRow]:
        """Meta reach/frequency as one windowed query (§8.3). Google returns []."""

    @abc.abstractmethod
    def spend_today(self) -> SpendSnapshot: ...

    @abc.abstractmethod
    def search_terms(self, start: dt.date, end: dt.date) -> list[SearchTermRow]:
        """Google only; Meta returns []."""

    @abc.abstractmethod
    def change_history(self, since: dt.datetime) -> list[ChangeEvent]: ...

    @abc.abstractmethod
    def read_state(self, entity_type: EntityType, external_id: str) -> EntityState: ...


class WriteConnector(_ConnectorBase):
    """Mutations. Every method supports ``validate_only`` (§15 step 4, VERIFY-P0 per platform).

    Import restricted to engine/executor and engine/pacing/emergency.py (§0 rule 3).
    """

    required_access: ClassVar[Access] = Access.WRITE

    @abc.abstractmethod
    def set_status(
        self,
        entity_type: EntityType,
        external_id: str,
        status: EntityStatus,
        *,
        validate_only: bool,
    ) -> WriteResult: ...

    @abc.abstractmethod
    def set_daily_budget(
        self, entity_type: EntityType, external_id: str, amount: Decimal, *, validate_only: bool
    ) -> WriteResult:
        """``amount`` in major units; the connector converts via ``money``."""

    @abc.abstractmethod
    def add_negative_keyword(
        self, ad_group_external_id: str, text: str, match_type: str, *, validate_only: bool
    ) -> WriteResult: ...

    @abc.abstractmethod
    def remove_negative_keyword(
        self, ad_group_external_id: str, criterion_external_id: str, *, validate_only: bool
    ) -> WriteResult: ...

    @abc.abstractmethod
    def create_ad(self, draft: AdDraft, *, validate_only: bool) -> WriteResult:
        """Must create the ad PAUSED."""
