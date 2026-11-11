"""Proposal vocabulary shared by every producer (rules, allocation, API manual proposals, later
LLM/evaluator) — one definition of ``dedupe_key`` and ``spend_direction`` (plan §8.3, §12.3).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from engine.db.models import Proposal
from engine.enums import (
    Action,
    EntityType,
    ProposalSource,
    ProposalStatus,
    Risk,
    SpendDirection,
)

# Actions a guardrail can judge / an executor can apply. ``noop`` never becomes a proposal and
# ``escalate`` proposals wait for triage (status ``escalated``) and are never executed.
ACTIONABLE: frozenset[Action] = frozenset(Action) - {Action.NOOP, Action.ESCALATE}


def dedupe_key(*parts: object) -> str:
    """§8.3: sha256(client_id | entity_type | entity_id | action | rule_key-or-source | as_of)."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def proposal_dedupe_key(
    client_id: str,
    *,
    entity_type: EntityType,
    entity_id: int,
    action: Action,
    rule_key_or_source: str,
    as_of: dt.date,
) -> str:
    return dedupe_key(
        client_id, entity_type.value, entity_id, action.value, rule_key_or_source, as_of.isoformat()
    )


def change_pct_of(params: dict[str, Any] | None) -> Decimal | None:
    try:
        pct = Decimal(str((params or {})["change_pct"]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None
    return pct if pct.is_finite() else None


def spend_direction(action: Action, params: dict[str, Any] | None) -> SpendDirection:
    """§12.3 definitions. A budget_change without a readable change_pct is ``neutral``
    (it can never pass the guardrail, which blocks invalid params)."""
    if action in (Action.PAUSE, Action.ADD_NEGATIVE):
        return SpendDirection.REDUCING
    if action is Action.BUDGET_CHANGE:
        pct = change_pct_of(params)
        if pct is None or pct == 0:
            return SpendDirection.NEUTRAL
        return SpendDirection.REDUCING if pct < 0 else SpendDirection.INCREASING
    if action in (Action.ENABLE, Action.NEW_AD, Action.NEW_CAMPAIGN):
        return SpendDirection.INCREASING
    return SpendDirection.NEUTRAL


@dataclass(frozen=True)
class ProposalDraft:
    as_of: dt.date
    source: ProposalSource
    rule_key: str | None
    entity_type: EntityType
    entity_id: int
    action: Action
    risk: Risk
    reason: str
    params: dict[str, Any] = field(default_factory=dict)
    expected_impact: dict[str, Any] | None = None
    status: ProposalStatus = ProposalStatus.PENDING

    def key(self, client_id: str) -> str:
        return proposal_dedupe_key(
            client_id,
            entity_type=self.entity_type,
            entity_id=self.entity_id,
            action=self.action,
            rule_key_or_source=self.rule_key or self.source.value,
            as_of=self.as_of,
        )


def insert_proposal(session: Session, client_id: str, draft: ProposalDraft) -> int | None:
    """Idempotent insert (``ON CONFLICT (dedupe_key) DO NOTHING``); returns the new id or None."""
    stmt = (
        insert(Proposal)
        .values(
            client_id=client_id,
            dedupe_key=draft.key(client_id),
            as_of=draft.as_of,
            source=draft.source.value,
            rule_key=draft.rule_key,
            entity_type=draft.entity_type,
            entity_id=draft.entity_id,
            action=draft.action.value,
            params=draft.params,
            expected_impact=draft.expected_impact,
            reason=draft.reason,
            risk=draft.risk.value,
            spend_direction=spend_direction(draft.action, draft.params).value,
            status=draft.status.value,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(Proposal.id)
    )
    new_id = session.scalar(stmt)
    return int(new_id) if new_id is not None else None
