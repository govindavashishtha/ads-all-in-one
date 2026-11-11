"""Approval decisions (plan §14 steps 3-5, 7). Called only from authenticated POSTs.

What this does NOT do (by design, until Phase 3): run the executor. An approved proposal
moves to ``approved``; the executor (Phase 3) must re-run ``guardrails.check(approved=True)``
with fresh state immediately before applying, and set ``blocked`` if it now blocks (§14.5).
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.auth.adapter import User
from api.services.queries import audit
from engine.db.models import Approval, Proposal
from engine.enums import ProposalStatus

NEXT_STEP_APPROVED = (
    "recorded; the executor re-runs the guardrail with fresh state before applying "
    "(executor ships in Phase 3 — nothing is sent to an ad platform yet)"
)
NEXT_STEP_REJECTED = "recorded; rejected proposals are never executed"


class DecisionError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def latest_approval(db: Session, proposal_id: int) -> Approval | None:
    return db.scalars(
        select(Approval)
        .where(Approval.proposal_id == proposal_id)
        .order_by(Approval.requested_at.desc(), Approval.id.desc())
        .limit(1)
    ).first()


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
    proposal = db.scalars(
        select(Proposal)
        .where(Proposal.id == proposal_id, Proposal.client_id == client_id)
        .with_for_update()
    ).one_or_none()
    if proposal is None:
        raise DecisionError(404, f"proposal {proposal_id} not found")
    if proposal.status != ProposalStatus.AWAITING_APPROVAL.value:
        raise DecisionError(
            409, f"proposal {proposal_id} is {proposal.status}, not awaiting_approval"
        )
    if proposal.expires_at is not None and proposal.expires_at <= now:
        raise DecisionError(409, f"proposal {proposal_id} expired at {proposal.expires_at}")

    approval = latest_approval(db, proposal_id)
    if approval is None or approval.decided_at is not None:
        approval = Approval(client_id=client_id, proposal_id=proposal_id, channel=user.session_kind)
        db.add(approval)
    approval.decided_at = now
    approval.decision = decision
    approval.decided_by = user.username
    approval.comment = comment
    proposal.status = (
        ProposalStatus.APPROVED.value if decision == "approved" else ProposalStatus.REJECTED.value
    )
    audit(
        db,
        client_id=client_id,
        actor=user.username,
        action=f"proposal.{'approve' if decision == 'approved' else 'reject'}",
        target=f"proposal:{proposal_id}",
        payload={"comment": comment, "via": user.session_kind, "role": user.role.value},
        ip=ip,
        at=now,
    )
    db.flush()
    return proposal


def as_http(err: DecisionError) -> HTTPException:
    return HTTPException(status_code=err.status_code, detail=err.detail)
