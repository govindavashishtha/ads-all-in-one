"""Builders for LLM-layer unit tests (no database)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from engine.db.models import Proposal
from engine.enums import EntityType, Platform
from engine.features.data import EntityInfo
from engine.llm.summariser import Case, case_for

AS_OF = dt.date(2026, 11, 10)


def entity(
    entity_type: str = "ad_group",
    *,
    id: int = 7,
    is_budget_unit: bool = True,
    budget_level: str = "ad_set",
    learning: bool = False,
    automation_scope: str = "full",
    campaign_kind: str = "standard",
    platform: str = "meta",
    managed: bool = True,
    name: str | None = "Retargeting 30d",
    daily_budget: str | None = "1000",
) -> EntityInfo:
    et = EntityType(entity_type)
    return EntityInfo(
        entity_type=et,
        id=id,
        platform=Platform(platform),
        name=name,
        ad_account_id=1,
        campaign_id=1 if et is not EntityType.CAMPAIGN else id,
        ad_group_id=id if et is EntityType.AD_GROUP else (3 if et is EntityType.AD else None),
        status="active",
        effective_active=True,
        campaign_kind=campaign_kind,
        campaign_objective="conversions",
        automation_scope=automation_scope,
        budget_level=budget_level,
        campaign_managed=managed,
        pool=None,
        daily_budget=Decimal(daily_budget) if daily_budget and is_budget_unit else None,
        learning_raw=learning,
        learning_effective=learning,
        is_budget_unit=is_budget_unit,
    )


def escalation(pid: int, e: EntityInfo, rule: str = "cpa_spike_anomaly") -> Proposal:
    return Proposal(
        id=pid,
        as_of=AS_OF,
        rule_key=rule,
        entity_type=e.entity_type,
        entity_id=e.id,
        action="escalate",
        params={},
        status="escalated",
    )


def make_case(
    pid: int,
    e: EntityInfo,
    rule: str = "cpa_spike_anomaly",
    metrics: dict[str, Any] | None = None,
    *,
    triage: bool = True,
) -> Case:
    f = {"days_live": 30, "active_ads_in_group": 4, **(metrics or {})}
    s = {"p_beats_target": 0.18, "cpa_p90": 910, "rank_in_peer": 4, "peer_size": 5}
    return case_for(escalation(pid, e, rule), e, f, s, triage=triage)
