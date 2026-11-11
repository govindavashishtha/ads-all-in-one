"""§11 rule semantics: first match by priority, exceptions are false, None handling."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from engine.enums import Action, EntityType, ProposalStatus, Risk
from engine.rules.engine import draft_for, evaluate, first_match, ordered
from engine.rules.schema import Rule


def _rule(key: str, priority: int, when: str, action: str = "noop", **kw: Any) -> Rule:
    return Rule(key=key, priority=priority, applies_to=["campaign"], when=when, action=action, **kw)


def test_exceptions_and_none_are_false() -> None:
    assert evaluate("cpa_7d > 100", {"cpa_7d": 150.0}) is True
    assert evaluate("cpa_7d > 100", {"cpa_7d": None}) is False  # TypeError → false
    assert evaluate("cpa_7d / conversions_7d > 1", {"cpa_7d": 1.0, "conversions_7d": 0}) is False
    assert evaluate("missing_name > 1", {}) is False
    assert evaluate("__import__('os')", {}) is False
    assert evaluate("max(1, 2) == 2 and abs(-3) == 3 and min(1, 2) == 1", {}) is True
    assert evaluate("cpa_7d is None", {"cpa_7d": None}) is True


def test_first_match_by_priority_then_key() -> None:
    rules = ordered(
        [
            _rule("zeta", 10, "spend_7d > 0"),
            _rule("alpha", 10, "spend_7d > 0"),
            _rule("first", 5, "spend_7d > 999999"),
            _rule("broken", 1, "spend_7d > None"),
        ]
    )
    assert [r.key for r in rules] == ["broken", "first", "alpha", "zeta"]
    m = first_match(rules, EntityType.CAMPAIGN, 1, {"spend_7d": 10.0})
    assert m is not None and m.rule.key == "alpha"
    assert first_match(rules, EntityType.AD, 1, {"spend_7d": 10.0}) is None  # applies_to
    assert first_match(rules, EntityType.CAMPAIGN, 1, {"spend_7d": 0.0}) is None


def test_drafts_noop_escalate_and_default_risk() -> None:
    names = {"spend_7d": 10.0, "is_budget_unit": True}
    as_of = dt.date(2026, 11, 10)
    noop = first_match([_rule("hold", 1, "spend_7d > 0")], EntityType.CAMPAIGN, 1, names)
    assert noop is not None and draft_for(noop, as_of, None, {}) is None
    esc = first_match(
        [_rule("anom", 1, "spend_7d > 0", "escalate", reason="anomaly")],
        EntityType.CAMPAIGN,
        1,
        names,
    )
    assert esc is not None
    d = draft_for(esc, as_of, None, {})
    assert d is not None and d.status is ProposalStatus.ESCALATED and d.risk is Risk.MEDIUM
    assert d.reason.startswith("anom [anomaly]: spend_7d > 0 — spend_7d=10")
    bc = first_match(
        [_rule("scale", 1, "is_budget_unit", "budget_change", params={"change_pct": 15})],
        EntityType.CAMPAIGN,
        1,
        names,
    )
    assert bc is not None
    d = draft_for(bc, as_of, Decimal("1000"), {})
    assert d is not None and d.action is Action.BUDGET_CHANGE and d.risk is Risk.MEDIUM  # D-7
    assert d.params["change_pct"] == 15
    assert d.expected_impact is not None
    assert d.expected_impact["daily_budget_delta_est"] == "150.00"
