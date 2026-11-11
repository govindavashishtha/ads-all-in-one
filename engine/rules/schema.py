"""Shape of the rules config (plan §10 stats, §11.2). Used by bootstrap and PUT /rules.

Includes load-time validation that expressions reference only registry names (§11.1).
Expression *evaluation* (simpleeval, first-match) belongs to engine/rules/engine.py.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from engine.enums import Action, EntityType, Risk
from engine.rules.registry import ALLOWED_FUNCTIONS, NAMES

_CONSTANTS = frozenset({"True", "False", "None"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Targets(_Strict):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    objective: Literal["conversions", "value", "traffic"]
    target_cpa: Decimal = Field(gt=0)
    target_roas: Decimal | None = Field(default=None, gt=0)
    meta_conversion_action: str


class Stats(_Strict):
    score_window_days: int = Field(gt=0)
    settle_days: int = Field(ge=0)
    prior_window_days: int = Field(gt=0)
    ctr_prior_impressions: int = Field(gt=0)
    theta_prior_spend_x_cpa: Decimal = Field(gt=0)
    aov_prior_conversions: Decimal = Field(gt=0)
    min_parent_spend_x_cpa: Decimal = Field(ge=0)
    mc_draws: int = Field(gt=0)


def expression_names(expr: str) -> set[str]:
    """Names referenced by a rule expression (raises SyntaxError if it doesn't parse)."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute | ast.Subscript | ast.Lambda):
            raise ValueError(f"unsupported syntax in rule expression: {expr!r}")
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - _CONSTANTS


class Rule(_Strict):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    priority: int = Field(ge=1, le=999)
    applies_to: list[EntityType] = Field(min_length=1)
    when: str
    action: Action
    reason: str | None = None
    params: dict[str, Any] | None = None
    risk: Risk | None = None

    @field_validator("when")
    @classmethod
    def _names_in_registry(cls, v: str) -> str:
        try:
            names = expression_names(v)
        except SyntaxError as exc:
            raise ValueError(f"rule expression does not parse: {v!r}") from exc
        unknown = names - NAMES - ALLOWED_FUNCTIONS
        if unknown:
            raise ValueError(f"unknown names in rule expression: {sorted(unknown)}")
        return v


class RulesConfig(_Strict):
    version: int = Field(ge=1)
    inputs_confirmed: bool = False
    targets: Targets
    stats: Stats
    rules: list[Rule] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_keys(self) -> RulesConfig:
        keys = [r.key for r in self.rules]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate rule keys")
        return self


def parse_rules(body: dict[str, Any]) -> RulesConfig:
    return RulesConfig.model_validate(body)
