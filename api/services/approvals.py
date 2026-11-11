"""Approval decisions for the API (plan §14 steps 3-5, 7). Called only from authenticated POSTs.

Thin adapter: the logic lives in ``engine.approvals.service`` (single source of truth, shared
with the engine's approval jobs and the Phase 3 executor). This module only maps the API's
``User`` onto it and errors onto HTTP.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from api.auth.adapter import User
from engine.approvals import service
from engine.approvals.service import (
    NEXT_STEP_APPROVED,
    NEXT_STEP_REJECTED,
    DecisionError,
    latest_approval,
)
from engine.db.models import Proposal

__all__ = [
    "NEXT_STEP_APPROVED",
    "NEXT_STEP_REJECTED",
    "DecisionError",
    "as_http",
    "decide",
    "latest_approval",
]


def decide(
    db: Session,
    *,
    client_id: str,
    proposal_id: int,
    decision: Literal["approved", "rejected"],
    user: User,
    comment: str | None,
    now: dt.datetime,
    ip: str | None,
) -> Proposal:
    """Record one decision. Raises DecisionError (404/409). Caller commits."""
    return service.decide(
        db,
        client_id=client_id,
        proposal_id=proposal_id,
        decision=decision,
        actor=user.username,
        role=user.role.value,
        via=user.session_kind,
        comment=comment,
        now=now,
        ip=ip,
    )


def as_http(err: DecisionError) -> HTTPException:
    return HTTPException(status_code=err.status_code, detail=err.detail)
