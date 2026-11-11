"""SQLAlchemy 2 models for every table in plan §8.3.

Conventions (§8.1): client_id everywhere, created_at TIMESTAMPTZ DEFAULT now(), identity ids,
platform ids as ``external_id TEXT``, money NUMERIC(14,2) in major units, UTC TIMESTAMPTZ.
Metric ``date`` / ``ts_hour`` are in the ad account's timezone (§0 rule 9) and therefore
stored as DATE / TIMESTAMP WITHOUT TIME ZONE (account-local wall-clock hour start).

Closed vocabularies are enforced with CHECK constraints (see ``engine.enums``); ``platform``
and ``entity_type`` are native Postgres enums as the plan specifies.
"""

from __future__ import annotations

import datetime as dt
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from engine.db.base import ENTITY_TYPE_ENUM, PLATFORM_ENUM, Base, ClientScoped, identity_pk
from engine.enums import (
    AdGroupKind,
    AutomationScope,
    BudgetLevel,
    CampaignKind,
    ChangeStatus,
    ConfigKind,
    EmergencyTrigger,
    EntityType,
    HistorySource,
    JobRequestStatus,
    JobStatus,
    LLMTier,
    Mode,
    NotificationKind,
    NotificationStatus,
    Platform,
    ProposalSource,
    ProposalStatus,
    RestorePolicy,
    Risk,
    Role,
    SessionKind,
    Severity,
    SpendDirection,
    Verdict,
)


def ck_in(
    column: str, values: type[StrEnum] | list[str], name: str | None = None
) -> CheckConstraint:
    vals = [m.value for m in values] if isinstance(values, type) else values
    quoted = ", ".join(f"'{v}'" for v in vals)
    return CheckConstraint(f"{column} IN ({quoted})", name=name or column)


MONEY = Numeric(14, 2)
CONV = Numeric(14, 4)


def _now() -> Any:
    return func.now()


# --------------------------------------------------------------------------- clients


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (ck_in("mode", Mode),)

    client_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text, server_default=Mode.READ_ONLY.value)
    kill_switch: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


# --------------------------------------------------------------------------- entities


class AdAccount(ClientScoped, Base):
    __tablename__ = "ad_accounts"
    __table_args__ = (UniqueConstraint("client_id", "platform", "external_id"),)

    id: Mapped[int] = identity_pk()
    platform: Mapped[Platform] = mapped_column(PLATFORM_ENUM)
    external_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)  # IANA, read from the platform
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Campaign(ClientScoped, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("client_id", "platform", "external_id"),
        ck_in("kind", CampaignKind),
        ck_in("automation_scope", AutomationScope),
        ck_in("budget_level", BudgetLevel),
    )

    id: Mapped[int] = identity_pk()
    ad_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ad_accounts.id"))
    platform: Mapped[Platform] = mapped_column(PLATFORM_ENUM)
    external_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    objective: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    automation_scope: Mapped[str] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    budget_level: Mapped[str] = mapped_column(Text)
    daily_budget: Mapped[Decimal | None] = mapped_column(MONEY)
    lifetime_budget: Mapped[Decimal | None] = mapped_column(MONEY)
    shared_budget_external_id: Mapped[str | None] = mapped_column(Text)
    bid_strategy: Mapped[str | None] = mapped_column(Text)
    learning: Mapped[bool | None] = mapped_column(Boolean)
    managed: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    pool: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class AdGroup(ClientScoped, Base):
    __tablename__ = "ad_groups"
    __table_args__ = (
        UniqueConstraint("client_id", "platform", "external_id"),
        ck_in("kind", AdGroupKind),
    )

    id: Mapped[int] = identity_pk()
    campaign_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("campaigns.id"))
    platform: Mapped[Platform] = mapped_column(PLATFORM_ENUM)
    external_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    daily_budget: Mapped[Decimal | None] = mapped_column(MONEY)
    optimization_goal: Mapped[str | None] = mapped_column(Text)
    learning: Mapped[bool | None] = mapped_column(Boolean)
    learning_status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class Ad(ClientScoped, Base):
    __tablename__ = "ads"
    __table_args__ = (UniqueConstraint("client_id", "platform", "external_id"),)

    id: Mapped[int] = identity_pk()
    ad_group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ad_groups.id"))
    platform: Mapped[Platform] = mapped_column(PLATFORM_ENUM)
    external_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    format: Mapped[str | None] = mapped_column(Text)
    creative_id: Mapped[str | None] = mapped_column(Text)
    headline: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    cta: Mapped[str | None] = mapped_column(Text)
    first_live_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class Keyword(ClientScoped, Base):
    __tablename__ = "keywords"
    __table_args__ = (
        UniqueConstraint("client_id", "ad_group_id", "external_id"),
        CheckConstraint("platform = 'google'", name="platform_google"),
    )

    id: Mapped[int] = identity_pk()
    ad_group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ad_groups.id"))
    platform: Mapped[Platform] = mapped_column(PLATFORM_ENUM, server_default=Platform.GOOGLE.value)
    external_id: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    match_type: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    is_negative: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class SearchTerm(ClientScoped, Base):
    __tablename__ = "search_terms"
    __table_args__ = (UniqueConstraint("client_id", "ad_group_id", "term", "date"),)

    id: Mapped[int] = identity_pk()
    ad_group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("ad_groups.id"))
    term: Mapped[str] = mapped_column(Text)
    date: Mapped[dt.date] = mapped_column(Date)  # account-local day
    impressions: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    clicks: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    cost: Mapped[Decimal] = mapped_column(MONEY, server_default=sa_text("0"))
    conversions: Mapped[Decimal] = mapped_column(CONV, server_default=sa_text("0"))
    conv_value: Mapped[Decimal] = mapped_column(MONEY, server_default=sa_text("0"))


class EntityHistory(ClientScoped, Base):
    """Append-only; one row only when a tracked field changed (§8.3)."""

    __tablename__ = "entity_history"
    __table_args__ = (
        ck_in("source", HistorySource),
        Index(
            "ix_entity_history_entity_observed",
            "entity_type",
            "entity_id",
            sa_text("observed_at DESC"),
        ),
    )

    id: Mapped[int] = identity_pk()
    entity_type: Mapped[EntityType] = mapped_column(ENTITY_TYPE_ENUM)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    daily_budget: Mapped[Decimal | None] = mapped_column(MONEY)
    learning: Mapped[bool | None] = mapped_column(Boolean)
    source: Mapped[str] = mapped_column(Text)


# --------------------------------------------------------------------------- metrics


class MetricsHourly(ClientScoped, Base):
    __tablename__ = "metrics_hourly"
    __table_args__ = (
        PrimaryKeyConstraint("client_id", "platform", "entity_type", "entity_id", "ts_hour"),
        ck_in("entity_type", ["account", "campaign"], name="entity_level"),
    )

    platform: Mapped[Platform] = mapped_column(PLATFORM_ENUM)
    entity_type: Mapped[EntityType] = mapped_column(ENTITY_TYPE_ENUM)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    ts_hour: Mapped[datetime] = mapped_column(DateTime(timezone=False))  # account-local hour
    impressions: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    clicks: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    spend: Mapped[Decimal] = mapped_column(MONEY, server_default=sa_text("0"))
    conversions: Mapped[Decimal] = mapped_column(CONV, server_default=sa_text("0"))
    conv_value: Mapped[Decimal] = mapped_column(MONEY, server_default=sa_text("0"))
    pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class MetricsDaily(ClientScoped, Base):
    __tablename__ = "metrics_daily"
    __table_args__ = (
        PrimaryKeyConstraint("client_id", "platform", "entity_type", "entity_id", "date"),
    )

    platform: Mapped[Platform] = mapped_column(PLATFORM_ENUM)
    entity_type: Mapped[EntityType] = mapped_column(ENTITY_TYPE_ENUM)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    date: Mapped[dt.date] = mapped_column(Date)  # account-local day
    impressions: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    clicks: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    spend: Mapped[Decimal] = mapped_column(MONEY, server_default=sa_text("0"))
    conversions: Mapped[Decimal] = mapped_column(CONV, server_default=sa_text("0"))
    conv_value: Mapped[Decimal] = mapped_column(MONEY, server_default=sa_text("0"))
    video_views: Mapped[int | None] = mapped_column(BigInteger)
    pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    is_final: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))


class MetricsWindow(ClientScoped, Base):
    __tablename__ = "metrics_window"
    __table_args__ = (
        PrimaryKeyConstraint(
            "client_id", "platform", "entity_type", "entity_id", "window_days", "as_of"
        ),
    )

    platform: Mapped[Platform] = mapped_column(PLATFORM_ENUM)
    entity_type: Mapped[EntityType] = mapped_column(ENTITY_TYPE_ENUM)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    window_days: Mapped[int] = mapped_column(SmallInteger)
    as_of: Mapped[dt.date] = mapped_column(Date)
    reach: Mapped[int | None] = mapped_column(BigInteger)
    frequency: Mapped[float | None] = mapped_column(Double)


class Feature(ClientScoped, Base):
    """One column per ``source='features'`` name in engine/rules/registry.py (test-enforced)."""

    __tablename__ = "features"
    __table_args__ = (PrimaryKeyConstraint("client_id", "entity_type", "entity_id", "as_of"),)

    entity_type: Mapped[EntityType] = mapped_column(ENTITY_TYPE_ENUM)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    as_of: Mapped[dt.date] = mapped_column(Date)
    is_budget_unit: Mapped[bool] = mapped_column(Boolean)
    learning: Mapped[bool | None] = mapped_column(Boolean)
    hours_live: Mapped[float | None] = mapped_column(Double)
    days_live: Mapped[float | None] = mapped_column(Double)
    impressions_1d: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    clicks_1d: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    spend_1d: Mapped[Decimal] = mapped_column(MONEY, server_default=sa_text("0"))
    conversions_1d: Mapped[Decimal] = mapped_column(CONV, server_default=sa_text("0"))
    spend_7d: Mapped[Decimal] = mapped_column(MONEY, server_default=sa_text("0"))
    conversions_7d: Mapped[Decimal] = mapped_column(CONV, server_default=sa_text("0"))
    spend_s14: Mapped[Decimal] = mapped_column(MONEY, server_default=sa_text("0"))
    conversions_s14: Mapped[Decimal] = mapped_column(CONV, server_default=sa_text("0"))
    impressions_s14: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    clicks_s14: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    ctr_1d: Mapped[float | None] = mapped_column(Double)
    ctr_7d: Mapped[float | None] = mapped_column(Double)
    ctr_28d: Mapped[float | None] = mapped_column(Double)
    cpa_1d: Mapped[Decimal | None] = mapped_column(MONEY)
    cpa_7d: Mapped[Decimal | None] = mapped_column(MONEY)
    cpa_28d: Mapped[Decimal | None] = mapped_column(MONEY)
    roas_7d: Mapped[float | None] = mapped_column(Double)
    roas_28d: Mapped[float | None] = mapped_column(Double)
    frequency_7d: Mapped[float | None] = mapped_column(Double)
    spend_pace_7d: Mapped[float | None] = mapped_column(Double)
    fatigue_flag: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    active_ads_in_group: Mapped[int | None] = mapped_column(Integer)


class Score(ClientScoped, Base):
    __tablename__ = "scores"
    __table_args__ = (PrimaryKeyConstraint("client_id", "entity_type", "entity_id", "as_of"),)

    entity_type: Mapped[EntityType] = mapped_column(ENTITY_TYPE_ENUM)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    as_of: Mapped[dt.date] = mapped_column(Date)
    ctr_post_mean: Mapped[float | None] = mapped_column(Double)
    ctr_p10: Mapped[float | None] = mapped_column(Double)
    ctr_p90: Mapped[float | None] = mapped_column(Double)
    theta_a: Mapped[float | None] = mapped_column(Double)
    theta_b: Mapped[float | None] = mapped_column(Double)
    cpa_shrunk: Mapped[Decimal | None] = mapped_column(MONEY)
    cpa_p10: Mapped[Decimal | None] = mapped_column(MONEY)
    cpa_p90: Mapped[Decimal | None] = mapped_column(MONEY)
    aov_shrunk: Mapped[Decimal | None] = mapped_column(MONEY)
    roas_shrunk: Mapped[float | None] = mapped_column(Double)
    roas_p10: Mapped[float | None] = mapped_column(Double)
    roas_p90: Mapped[float | None] = mapped_column(Double)
    p_beats_target: Mapped[float | None] = mapped_column(Double)
    rank_metric: Mapped[float | None] = mapped_column(Double)
    rank_in_peer: Mapped[int | None] = mapped_column(Integer)
    peer_size: Mapped[int | None] = mapped_column(Integer)
    peer_group: Mapped[str | None] = mapped_column(Text)
    composite: Mapped[int | None] = mapped_column(SmallInteger)  # display only (§10.5)
    data_sufficient: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))


# --------------------------------------------------------------------------- config


class ConfigVersion(ClientScoped, Base):
    __tablename__ = "config_versions"
    __table_args__ = (
        UniqueConstraint("client_id", "kind", "version"),
        ck_in("kind", ConfigKind),
        Index(
            "uq_config_versions_one_active",
            "client_id",
            "kind",
            unique=True,
            postgresql_where=sa_text("active"),
        ),
    )

    id: Mapped[int] = identity_pk()
    kind: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    changed_by: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- decisions


class Proposal(ClientScoped, Base):
    __tablename__ = "proposals"
    __table_args__ = (
        UniqueConstraint("dedupe_key"),
        ck_in("source", ProposalSource),
        ck_in("risk", Risk),
        ck_in("spend_direction", SpendDirection),
        ck_in("status", ProposalStatus),
        Index("ix_proposals_client_status", "client_id", "status"),
    )

    id: Mapped[int] = identity_pk()
    dedupe_key: Mapped[str] = mapped_column(Text)
    as_of: Mapped[dt.date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(Text)
    rule_key: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[EntityType] = mapped_column(ENTITY_TYPE_ENUM)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(Text)  # engine.enums.Action
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expected_impact: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(Text)
    spend_direction: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=ProposalStatus.PENDING.value)
    guardrail_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(ClientScoped, Base):
    __tablename__ = "approvals"
    __table_args__ = (ck_in("decision", ["approved", "rejected"]),)

    id: Mapped[int] = identity_pk()
    proposal_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("proposals.id"))
    channel: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)


class ChangeLog(ClientScoped, Base):
    __tablename__ = "change_log"
    __table_args__ = (
        ck_in("status", ChangeStatus),
        Index("ix_change_log_client_applied", "client_id", "applied_at"),
    )

    id: Mapped[int] = identity_pk()
    proposal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("proposals.id"))
    platform: Mapped[Platform] = mapped_column(PLATFORM_ENUM)
    entity_type: Mapped[EntityType] = mapped_column(ENTITY_TYPE_ENUM)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    intended: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rollback_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validate_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_by: Mapped[str | None] = mapped_column(Text)
    rollback_of: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("change_log.id"))


class EmergencyAction(ClientScoped, Base):
    __tablename__ = "emergency_actions"
    __table_args__ = (ck_in("trigger", EmergencyTrigger), ck_in("restore_policy", RestorePolicy))

    id: Mapped[int] = identity_pk()
    platform: Mapped[Platform] = mapped_column(PLATFORM_ENUM)
    trigger: Mapped[str] = mapped_column(Text)
    campaign_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    restore_policy: Mapped[str] = mapped_column(Text)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    restored_by: Mapped[str | None] = mapped_column(Text)


class Evaluation(ClientScoped, Base):
    __tablename__ = "evaluations"
    __table_args__ = (
        UniqueConstraint("change_id", "horizon_days"),  # idempotency (§0 rule 7)
        CheckConstraint("horizon_days IN (3, 7, 14)", name="horizon_days"),
        ck_in("verdict", Verdict),
        ck_in("unit_entity_type", ["campaign", "ad_group"], name="unit_level"),
        Index("ix_evaluations_due_open", "due_at", postgresql_where=sa_text("done_at IS NULL")),
    )

    id: Mapped[int] = identity_pk()
    change_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("change_log.id"))
    horizon_days: Mapped[int] = mapped_column(SmallInteger)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unit_entity_type: Mapped[EntityType] = mapped_column(ENTITY_TYPE_ENUM)
    unit_entity_id: Mapped[int] = mapped_column(BigInteger)
    baseline: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    observed: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    p_worse: Mapped[float | None] = mapped_column(Double)
    p_better: Mapped[float | None] = mapped_column(Double)
    verdict: Mapped[str | None] = mapped_column(Text)
    action_taken: Mapped[str | None] = mapped_column(Text)


class RuleStat(ClientScoped, Base):
    __tablename__ = "rule_stats"
    __table_args__ = (PrimaryKeyConstraint("client_id", "rule_key"),)

    rule_key: Mapped[str] = mapped_column(Text)
    n_changes: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    n_better: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    n_neutral: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    n_worse: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


# --------------------------------------------------------------------------- ops


class LLMCall(ClientScoped, Base):
    __tablename__ = "llm_calls"
    __table_args__ = (
        ck_in("tier", LLMTier),
        Index("ix_llm_calls_client_created", "client_id", "created_at"),
        Index("ix_llm_calls_prompt_hash", "prompt_hash"),
    )

    id: Mapped[int] = identity_pk()
    job: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    cached_input_tokens: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 5), server_default=sa_text("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_hash: Mapped[str | None] = mapped_column(Text)
    result_ref: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)


class Alert(ClientScoped, Base):
    __tablename__ = "alerts"
    __table_args__ = (
        ck_in("severity", Severity),
        Index(
            "uq_alerts_open_dedupe",
            "client_id",
            "dedupe_key",
            unique=True,
            postgresql_where=sa_text("acked_at IS NULL"),
        ),
    )

    id: Mapped[int] = identity_pk()
    dedupe_key: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[EntityType | None] = mapped_column(ENTITY_TYPE_ENUM)
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    message: Mapped[str] = mapped_column(Text)
    sent_channels: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=sa_text("'{}'"))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_by: Mapped[str | None] = mapped_column(Text)


class Notification(ClientScoped, Base):
    """Outbox for engine/notify (approval requests/reminders, digests). Added in 0002.

    Producers insert ``queued`` rows (idempotent via ``dedupe_key``); ``engine.notify.dispatch``
    delivers them on the configured channels and records the outcome. The rendered digest is
    stored here too (``kind='digest'``), so it doubles as the digest archive.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("client_id", "dedupe_key"),
        ck_in("kind", NotificationKind),
        ck_in("status", NotificationStatus),
        Index("ix_notifications_client_status", "client_id", "status"),
    )

    id: Mapped[int] = identity_pk()
    dedupe_key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    proposal_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("proposals.id"))
    status: Mapped[str] = mapped_column(Text, server_default=NotificationStatus.QUEUED.value)
    sent_channels: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=sa_text("'{}'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(ClientScoped, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("token_hash"),
        ck_in("kind", SessionKind),
        ck_in("role", Role),
    )

    id: Mapped[int] = identity_pk()
    token_hash: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    user: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(ClientScoped, Base):
    """Append-only (§8.3, §20). UPDATE/DELETE are rejected by a trigger in migration 0001."""

    __tablename__ = "audit_log"

    id: Mapped[int] = identity_pk()
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


class JobRun(ClientScoped, Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        # NULLS NOT DISTINCT so a step-less job run is still unique per window (PG >= 15).
        UniqueConstraint(
            "client_id", "job", "step", "window_key", postgresql_nulls_not_distinct=True
        ),
        ck_in("status", JobStatus),
    )

    id: Mapped[int] = identity_pk()
    job: Mapped[str] = mapped_column(Text)
    step: Mapped[str | None] = mapped_column(Text)
    window_key: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text)
    rows_ingested: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)


class JobRequest(ClientScoped, Base):
    """Manual job trigger queue (migration 0003, POST /jobs/{name}/run).

    The API inserts ``queued`` rows (admin only, audited, whitelisted names); the engine's
    ``job_requests`` poller claims them with ``FOR UPDATE SKIP LOCKED`` and runs the job in
    its own process, so the API never runs engine jobs or holds connector credentials.
    At most one open (queued/running) request per job (partial unique index).
    """

    __tablename__ = "job_requests"
    __table_args__ = (
        ck_in("status", JobRequestStatus),
        Index(
            "uq_job_requests_open",
            "client_id",
            "job",
            unique=True,
            postgresql_where=sa_text("status IN ('queued', 'running')"),
        ),
        Index("ix_job_requests_client_status", "client_id", "status"),
    )

    id: Mapped[int] = identity_pk()
    job: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())
    status: Mapped[str] = mapped_column(Text, server_default=JobRequestStatus.QUEUED.value)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class SystemStatus(ClientScoped, Base):
    """Engine-process facts the API cannot see directly (migration 0003): the pacing job's
    latest per-platform projection (``pacing:<platform>``), the heartbeat ping outcome
    (``heartbeat``) and per-platform API operations used today (``operations:<platform>``).
    One row per key, overwritten by the engine (latest value only)."""

    __tablename__ = "system_status"
    __table_args__ = (UniqueConstraint("client_id", "key"),)

    id: Mapped[int] = identity_pk()
    key: Mapped[str] = mapped_column(Text)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=_now())


ALL_TABLES: tuple[str, ...] = tuple(sorted(Base.metadata.tables))
