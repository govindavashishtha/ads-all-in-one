"""Shape of the limits config (plan §12.2). Used by bootstrap and PUT /limits.

Validation only (types, ranges, internal consistency, HARD_* ceilings). No guardrail logic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.enums import Action, Platform
from engine.errors import ConfigError

Pct = Annotated[Decimal, Field(ge=0, le=100)]
Fraction = Annotated[Decimal, Field(gt=0)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Cap(_Strict):
    daily: Decimal = Field(gt=0)
    monthly: Decimal = Field(gt=0)


class Caps(_Strict):
    total: Cap
    meta: Cap
    google: Cap

    @model_validator(mode="after")
    def _platform_le_total(self) -> Caps:
        for p in (self.meta, self.google):
            if p.daily > self.total.daily or p.monthly > self.total.monthly:
                raise ValueError("a platform cap cannot exceed the total cap")
        return self

    def for_platform(self, platform: Platform) -> Cap:
        return self.meta if platform is Platform.META else self.google


class CampaignLimits(_Strict):
    max_daily_budget: Decimal = Field(gt=0)
    min_daily_budget: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> CampaignLimits:
        if self.min_daily_budget > self.max_daily_budget:
            raise ValueError("min_daily_budget > max_daily_budget")
        return self


class ChangeLimits(_Strict):
    max_budget_change_pct_per_day: Pct
    min_useful_change_pct: Pct
    auto_max_budget_change_pct: Pct
    auto_max_abs_change: Decimal = Field(ge=0)
    max_auto_pauses_per_day: int = Field(ge=0)
    keep_min_active_ads_per_group: int = Field(ge=0)


class Cooldowns(_Strict):
    same_entity_days: int = Field(ge=0)
    after_manual_change_hours: int = Field(ge=0)


class LearningPhase(_Strict):
    block_budget_changes: bool
    block_pauses: bool


class PlatformFlex(_Strict):
    meta: Decimal = Field(gt=0)
    google: Decimal = Field(gt=0)


class Pacing(_Strict):
    min_elapsed_hours: Decimal = Field(ge=0, le=24)
    warn_spent_fraction: Fraction
    warn_projected_ratio: Fraction
    emergency_spent_fraction: Fraction
    platform_flex: PlatformFlex
    emergency_stop_enabled: bool


class Allocation(_Strict):
    share_floor_factor: Decimal = Field(ge=0, le=1)
    share_ceiling: Decimal = Field(gt=0, le=1)
    min_spend_pace: Decimal = Field(ge=0)
    max_frequency_7d: Decimal = Field(gt=0)


class LimitsConfig(_Strict):
    version: int = Field(ge=1)
    inputs_confirmed: bool = False
    caps: Caps
    campaign: CampaignLimits
    change: ChangeLimits
    cooldowns: Cooldowns
    learning_phase: LearningPhase
    pacing: Pacing
    allocation: Allocation
    approval_required_for: list[Action]

    def check_hard_ceilings(self, hard_daily: Decimal, hard_monthly: Decimal) -> None:
        """§7.1: no cap may exceed a HARD_* ceiling (HARD_* are totals across platforms)."""
        too_high = [
            f"caps.{name}.{period}={value}"
            for name, cap in (
                ("total", self.caps.total),
                ("meta", self.caps.meta),
                ("google", self.caps.google),
            )
            for period, value, ceiling in (
                ("daily", cap.daily, hard_daily),
                ("monthly", cap.monthly, hard_monthly),
            )
            if value > ceiling
        ]
        if too_high:
            raise ConfigError(
                f"limits exceed HARD ceilings (daily={hard_daily}, monthly={hard_monthly}): "
                + ", ".join(too_high)
            )


def parse_limits(body: dict[str, Any]) -> LimitsConfig:
    return LimitsConfig.model_validate(body)
