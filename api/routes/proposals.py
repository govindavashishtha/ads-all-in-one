"""/proposals (§14, §17). Decisions are authenticated POSTs only — approval links are GET
pages on the dashboard and never act (§14.3)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.auth.adapter import Admin, Approver, User, Viewer
from api.deps import ClockDep, DbDep, SettingsDep, client_ip, load_client
from api.errors import NOT_ENABLED_RESPONSES, not_enabled, not_found
from api.schemas import (
    ApprovalOut,
    ApproveRequest,
    BatchApproveRequest,
    BatchDecisionResponse,
    DecisionResponse,
    DecisionResult,
    ManualProposalRequest,
    ProposalOut,
    RejectRequest,
)
from api.services import approvals
from api.services.entities import parse_guardrail, parse_triage, proposal_change, resolve
from api.services.queries import audit
from engine.db.models import Ad, AdGroup, Campaign, Keyword, Proposal
from engine.enums import Action, EntityType, ProposalSource, ProposalStatus
from engine.proposals import dedupe_key as _engine_dedupe_key
from engine.proposals import spend_direction as _engine_spend_direction

router = APIRouter(prefix="/proposals", tags=["proposals"])

_ENTITY_MODELS: dict[EntityType, Any] = {
    EntityType.CAMPAIGN: Campaign,
    EntityType.AD_GROUP: AdGroup,
    EntityType.AD: Ad,
    EntityType.KEYWORD: Keyword,
}


def _outs(db: Session, rows: Sequence[Proposal]) -> list[ProposalOut]:
    """ProposalOut with the latest approval, entity display fields, typed guardrail result
    and before → after."""
    if not rows:
        return []
    infos = resolve(db, rows[0].client_id, [(p.entity_type, p.entity_id) for p in rows])
    out = []
    for p in rows:
        appr = approvals.latest_approval(db, p.id)
        info = infos.get((p.entity_type, p.entity_id))
        gr = parse_guardrail(p.guardrail_result)
        base = ProposalOut.model_validate(
            {
                **{c: getattr(p, c) for c in _PROPOSAL_COLUMNS},
                "guardrail_result": gr,
            }
        )
        out.append(
            base.model_copy(
                update={
                    "approval": ApprovalOut.model_validate(appr) if appr else None,
                    "entity_name": info.entity_name if info else None,
                    "platform": info.platform if info else None,
                    "campaign_id": info.campaign_id if info else None,
                    "campaign_name": info.campaign_name if info else None,
                    "change": proposal_change(p.action, p.params, p.expected_impact, gr, info),
                    "triage": parse_triage(p.params),
                }
            )
        )
    return out


def _out(db: Session, p: Proposal) -> ProposalOut:
    return _outs(db, [p])[0]


_PROPOSAL_COLUMNS = (
    "id",
    "dedupe_key",
    "as_of",
    "source",
    "rule_key",
    "entity_type",
    "entity_id",
    "action",
    "params",
    "expected_impact",
    "reason",
    "risk",
    "spend_direction",
    "status",
    "expires_at",
    "created_at",
)


@router.get("", response_model=list[ProposalOut])
def list_proposals(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    status: ProposalStatus | None = None,
    entity_type: EntityType | None = None,
    entity_id: int | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[ProposalOut]:
    q = select(Proposal).where(Proposal.client_id == settings.client_id)
    if status:
        q = q.where(Proposal.status == status.value)
    if entity_type:
        q = q.where(Proposal.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(Proposal.entity_id == entity_id)
    rows = db.scalars(q.order_by(Proposal.id.desc()).limit(limit).offset(offset)).all()
    return _outs(db, rows)


@router.post("/approve-batch", response_model=BatchDecisionResponse)
def approve_batch(
    *,
    body: BatchApproveRequest,
    request: Request,
    user: Approver,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> BatchDecisionResponse:
    """Approve several proposals; each is decided (and later guardrailed) individually."""
    now = clock()
    results: list[DecisionResult] = []
    for pid in dict.fromkeys(body.ids):
        nested = db.begin_nested()
        try:
            p = approvals.decide(
                db,
                client_id=settings.client_id,
                proposal_id=pid,
                decision="approved",
                user=user,
                comment=body.comment,
                now=now,
                ip=client_ip(request),
            )
            nested.commit()
            results.append(DecisionResult(proposal_id=pid, ok=True, status=p.status))
        except approvals.DecisionError as err:
            nested.rollback()
            results.append(DecisionResult(proposal_id=pid, ok=False, error=err.detail))
    db.commit()
    return BatchDecisionResponse(results=results, next_step=approvals.NEXT_STEP_APPROVED)


@router.get("/{proposal_id}", response_model=ProposalOut)
def get_proposal(
    *, proposal_id: int, user: Viewer, db: DbDep, settings: SettingsDep
) -> ProposalOut:
    p = db.get(Proposal, proposal_id)
    if p is None or p.client_id != settings.client_id:
        not_found("proposal", proposal_id)
    return _out(db, p)


def _decide(
    db: Session,
    *,
    settings: Any,
    user: User,
    request: Request,
    now: Any,
    proposal_id: int,
    decision: Any,
    comment: str | None,
) -> Proposal:
    try:
        p = approvals.decide(
            db,
            client_id=settings.client_id,
            proposal_id=proposal_id,
            decision=decision,
            user=user,
            comment=comment,
            now=now,
            ip=client_ip(request),
        )
    except approvals.DecisionError as err:
        raise approvals.as_http(err) from err
    db.commit()
    return p


@router.post("/{proposal_id}/approve", response_model=DecisionResponse)
def approve(
    *,
    proposal_id: int,
    request: Request,
    user: Approver,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    body: ApproveRequest | None = None,
) -> DecisionResponse:
    comment = body.comment if body else None
    p = _decide(
        db,
        settings=settings,
        user=user,
        request=request,
        now=clock(),
        proposal_id=proposal_id,
        decision="approved",
        comment=comment,
    )
    return DecisionResponse(proposal=_out(db, p), next_step=approvals.NEXT_STEP_APPROVED)


@router.post("/{proposal_id}/reject", response_model=DecisionResponse)
def reject(
    *,
    proposal_id: int,
    body: RejectRequest,
    request: Request,
    user: Approver,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> DecisionResponse:
    p = _decide(
        db,
        settings=settings,
        user=user,
        request=request,
        now=clock(),
        proposal_id=proposal_id,
        decision="rejected",
        comment=body.reason,
    )
    return DecisionResponse(proposal=_out(db, p), next_step=approvals.NEXT_STEP_REJECTED)


@router.get("/{proposal_id}/dry-run", responses=NOT_ENABLED_RESPONSES)
def dry_run(*, proposal_id: int, user: Viewer, db: DbDep, settings: SettingsDep) -> None:
    """Payload that would be sent to the platform. Built by the executor (Phase 3)."""
    p = db.get(Proposal, proposal_id)
    if p is None or p.client_id != settings.client_id:
        not_found("proposal", proposal_id)
    not_enabled("3", "dry-run payloads are built by the executor, which ships in Phase 3")


# One definition for every producer (engine.proposals); re-exported for existing callers.
spend_direction = _engine_spend_direction
dedupe_key = _engine_dedupe_key


@router.post("", response_model=ProposalOut, status_code=201)
def create_manual(
    *,
    body: ManualProposalRequest,
    request: Request,
    user: Admin,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> ProposalOut:
    """Manual proposal (source=human), inserted as ``pending``. It is guardrailed like any
    other by the daily pipeline's ``guardrail`` step; this endpoint never executes anything."""
    client = load_client(db, settings)
    action = Action(body.action)
    model = _ENTITY_MODELS.get(body.entity_type)
    if model is None:
        raise HTTPException(422, f"manual proposals are not supported for {body.entity_type}")
    entity = db.get(model, body.entity_id)
    if entity is None or entity.client_id != client.client_id:
        not_found(body.entity_type.value, body.entity_id)
    params = dict(body.params)
    if action is Action.BUDGET_CHANGE:
        try:
            pct = Decimal(str(params.get("change_pct")))
        except InvalidOperation as exc:
            raise HTTPException(422, "budget_change needs numeric params.change_pct") from exc
        if not pct.is_finite() or pct == 0:
            raise HTTPException(422, "budget_change needs a non-zero params.change_pct")
    now = clock()
    as_of = now.astimezone(ZoneInfo(client.timezone)).date()
    key = dedupe_key(
        client.client_id,
        body.entity_type.value,
        body.entity_id,
        action.value,
        ProposalSource.HUMAN.value,
        as_of.isoformat(),
    )
    stmt = (
        insert(Proposal)
        .values(
            client_id=client.client_id,
            dedupe_key=key,
            as_of=as_of,
            source=ProposalSource.HUMAN.value,
            rule_key=None,
            entity_type=body.entity_type,
            entity_id=body.entity_id,
            action=action.value,
            params=params,
            expected_impact=body.expected_impact,
            reason=body.reason,
            risk=body.risk.value,
            spend_direction=spend_direction(action, params).value,
            status=ProposalStatus.PENDING.value,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(Proposal.id)
    )
    new_id = db.scalar(stmt)
    if new_id is None:
        existing = db.scalar(select(Proposal.id).where(Proposal.dedupe_key == key))
        raise HTTPException(409, f"an identical proposal already exists today (id {existing})")
    audit(
        db,
        client_id=client.client_id,
        actor=user.username,
        action="proposal.create",
        target=f"proposal:{new_id}",
        payload={"action": action.value, "entity": f"{body.entity_type.value}:{body.entity_id}"},
        ip=client_ip(request),
        at=now,
    )
    db.commit()
    p = db.get(Proposal, new_id)
    assert p is not None
    return _out(db, p)
