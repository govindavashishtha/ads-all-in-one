"""/changes and /evaluations (§17). Rollback is queued for the engine executor (D-29)."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select

from api.auth.adapter import Approver, Viewer
from api.deps import ClockDep, DbDep, SettingsDep, client_ip
from api.errors import not_found
from api.schemas import ChangeOut, EvaluationOut, RollbackRequest, WriteRequestOut
from api.services.entities import change_log_change, resolve
from api.services.writes import queue_write
from api.timeutil import parse_days
from engine.db.models import ChangeLog, Evaluation
from engine.enums import ChangeStatus, WriteRequestKind

router = APIRouter(tags=["changes"])


def _since(value: str | None, now: dt.datetime) -> dt.datetime | None:
    if value is None:
        return None
    if value.endswith("d"):
        return now - dt.timedelta(days=parse_days(value, "since"))
    try:
        d = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, "since must be like '7d' or an ISO date") from exc
    return dt.datetime.combine(d, dt.time(), tzinfo=dt.UTC)


@router.get("/changes", response_model=list[ChangeOut])
def list_changes(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    since: str | None = Query(None, description="'7d' or ISO date (UTC midnight)"),
    status: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> list[ChangeOut]:
    """Change feed (newest first) with evaluations; ``verdict`` = latest D+7/D+14 verdict."""
    q = select(ChangeLog).where(ChangeLog.client_id == settings.client_id)
    start = _since(since, clock())
    if start:
        q = q.where(ChangeLog.started_at >= start)
    if status:
        q = q.where(ChangeLog.status == status)
    rows = db.scalars(
        q.order_by(ChangeLog.started_at.desc(), ChangeLog.id.desc()).limit(limit)
    ).all()
    evals: dict[int, list[Evaluation]] = defaultdict(list)
    if rows:
        for e in db.scalars(
            select(Evaluation)
            .where(Evaluation.change_id.in_([r.id for r in rows]))
            .order_by(Evaluation.horizon_days)
        ):
            evals[e.change_id].append(e)
    infos = resolve(db, settings.client_id, [(r.entity_type, r.entity_id) for r in rows])
    out = []
    for r in rows:
        ev = evals.get(r.id, [])
        finals = [e for e in ev if e.horizon_days >= 7 and e.verdict]
        info = infos.get((r.entity_type, r.entity_id))
        out.append(
            ChangeOut.model_validate(r).model_copy(
                update={
                    "evaluations": [EvaluationOut.model_validate(e) for e in ev],
                    "verdict": finals[-1].verdict if finals else None,
                    "entity_name": info.entity_name if info else None,
                    "campaign_id": info.campaign_id if info else None,
                    "campaign_name": info.campaign_name if info else None,
                    "change": change_log_change(r.action, r.before, r.intended, r.after),
                }
            )
        )
    return out


@router.post(
    "/changes/{change_id}/rollback",
    status_code=202,
    response_model=WriteRequestOut,
    responses={409: {"description": "not applied, or a rollback is already queued"}},
)
def rollback(
    *,
    change_id: int,
    request: Request,
    user: Approver,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    body: RollbackRequest | None = None,
) -> WriteRequestOut:
    """Queue a manual rollback (§15). The engine re-reads the platform: if the state still
    equals the change's result it applies the rollback payload (guarded: steps 1–4, plus caps
    when it increases spend; write gate D-28); if someone changed it since, it creates a
    ``rollback_with_state_drift`` proposal that needs approval. Poll
    ``GET /write-requests/{id}``. Audited."""
    row = db.get(ChangeLog, change_id)
    if row is None or row.client_id != settings.client_id:
        not_found("change", change_id)
    if row.status != ChangeStatus.APPLIED.value:
        raise HTTPException(
            409, f"change {change_id} is {row.status}; only applied changes can be rolled back"
        )
    return queue_write(
        db,
        settings,
        kind=WriteRequestKind.ROLLBACK,
        dedupe_key=f"rollback:{change_id}",
        target_id=change_id,
        params={"reason": body.reason} if body and body.reason else None,
        actor=user.username,
        ip=client_ip(request),
        now=clock(),
    )


@router.get("/evaluations", response_model=list[EvaluationOut])
def list_evaluations(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    change_id: int | None = None,
    open_only: bool = False,
    limit: int = Query(200, ge=1, le=1000),
) -> list[EvaluationOut]:
    q = select(Evaluation).where(Evaluation.client_id == settings.client_id)
    if change_id is not None:
        q = q.where(Evaluation.change_id == change_id)
    if open_only:
        q = q.where(Evaluation.done_at.is_(None))
    rows = db.scalars(q.order_by(Evaluation.due_at.desc()).limit(limit)).all()
    return [EvaluationOut.model_validate(e) for e in rows]
