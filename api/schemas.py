"""Pydantic v2 request/response models for /api/v1 (plan §17). Shared with the CLI (§5).

Conventions for API consumers (dashboard, CLI):
- Money fields (``Decimal``) serialise as JSON **strings** in major currency units, 2 dp
  (e.g. ``"1234.50"``) in the client currency. Never parse them as floats for arithmetic.
- Ratios (``ctr``, ``roas``, fractions, probabilities) are JSON numbers.
- Timestamps are ISO-8601 UTC with offset. Metric ``date`` / ``ts_hour`` are in the **ad
  account's** timezone (``ts_hour`` has no offset by design, §0 rule 9).

This module must stay free of SQLAlchemy / DB imports: the CLI imports it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from engine.enums import (
    Action,
    EntityType,
    JobRequestStatus,
    Mode,
    Platform,
    Risk,
    Role,
    WriteRequestKind,
)
from engine.guardrails.check import BlockReason


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- errors


class NotEnabledDetail(_Out):
    code: Literal["not_enabled"] = "not_enabled"
    phase: str = Field(description="Plan phase (§23) that enables this endpoint")
    message: str


class NotEnabledResponse(_Out):
    detail: NotEnabledDetail


class ErrorResponse(_Out):
    detail: Any


# --------------------------------------------------------------------------- auth


class LoginRequest(_In):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1000)


class TokenRequest(LoginRequest):
    role: Role | None = Field(
        default=None,
        description="Optional down-scope: issue the CLI token with a lower role than the user's",
    )


class LoginResponse(_Out):
    username: str
    role: Role
    expires_at: dt.datetime
    csrf_token: str = Field(
        description="Send back as the X-CSRF-Token header on every state-changing request "
        "(also set as the readable `aa_csrf` cookie)"
    )


class TokenResponse(_Out):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    username: str
    role: Role
    expires_at: dt.datetime


class MeResponse(_Out):
    username: str
    role: Role
    session_kind: str
    auth_mode: str


class SessionOut(_Out):
    id: int
    kind: str
    user: str
    role: Role
    created_at: dt.datetime
    expires_at: dt.datetime
    revoked_at: dt.datetime | None
    current: bool = False


# --------------------------------------------------------------------------- shared


class RangeMetrics(_Out):
    """Sums over complete account days ``[from_date, to_date]`` (inclusive)."""

    from_date: dt.date | None = None
    to_date: dt.date | None = None
    impressions: int = 0
    clicks: int = 0
    spend: Decimal = Decimal("0.00")
    conversions: Decimal = Decimal("0")
    conv_value: Decimal = Decimal("0.00")
    ctr: float | None = None
    cpa: Decimal | None = None
    roas: float | None = None


class ScoreOut(_Out):
    as_of: dt.date
    ctr_post_mean: float | None = None
    ctr_p10: float | None = None
    ctr_p90: float | None = None
    cpa_shrunk: Decimal | None = None
    cpa_p10: Decimal | None = None
    cpa_p90: Decimal | None = None
    aov_shrunk: Decimal | None = None
    roas_shrunk: float | None = None
    roas_p10: float | None = None
    roas_p90: float | None = None
    p_beats_target: float | None = None
    rank_metric: float | None = None
    rank_in_peer: int | None = None
    peer_size: int | None = None
    peer_group: str | None = None
    composite: int | None = None
    data_sufficient: bool = False


class TargetsOut(_Out):
    """``targets`` of the active rules version (§11.2, INPUT I-1). Placeholder values until
    the inputs are confirmed (``inputs_confirmed`` false in the seed)."""

    currency: str | None = None
    objective: Literal["conversions", "value", "traffic"] | None = None
    target_cpa: Decimal | None = None
    target_roas: Decimal | None = None
    rules_version: int | None = None


class ValueChange(_Out):
    """Human-readable before → after of one proposal / change (values as display strings;
    money in major units of the client currency)."""

    field: Literal["daily_budget", "status", "negative_keyword", "creative", "campaign", "none"]
    before: str | None = None
    after: str | None = None
    after_is_estimate: bool = Field(
        default=False,
        description="true when `after` is the guardrail's clamped value for a proposal that "
        "has not been applied yet (the executor re-runs the guardrail before applying)",
    )


class EntityRef(_Out):
    """Display fields of the entity a proposal / change / ranking row points at."""

    entity_name: str | None = None
    platform: Platform | None = None
    campaign_id: int | None = None
    campaign_name: str | None = None


# --------------------------------------------------------------------------- overview


class KillSwitchState(_Out):
    effective: bool
    db: bool
    env_override: bool = Field(description="KILL_SWITCH=true in env; the API cannot clear it")


class SpendBlock(_Out):
    platform: Literal["meta", "google", "total"]
    account_date: dt.date | None = Field(description="'today' in the ad account's timezone")
    elapsed_day_fraction: float | None = None
    spend_today: Decimal
    daily_cap: Decimal | None
    spent_fraction_today: float | None
    mtd_spend: Decimal
    monthly_cap: Decimal | None
    mtd_fraction: float | None
    range: RangeMetrics
    timezone: str | None = Field(default=None, description="ad account IANA timezone")
    projected_eod: Decimal | None = Field(
        default=None,
        description="pacing job's projected end-of-day spend (§12.4) for `account_date`; null "
        "when the pacing job has not run today yet or it is before `min_elapsed_hours`",
    )
    projected_eod_at: dt.datetime | None = Field(
        default=None, description="when the pacing job computed `projected_eod` (UTC)"
    )
    curve_source: Literal["learned", "linear", "mixed"] = Field(
        default="linear",
        description="intraday curve behind `intraday[].expected_cumulative`: learned from the "
        "last 28 days (§12.4 step 2, ≥ 14 usable days) or the h/24 fallback",
    )
    intraday: list[IntradayPoint] = Field(default_factory=list)


class IntradayPoint(_Out):
    """One account-local hour of today. ``expected_cumulative`` = curve(h+1) × daily cap."""

    hour: int = Field(ge=0, le=23)
    spend: Decimal | None = Field(description="null for hours that have not started yet")
    cumulative_spend: Decimal | None
    expected_cumulative: Decimal | None


class TrendPoint(_Out):
    """One complete account day (dates are account-local; platforms summed by date)."""

    date: dt.date
    impressions: int
    clicks: int
    spend: Decimal
    conversions: Decimal
    conv_value: Decimal
    cpa: Decimal | None
    roas: float | None
    spend_by_platform: dict[str, Decimal]


class AlertCounts(_Out):
    info: int = 0
    warn: int = 0
    critical: int = 0
    total: int = 0


class OverviewResponse(_Out):
    client_id: str
    client_name: str
    currency: str
    mode: Mode
    hard_max_mode: Mode
    kill_switch: KillSwitchState
    range_days: int
    platforms: list[SpendBlock]
    total: SpendBlock
    open_alerts: AlertCounts
    open_approvals: int
    active_emergency_actions: int
    last_synced_at: dict[str, dt.datetime | None]
    limits_version: int | None
    note: str | None = None
    timezone: str = Field(description="client timezone (TIMEZONE); accounts may differ")
    targets: TargetsOut | None = None
    trend_days: int
    trend: list[TrendPoint] = Field(
        default_factory=list, description="last `trend_days` complete days, oldest first"
    )
    alerts: list[AlertOut] = Field(
        default_factory=list, description="open warn/critical alerts, newest first (max 10)"
    )
    emergency_actions: list[EmergencyActionOut] = Field(
        default_factory=list, description="active (not restored) emergency actions"
    )


# --------------------------------------------------------------------------- entities


class AdOut(_Out):
    id: int
    ad_group_id: int
    platform: Platform
    external_id: str
    name: str | None
    status: str | None
    format: str | None
    creative_id: str | None
    headline: str | None
    body: str | None
    cta: str | None
    first_live_at: dt.datetime | None
    updated_at: dt.datetime
    metrics: RangeMetrics = Field(default_factory=RangeMetrics)
    score: ScoreOut | None = None


class KeywordOut(_Out):
    id: int
    ad_group_id: int
    platform: Platform
    external_id: str
    text: str
    match_type: str | None
    status: str | None
    is_negative: bool
    updated_at: dt.datetime
    metrics: RangeMetrics = Field(default_factory=RangeMetrics)
    score: ScoreOut | None = None


class AdGroupOut(_Out):
    id: int
    campaign_id: int
    platform: Platform
    external_id: str
    kind: str
    name: str | None
    status: str | None
    daily_budget: Decimal | None
    optimization_goal: str | None
    learning: bool | None
    learning_status_at: dt.datetime | None
    updated_at: dt.datetime
    metrics: RangeMetrics = Field(default_factory=RangeMetrics)
    score: ScoreOut | None = None


class AdGroupDetail(AdGroupOut):
    ads: list[AdOut] = Field(default_factory=list)
    keywords: list[KeywordOut] = Field(default_factory=list)


class CampaignOut(_Out):
    id: int
    ad_account_id: int
    platform: Platform
    external_id: str
    name: str | None
    objective: str | None
    kind: str
    automation_scope: str
    status: str | None
    budget_level: str
    daily_budget: Decimal | None
    lifetime_budget: Decimal | None
    shared_budget_external_id: str | None
    bid_strategy: str | None
    learning: bool | None
    managed: bool
    pool: str | None
    updated_at: dt.datetime
    metrics: RangeMetrics = Field(default_factory=RangeMetrics)
    score: ScoreOut | None = None


class CampaignDetail(CampaignOut):
    ad_groups: list[AdGroupOut] = Field(default_factory=list)


class ManagedRequest(_In):
    managed: bool
    reason: str | None = Field(default=None, max_length=2000)


# --------------------------------------------------------------------------- metrics


class MetricsPoint(_Out):
    date: dt.date | None = None
    ts_hour: dt.datetime | None = Field(default=None, description="account-local, no offset")
    impressions: int
    clicks: int
    spend: Decimal
    conversions: Decimal
    conv_value: Decimal
    video_views: int | None = None


class MetricsSeries(_Out):
    entity_type: EntityType
    entity_id: int
    platform: Platform | None
    grain: Literal["day", "hour"]
    from_date: dt.date
    to_date: dt.date
    points: list[MetricsPoint]


class RankingRow(ScoreOut):
    entity_type: EntityType
    entity_id: int
    platform: Platform
    name: str | None
    status: str | None
    campaign_id: int | None = Field(description="owning campaign (itself for campaigns)")
    campaign_name: str | None = None
    parent_id: int | None = Field(
        default=None, description="direct parent: campaign for ad groups, ad group for ads/keywords"
    )
    parent_name: str | None = None
    spend: Decimal | None = Field(
        default=None, description="spend in the settled scoring window (features.spend_s14)"
    )
    conversions: Decimal | None = Field(default=None, description="features.conversions_s14")
    spend_7d: Decimal | None = None
    conversions_7d: Decimal | None = None
    fatigue_flag: bool = False
    learning: bool | None = None


class RankingsResponse(_Out):
    level: EntityType
    as_of: dt.date | None
    rows: list[RankingRow]
    targets: TargetsOut | None = Field(
        default=None, description="objective and targets the scores were ranked against"
    )


# --------------------------------------------------------------------------- proposals


class ApprovalOut(_Out):
    id: int
    proposal_id: int
    channel: str | None
    requested_at: dt.datetime
    reminded_at: dt.datetime | None
    decided_at: dt.datetime | None
    decision: str | None
    decided_by: str | None
    comment: str | None


class GuardrailResultOut(_Out):
    """Typed ``proposals.guardrail_result`` (written by ``engine.guardrails.check.result_json``).

    - ``allow``: may be applied as is (``new_daily_budget`` for budget changes).
    - ``needs_approval``: ``reasons`` (e.g. ``mode_suggest``, ``risk_medium``,
      ``entity_learning``) say why a human must approve.
    - ``block``: ``reason`` is the first failing §12.3 check (``BlockReason``).

    ``detail`` for budget changes: ``current``, ``start_of_day_budget``,
    ``requested_change_pct``, ``lower_bound``, ``upper_bound``, ``new`` (strings), and ``why``
    when the change was too small; for blocks a ``why`` text.
    """

    result: Literal["allow", "needs_approval", "block"]
    reason: BlockReason | None = None
    reasons: list[str] = Field(default_factory=list)
    new_daily_budget: Decimal | None = None
    mode: Mode | None = None
    approved: bool = False
    checked_at: dt.datetime | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class TriageOut(_Out):
    """Outcome of LLM triage on an escalation (``params.triage``, Phase 4, D-21)."""

    outcome: str = Field(description="proposal | noop | flag_human")
    action: str | None = None
    confidence: float | None = None
    tier: str | None = None
    reason: str | None = Field(None, description="model's reason (untrusted text, display only)")
    cause: str | None = None
    llm_call_id: int | None = None
    proposal_id: int | None = Field(None, description="proposal created from this escalation")
    at: dt.datetime | None = None


class ProposalOut(EntityRef):
    id: int
    dedupe_key: str
    as_of: dt.date
    source: str
    rule_key: str | None
    entity_type: EntityType
    entity_id: int
    action: str
    params: dict[str, Any] | None
    expected_impact: dict[str, Any] | None
    reason: str | None
    risk: str
    spend_direction: str
    status: str
    guardrail_result: GuardrailResultOut | None
    expires_at: dt.datetime | None
    created_at: dt.datetime
    approval: ApprovalOut | None = None
    change: ValueChange | None = None
    triage: TriageOut | None = Field(None, description="LLM triage result for escalations")


class ApproveRequest(_In):
    comment: str | None = Field(default=None, max_length=2000)


class RejectRequest(_In):
    reason: str = Field(min_length=1, max_length=2000)


class BatchApproveRequest(_In):
    ids: list[int] = Field(min_length=1, max_length=100)
    comment: str | None = Field(default=None, max_length=2000)


class DecisionResult(_Out):
    proposal_id: int
    ok: bool
    status: str | None = None
    error: str | None = None


class DecisionResponse(_Out):
    proposal: ProposalOut
    next_step: str


class BatchDecisionResponse(_Out):
    results: list[DecisionResult]
    next_step: str


class ManualProposalRequest(_In):
    entity_type: EntityType
    entity_id: int
    action: Literal[
        Action.PAUSE,
        Action.ENABLE,
        Action.BUDGET_CHANGE,
        Action.ADD_NEGATIVE,
        Action.NEW_AD,
        Action.NEW_CAMPAIGN,
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)
    risk: Risk
    expected_impact: dict[str, Any] | None = None


# --------------------------------------------------------------------------- changes


class EvaluationOut(_Out):
    id: int
    change_id: int
    horizon_days: int
    due_at: dt.datetime
    done_at: dt.datetime | None
    unit_entity_type: EntityType
    unit_entity_id: int
    baseline: dict[str, Any] | None
    observed: dict[str, Any] | None
    p_worse: float | None
    p_better: float | None
    verdict: str | None
    action_taken: str | None


class ChangeOut(EntityRef):
    id: int
    proposal_id: int | None
    platform: Platform
    entity_type: EntityType
    entity_id: int
    action: str
    status: str
    before: dict[str, Any] | None
    intended: dict[str, Any] | None
    after: dict[str, Any] | None
    rollback_payload: dict[str, Any] | None
    validate_result: dict[str, Any] | None
    started_at: dt.datetime
    applied_at: dt.datetime | None
    applied_by: str | None
    rollback_of: int | None
    evaluations: list[EvaluationOut] = Field(default_factory=list)
    verdict: str | None = Field(default=None, description="latest final verdict, if any")
    change: ValueChange | None = None


# --------------------------------------------------------------------------- config


class ConfigVersionOut(_Out):
    id: int
    kind: str
    version: int
    body: dict[str, Any]
    active: bool
    changed_by: str
    reason: str | None
    created_at: dt.datetime


class ConfigVersionSummary(_Out):
    id: int
    version: int
    active: bool
    changed_by: str
    reason: str | None
    created_at: dt.datetime


class ConfigResponse(_Out):
    kind: str
    active: ConfigVersionOut | None
    history: list[ConfigVersionSummary]


class ConfigUpdateRequest(_In):
    body: dict[str, Any]
    reason: str = Field(min_length=1, max_length=2000)


# --------------------------------------------------------------------------- llm


class LLMUsageDay(_Out):
    date: dt.date
    tier: str
    calls: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost_usd: Decimal


class LLMUsageTotals(_Out):
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")


class LLMUsageResponse(_Out):
    range_days: int
    days: list[LLMUsageDay]
    by_purpose: dict[str, LLMUsageTotals]
    today: LLMUsageTotals
    month_to_date: LLMUsageTotals
    daily_token_cap: int
    daily_cost_cap_usd: Decimal
    monthly_cost_cap_usd: Decimal
    llm_enabled: bool = Field(
        description="engine.llm.status: true only when LLM_PROVIDER, key, models and prices are set"
    )
    provider: str | None = None
    disabled_reason: str | None = Field(None, description="why the LLM is off (null when enabled)")
    small_model: str | None = None
    large_model: str | None = None


# --------------------------------------------------------------------------- alerts etc.


class AlertOut(_Out):
    id: int
    dedupe_key: str
    severity: str
    kind: str
    entity_type: EntityType | None
    entity_id: int | None
    message: str
    sent_channels: list[str]
    last_sent_at: dt.datetime | None
    acked_at: dt.datetime | None
    acked_by: str | None
    created_at: dt.datetime


class EmergencyActionOut(_Out):
    id: int
    platform: Platform
    trigger: str
    campaign_ids: list[int]
    triggered_at: dt.datetime
    restore_policy: str
    restored_at: dt.datetime | None
    restored_by: str | None


class ModeRequest(_In):
    mode: Mode
    reason: str | None = Field(default=None, max_length=2000)


class ModeResponse(_Out):
    mode: Mode
    hard_max_mode: Mode


class KillSwitchRequest(_In):
    on: bool
    reason: str | None = Field(default=None, max_length=2000)


class KillSwitchResponse(KillSwitchState):
    note: str | None = None


class PauseAllRequest(_In):
    platform: Platform | None = Field(default=None, description="omit = both platforms")
    confirm: bool = False
    reason: str | None = Field(default=None, max_length=2000)


class RollbackRequest(_In):
    reason: str | None = Field(default=None, max_length=2000)


class RestoreRequest(_In):
    reason: str | None = Field(default=None, max_length=2000)


class WriteRequestOut(_Out):
    """A platform write queued by the API (``write_requests`` row, migration 0004) — run by
    the engine's executor within about a minute, never by the API (DECISIONS D-29)."""

    id: int
    kind: WriteRequestKind
    target_id: int | None
    params: dict[str, Any] | None
    status: JobRequestStatus
    requested_by: str
    requested_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    result: dict[str, Any] | None
    error: str | None


class ExecutionRequest(_In):
    enabled: bool
    reason: str | None = Field(default=None, max_length=2000)


class ExecutionState(_Out):
    """The write gate (DECISIONS D-28): platform writes happen only when it is open."""

    execution_enabled: bool = Field(description="clients.execution_enabled (default false)")
    mode: Mode
    hard_max_mode: Mode
    kill_switch: bool
    write_gate_open: bool = Field(description="open for spend-increasing writes")
    write_gate_reason: str | None = None


class DryRunOp(_Out):
    op: str
    platform: Platform
    entity_type: EntityType
    external_id: str
    before: dict[str, Any] | None = Field(description="as last synced (not read live)")
    intended: dict[str, Any]
    args: dict[str, Any]
    would_change: bool


class DryRunResponse(_Out):
    """What the executor would send (§17 dry-run). Built from DB state; the platform is not
    called and nothing is changed."""

    proposal_id: int
    action: str
    status: str
    guardrail: dict[str, Any] = Field(description="guardrail re-run now with approved=true")
    new_daily_budget: Decimal | None = None
    ops: list[DryRunOp] = Field(default_factory=list)
    plan_error: str | None = None
    execution: ExecutionState
    validate_only: bool = Field(
        True, description="the executor also sends a validate-only request before applying"
    )


class JobRunOut(_Out):
    id: int
    job: str
    step: str | None
    window_key: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    status: str
    rows_ingested: int | None
    error: str | None


class AdAccountOut(_Out):
    id: int
    platform: Platform
    external_id: str
    name: str | None
    status: str | None
    currency: str
    timezone: str
    last_synced_at: dt.datetime | None
    stale: bool = Field(default=False, description="last sync older than 2 h (§21) or never synced")


class HeartbeatOut(_Out):
    """Dead-man's switch (§21): the pacing job pings ``HEARTBEAT_URL`` after each run."""

    configured: bool = Field(description="HEARTBEAT_URL set on the engine (as last reported)")
    last_status: Literal["ok", "failed", "not_configured", "unknown"] = Field(
        description="outcome of the latest ping; unknown = the engine has not reported yet"
    )
    last_ping_at: dt.datetime | None = None
    last_pacing_success_at: dt.datetime | None = Field(
        default=None, description="latest successful `pacing` job_runs row"
    )
    healthy: bool = Field(
        description="pacing succeeded within the last 30 min and the last ping did not fail"
    )
    error: str | None = None


class PlatformOperationsOut(_Out):
    """Platform API operations used today by the engine (§4.3; alert at 80 % of the limit)."""

    platform: Platform
    day: dt.date | None = Field(default=None, description="account-local day of `used`")
    used: int | None = Field(description="null until the engine has made a call today")
    limit: int | None = Field(description="daily operation limit; null = no fixed limit (Meta)")
    fraction: float | None = None
    access_level: str | None = None
    updated_at: dt.datetime | None = None


class JobRequestOut(_Out):
    """A manual job trigger (``job_requests`` row) — run by the engine scheduler, not the API."""

    id: int
    job: str
    status: JobRequestStatus
    requested_by: str
    requested_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    error: str | None


class SystemResponse(_Out):
    client_id: str
    mode: Mode
    hard_max_mode: Mode
    kill_switch: KillSwitchState
    auth_mode: str
    app_env: str
    server_time: dt.datetime
    ad_accounts: list[AdAccountOut]
    latest_job_runs: list[JobRunOut]
    recent_failures: list[JobRunOut]
    heartbeat: HeartbeatOut
    platform_operations: list[PlatformOperationsOut]
    manual_jobs: list[str] = Field(description="job names accepted by POST /jobs/{name}/run")
    recent_job_requests: list[JobRequestOut] = Field(default_factory=list)
    execution: ExecutionState | None = None
    recent_write_requests: list[WriteRequestOut] = Field(default_factory=list)


# Resolve forward references (models above refer to ones defined later in this module).
for _model in (SpendBlock, OverviewResponse):
    _model.model_rebuild()
