"""Interface contracts other agents build on (no business logic tested here)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api.main import app
from engine.bootstrap import DEFAULT_LIMITS_PATH, load_yaml
from engine.connectors.base import ReadConnector
from engine.credentials.base import MetaCredentials
from engine.enums import (
    Access,
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
from engine.guardrails import check as guardrails
from engine.guardrails.limits_schema import parse_limits


def _meta_creds(access: Access) -> MetaCredentials:
    return MetaCredentials(access, "1", SecretStr("s"), SecretStr("t"), "act_1", "vTEST")


def test_connector_interfaces_are_abstract_and_access_checked() -> None:
    with pytest.raises(TypeError):
        ReadConnector(_meta_creds(Access.READ))  # type: ignore[abstract]

    class Dummy(ReadConnector):
        platform = Platform.META

    for name in sorted(ReadConnector.__abstractmethods__):
        setattr(Dummy, name, lambda self, *a, **k: None)
    Dummy.__abstractmethods__ = frozenset()
    Dummy(_meta_creds(Access.READ))  # type: ignore[abstract]
    with pytest.raises(ValueError, match="needs read"):
        Dummy(_meta_creds(Access.WRITE))  # type: ignore[abstract]


def test_guardrail_signature_exists_but_is_unimplemented() -> None:
    limits = parse_limits(load_yaml(DEFAULT_LIMITS_PATH))
    entity = guardrails.EntitySnapshot(
        platform=Platform.META,
        entity_type=EntityType.CAMPAIGN,
        entity_id=1,
        campaign_id=1,
        campaign_managed=True,
        automation_scope=AutomationScope.FULL,
        budget_level=BudgetLevel.CAMPAIGN,
        is_budget_unit=True,
        learning=False,
        status="active",
        current_daily_budget=Decimal("1000"),
        start_of_day_budget=Decimal("1000"),
        active_ads_in_group=None,
        last_applied_change_at=None,
        last_manual_change_at=None,
    )
    state = guardrails.GuardrailState(
        mode=Mode.READ_ONLY,
        kill_switch_db=False,
        kill_switch_env=False,
        limits=limits,
        entity=entity,
        active_budget_units=(),
        mtd_spend={},
        remaining_days_in_month=20,
        auto_pauses_applied_today=0,
        now=dt.datetime(2026, 11, 11, tzinfo=dt.UTC),
    )
    proposal = guardrails.ProposalView(
        id=None,
        source=ProposalSource.RULE,
        rule_key="scale_winner",
        entity_type=EntityType.CAMPAIGN,
        entity_id=1,
        action=Action.BUDGET_CHANGE,
        params={"change_pct": 15},
        risk=Risk.LOW,
        spend_direction=SpendDirection.INCREASING,
    )
    with pytest.raises(NotImplementedError):
        guardrails.check(proposal, state)
    assert next(iter(guardrails.BlockReason)).value == "mode_read_only"


def test_health_endpoint() -> None:
    assert TestClient(app).get("/api/v1/health").json() == {"status": "ok"}
