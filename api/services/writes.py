"""API side of the write path (DECISIONS D-28, D-29): queue platform writes for the engine,
report the write gate, build dry-runs. The API never holds a write credential and never
imports a write connector; it only reads the DB and inserts ``write_requests`` rows."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from api.schemas import DryRunOp, DryRunResponse, ExecutionState, WriteRequestOut
from api.services.queries import audit
from engine.config import Settings
from engine.db.models import Client, Keyword, Proposal, WriteRequest
from engine.enums import Action, JobRequestStatus, Mode, WriteRequestKind
from engine.executor.gate import write_gate
from engine.executor.plan import _ENTITY_MODELS, OpKind, PlanError, WriteOp, build_plan, describe
from engine.guardrails.check import Block, check, result_json
from engine.guardrails.state import EntityMissingError, StateBuilder, proposal_view

OPEN = [JobRequestStatus.QUEUED.value, JobRequestStatus.RUNNING.value]


def execution_state(db: Session, settings: Settings) -> ExecutionState:
    client = db.get(Client, settings.client_id)
    gate = write_gate(db, settings, reducing=False)
    return ExecutionState(
        execution_enabled=bool(client.execution_enabled) if client else False,
        mode=Mode(client.mode) if client else Mode.READ_ONLY,
        hard_max_mode=settings.hard_max_mode,
        kill_switch=gate.kill_switch,
        write_gate_open=gate.open,
        write_gate_reason=gate.reason,
    )


def queue_write(
    db: Session,
    settings: Settings,
    *,
    kind: WriteRequestKind,
    dedupe_key: str,
    target_id: int | None,
    params: dict[str, Any] | None,
    actor: str,
    ip: str | None,
    now: dt.datetime,
) -> WriteRequestOut:
    """Insert one queued request (409 if the same one is already open) + audit; commits."""
    new_id = db.scalar(
        insert(WriteRequest)
        .values(
            client_id=settings.client_id,
            kind=kind.value,
            dedupe_key=dedupe_key,
            target_id=target_id,
            params=params,
            requested_by=actor,
            requested_at=now,
            status=JobRequestStatus.QUEUED.value,
        )
        .on_conflict_do_nothing(
            index_elements=["client_id", "dedupe_key"], index_where=WriteRequest.status.in_(OPEN)
        )
        .returning(WriteRequest.id)
    )
    if new_id is None:
        db.rollback()
        open_req = db.scalars(
            select(WriteRequest).where(
                WriteRequest.client_id == settings.client_id,
                WriteRequest.dedupe_key == dedupe_key,
                WriteRequest.status.in_(OPEN),
            )
        ).first()
        raise HTTPException(
            409,
            {
                "code": "already_queued",
                "message": f"this {kind.value} request is already "
                f"{open_req.status if open_req else 'open'}",
                "request_id": open_req.id if open_req else None,
            },
        )
    audit(
        db,
        client_id=settings.client_id,
        actor=actor,
        action=f"write.{kind.value}_requested",
        target=f"write_request:{new_id}",
        payload={"target_id": target_id, **(params or {})},
        ip=ip,
        at=now,
    )
    db.commit()
    req = db.get(WriteRequest, new_id)
    assert req is not None
    return WriteRequestOut.model_validate(req)


def _db_before(db: Session, settings: Settings, op: WriteOp) -> dict[str, Any] | None:
    if op.kind is OpKind.CREATE_AD:
        return {"ad": None}
    if op.kind is OpKind.ADD_NEGATIVE:
        a = op.args
        exists = db.scalar(
            select(Keyword.external_id)
            .where(
                Keyword.client_id == settings.client_id,
                Keyword.is_negative.is_(True),
                Keyword.text == a["text"],
                Keyword.status == "active",
            )
            .limit(1)
        )
        return {"negative": True, "status": "active"} if exists else {"negative": None}
    model = _ENTITY_MODELS.get(op.entity_type)
    if model is None:
        return None
    row = db.get(model, op.log_entity_id)
    if row is None:
        return None
    budget = getattr(row, "daily_budget", None)
    return {"status": row.status, "daily_budget": None if budget is None else str(budget)}


def dry_run(db: Session, settings: Settings, p: Proposal, now: dt.datetime) -> DryRunResponse:
    execution = execution_state(db, settings)
    guard: dict[str, Any]
    new_budget = None
    blocked = False
    if Action(p.action) is Action.ROLLBACK_WITH_STATE_DRIFT:
        guard = {
            "result": "n/a",
            "note": "forced rollback of change "
            f"#{(p.params or {}).get('change_id')}; payload = its rollback_payload",
        }
    else:
        try:
            b = StateBuilder(db, settings, now)
            view = proposal_view(p)
            result = check(view, b.state(view.entity_type, view.entity_id), approved=True)
            guard = result_json(result, approved=True, mode=b.mode, checked_at=now)
            blocked = isinstance(result, Block)
            new_budget = None if blocked else result.new_daily_budget  # type: ignore[union-attr]
        except EntityMissingError as exc:
            guard, blocked = (
                {"result": "block", "reason": "unsupported_scope", "detail": {"why": str(exc)}},
                True,
            )
    ops: list[DryRunOp] = []
    plan_error = None
    if not blocked and Action(p.action) is not Action.ROLLBACK_WITH_STATE_DRIFT:
        try:
            for op in build_plan(db, settings.client_id, p, new_budget):
                ops.append(DryRunOp.model_validate(describe(op, _db_before(db, settings, op))))
        except PlanError as exc:
            plan_error = str(exc)
    return DryRunResponse(
        proposal_id=p.id,
        action=p.action,
        status=p.status,
        guardrail=guard,
        new_daily_budget=new_budget,
        ops=ops,
        plan_error=plan_error,
        execution=execution,
    )
