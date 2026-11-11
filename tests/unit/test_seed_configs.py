from __future__ import annotations

import copy
from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.bootstrap import DEFAULT_LIMITS_PATH, DEFAULT_RULES_PATH, load_yaml, validate_seeds
from engine.config import Settings
from engine.errors import ConfigError, MissingInputError
from engine.guardrails.limits_schema import parse_limits
from engine.rules.schema import expression_names, parse_rules


def test_seed_files_validate() -> None:
    rules = parse_rules(load_yaml(DEFAULT_RULES_PATH))
    limits = parse_limits(load_yaml(DEFAULT_LIMITS_PATH))
    assert [r.priority for r in rules.rules] == sorted(r.priority for r in rules.rules)
    assert rules.stats.mc_draws == 10000
    assert limits.caps.total.daily == Decimal("20000")
    assert rules.inputs_confirmed is False and limits.inputs_confirmed is False


def test_unknown_rule_name_rejected() -> None:
    body = load_yaml(DEFAULT_RULES_PATH)
    body["rules"][0]["when"] = "spend_1d > 0 and made_up_metric > 1"
    with pytest.raises(ValidationError, match="made_up_metric"):
        parse_rules(body)


def test_rule_expression_syntax_guard() -> None:
    assert expression_names("max(spend_7d, 1) > target_cpa and True") == {
        "max",
        "spend_7d",
        "target_cpa",
    }
    with pytest.raises(ValueError, match="unsupported"):
        expression_names("spend_7d.__class__")


def test_platform_cap_cannot_exceed_total() -> None:
    body = load_yaml(DEFAULT_LIMITS_PATH)
    body["caps"]["meta"]["daily"] = 999999
    with pytest.raises(ValidationError):
        parse_limits(body)


def test_dev_allows_placeholders_prod_refuses(make_settings: Callable[..., Settings]) -> None:
    rules, limits = load_yaml(DEFAULT_RULES_PATH), load_yaml(DEFAULT_LIMITS_PATH)
    validate_seeds(make_settings(), rules, limits)
    with pytest.raises(MissingInputError) as exc:
        validate_seeds(make_settings(app_env="production"), rules, limits)
    assert exc.value.input_id == "I-1"


def test_confirmed_but_empty_conversion_action_refused(
    make_settings: Callable[..., Settings],
) -> None:
    rules = copy.deepcopy(load_yaml(DEFAULT_RULES_PATH))
    limits = copy.deepcopy(load_yaml(DEFAULT_LIMITS_PATH))
    rules["inputs_confirmed"] = limits["inputs_confirmed"] = True
    with pytest.raises(MissingInputError, match="meta_conversion_action"):
        validate_seeds(make_settings(app_env="staging"), rules, limits)


def test_caps_above_hard_ceiling_refused(make_settings: Callable[..., Settings]) -> None:
    rules, limits = load_yaml(DEFAULT_RULES_PATH), load_yaml(DEFAULT_LIMITS_PATH)
    with pytest.raises(ConfigError, match=r"caps\.total\.daily"):
        validate_seeds(make_settings(hard_daily_cap="15000"), rules, limits)
    with pytest.raises(MissingInputError):
        validate_seeds(make_settings(hard_daily_cap=None), rules, limits)
