"""Guardrail interface (plan §12.3). PURE function: no I/O, no clock, no randomness.

``check(proposal, state, *, approved=False) -> Allow | NeedsApproval | Block``

Foundation defines the types and signature only. The guardrails agent implements the 12
ordered checks exactly as in §12.3 and the hypothesis invariants in §12.6
(tests/property/). Extend ``GuardrailState`` if a check needs more precomputed facts — keep
it a plain snapshot so ``check`` stays deterministic (§12.6 invariant 6).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from engine.enums import (
    Action,
    AutomationScope,
    BudgetLevel,
    EntityType,
    Mode,
    Platform,
    ProposalSource,
    Risk,
    SpendDirection,
)
from engine.guardrails.limits_schema import LimitsConfig


class BlockReason(StrEnum):
    """Block reasons in §12.3 order."""

    MODE_READ_ONLY = "mode_read_only"  # 1
    KILL_SWITCH = "kill_switch"  # 2
    NOT_MANAGED = "not_managed"  # 3
    UNSUPPORTED_SCOPE = "unsupported_scope"  # 4
    COOLDOWN = "cooldown"  # 5
    LEARNING_PHASE = "learning_phase"  # 6
    MIN_ACTIVE_ADS = "min_active_ads"  # 7
    CHANGE_TOO_SMALL = "change_too_small"  # 8
    CAP_EXCEEDED = "cap_exceeded"  # 9


@dataclass(frozen=True, slots=True)
class ProposalView:
    """The fields of a ``proposals`` row the guardrail needs."""

    id: int | None
    source: ProposalSource
    rule_key: str | None
    entity_type: EntityType
    entity_id: int
    action: Action
    params: dict[str, Any]
    risk: Risk
    spend_direction: SpendDirection
    from_pause_all: bool = False  # action came from /control/pause-all (privileged, §12.3)


@dataclass(frozen=True, slots=True)
class EntitySnapshot:
    """The proposal's entity and its campaign, as of ``GuardrailState.now``."""

    platform: Platform
    entity_type: EntityType
    entity_id: int
    campaign_id: int
    campaign_managed: bool
    automation_scope: AutomationScope
    budget_level: BudgetLevel
    is_budget_unit: bool
    learning: bool | None  # NULL is treated as true (§8.2)
    status: str | None
    current_daily_budget: Decimal | None
    start_of_day_budget: Decimal | None  # latest entity_history at/before account-day start
    active_ads_in_group: int | None  # for ad pauses
    last_applied_change_at: dt.datetime | None  # latest change_log 'applied' for this entity
    last_manual_change_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class BudgetUnitSnapshot:
    """One active budget unit (managed or not) for the budget-sum cap check (§12.3 step 9)."""

    platform: Platform
    entity_type: EntityType
    entity_id: int
    daily_budget: Decimal


@dataclass(frozen=True, slots=True)
class GuardrailState:
    mode: Mode
    kill_switch_db: bool
    kill_switch_env: bool
    limits: LimitsConfig
    entity: EntitySnapshot
    active_budget_units: tuple[BudgetUnitSnapshot, ...]
    mtd_spend: dict[Platform, Decimal]
    remaining_days_in_month: int  # account-local, including today
    auto_pauses_applied_today: int
    now: dt.datetime  # UTC; passed in so check() never reads the clock


@dataclass(frozen=True, slots=True)
class Allow:
    new_daily_budget: Decimal | None = None  # clamped value from step 8, if budget_change
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NeedsApproval:
    reasons: tuple[str, ...]
    new_daily_budget: Decimal | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Block:
    reason: BlockReason
    detail: dict[str, Any] = field(default_factory=dict)


GuardrailResult = Allow | NeedsApproval | Block


def check(
    proposal: ProposalView, state: GuardrailState, *, approved: bool = False
) -> GuardrailResult:
    """Run §12.3 checks 1–12 in order; first Block wins. ``approved=True`` skips steps 10–11."""
    raise NotImplementedError("guardrails.check is implemented by the guardrails agent (§12.3)")
