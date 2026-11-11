"""Alerts, emergency actions, control, jobs, system (§17)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on

from api.auth.adapter import Admin, Approver, Viewer
from api.deps import ClockDep, DbDep, SettingsDep, client_ip, load_client
from api.errors import NOT_ENABLED_RESPONSES, not_enabled, not_found
from api.schemas import (
    AdAccountOut,
    AlertOut,
    EmergencyActionOut,
    JobRunOut,
    KillSwitchRequest,
    KillSwitchResponse,
    KillSwitchState,
    ModeRequest,
    ModeResponse,
    PauseAllRequest,
    SystemResponse,
)
from api.services.queries import audit
from engine.db.models import AdAccount, Alert, Client, EmergencyAction, JobRun
from engine.enums import JobStatus, Mode, Platform, Severity

router = APIRouter(tags=["ops"])

# §9 job names (manual trigger targets).
JOB_NAMES = (
    "pacing",
    "hourly_pull",
    "daily_pipeline",
    "search_terms",
    "weekly_strategy",
    "approval_expiry",
    "emergency_restore",
    "token_reset",
    "retention",
    "backfill",
)
STALE_AFTER = dt.timedelta(hours=2)


# --------------------------------------------------------------------------- alerts


@router.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    open_only: bool = Query(True, alias="open", description="only un-acked alerts"),
    severity: Severity | None = None,
    limit: int = Query(200, ge=1, le=1000),
) -> list[AlertOut]:
    q = select(Alert).where(Alert.client_id == settings.client_id)
    if open_only:
        q = q.where(Alert.acked_at.is_(None))
    if severity:
        q = q.where(Alert.severity == severity.value)
    rows = db.scalars(q.order_by(Alert.created_at.desc(), Alert.id.desc()).limit(limit)).all()
    return [AlertOut.model_validate(a) for a in rows]


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
def ack_alert(
    *,
    alert_id: int,
    request: Request,
    user: Approver,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> AlertOut:
    a = db.get(Alert, alert_id)
    if a is None or a.client_id != settings.client_id:
        not_found("alert", alert_id)
    if a.acked_at is None:
        now = clock()
        a.acked_at = now
        a.acked_by = user.username
        audit(
            db,
            client_id=settings.client_id,
            actor=user.username,
            action="alert.ack",
            target=f"alert:{alert_id}",
            payload={"kind": a.kind, "severity": a.severity},
            ip=client_ip(request),
            at=now,
        )
        db.commit()
    return AlertOut.model_validate(a)


# --------------------------------------------------------------------------- emergency


@router.get("/emergency-actions", response_model=list[EmergencyActionOut])
def list_emergency_actions(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    active_only: bool = False,
    limit: int = Query(200, ge=1, le=1000),
) -> list[EmergencyActionOut]:
    q = select(EmergencyAction).where(EmergencyAction.client_id == settings.client_id)
    if active_only:
        q = q.where(EmergencyAction.restored_at.is_(None))
    rows = db.scalars(q.order_by(EmergencyAction.triggered_at.desc()).limit(limit)).all()
    return [EmergencyActionOut.model_validate(e) for e in rows]


@router.post("/emergency-actions/{action_id}/restore", responses=NOT_ENABLED_RESPONSES)
def restore_emergency(*, action_id: int, user: Approver, db: DbDep, settings: SettingsDep) -> None:
    """Restore campaigns paused by an emergency action (§12.4 step 6). Phase 3."""
    e = db.get(EmergencyAction, action_id)
    if e is None or e.client_id != settings.client_id:
        not_found("emergency action", action_id)
    not_enabled("3", "restore re-enables campaigns through the executor (Phase 3)")


# --------------------------------------------------------------------------- control


def _kill_state(settings: SettingsDep, client: Client) -> KillSwitchState:
    return KillSwitchState(
        effective=settings.effective_kill_switch(client.kill_switch),
        db=client.kill_switch,
        env_override=settings.kill_switch,
    )


@router.post("/control/mode", response_model=ModeResponse)
def set_mode(
    *,
    body: ModeRequest,
    request: Request,
    user: Admin,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> ModeResponse:
    """Set ``clients.mode``; rejected above ``HARD_MAX_MODE`` (§7.1)."""
    if not settings.mode_allowed(body.mode):
        raise HTTPException(
            403, f"mode {body.mode} is above HARD_MAX_MODE={settings.hard_max_mode} (env only)"
        )
    client = load_client(db, settings)
    before = client.mode
    now = clock()
    client.mode = body.mode.value
    client.updated_at = now
    audit(
        db,
        client_id=client.client_id,
        actor=user.username,
        action="control.mode",
        target=f"client:{client.client_id}",
        payload={"before": before, "after": body.mode.value, "reason": body.reason},
        ip=client_ip(request),
        at=now,
    )
    db.commit()
    return ModeResponse(mode=Mode(client.mode), hard_max_mode=settings.hard_max_mode)


@router.post("/control/kill-switch", response_model=KillSwitchResponse)
def set_kill_switch(
    *,
    body: KillSwitchRequest,
    request: Request,
    user: Admin,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> KillSwitchResponse:
    """Set the DB kill switch. The env ``KILL_SWITCH=true`` override cannot be cleared here."""
    client = load_client(db, settings)
    before = client.kill_switch
    now = clock()
    client.kill_switch = body.on
    client.updated_at = now
    audit(
        db,
        client_id=client.client_id,
        actor=user.username,
        action="control.kill_switch",
        target=f"client:{client.client_id}",
        payload={"before": before, "after": body.on, "reason": body.reason},
        ip=client_ip(request),
        at=now,
    )
    db.commit()
    state = _kill_state(settings, client)
    note = (
        "env KILL_SWITCH=true is set; it stays on until ops change the environment"
        if settings.kill_switch and not body.on
        else None
    )
    return KillSwitchResponse(**state.model_dump(), note=note)


@router.post("/control/pause-all", responses=NOT_ENABLED_RESPONSES)
def pause_all(*, body: PauseAllRequest, user: Approver) -> None:
    """Emergency pause of managed campaigns (§12.4). Needs the executor's emergency path and a
    write credential, which are installed only after the Phase 3 gate."""
    if not body.confirm:
        raise HTTPException(422, "pause-all requires confirm=true")
    not_enabled(
        "3",
        "pause-all needs the executor emergency path + write credentials (Phase 3 gate). "
        "Until then pause campaigns in Ads Manager / Google Ads; native account limits are "
        "the hard backstop (§12.5).",
    )


# --------------------------------------------------------------------------- jobs


@router.post("/jobs/{name}/run", responses=NOT_ENABLED_RESPONSES)
def run_job(*, name: str, user: Admin) -> None:
    """Manual job trigger. The scheduler runs in the engine container; the API→engine
    trigger channel is not built yet."""
    if name not in JOB_NAMES:
        raise HTTPException(404, f"unknown job {name!r}; known: {', '.join(JOB_NAMES)}")
    not_enabled(
        "1",
        f"manual trigger of {name!r} needs an API-to-engine trigger channel, which is not "
        "built yet; jobs currently run only on the engine scheduler.",
    )


# --------------------------------------------------------------------------- system


@router.get("/system", response_model=SystemResponse)
def system(*, user: Viewer, db: DbDep, settings: SettingsDep, clock: ClockDep) -> SystemResponse:
    """Job runs by step, last sync times, staleness (connector health proxy)."""
    client = load_client(db, settings)
    now = clock()
    accounts = [
        AdAccountOut.model_validate(a).model_copy(
            update={"stale": a.last_synced_at is None or now - a.last_synced_at > STALE_AFTER}
        )
        for a in db.scalars(
            select(AdAccount)
            .where(AdAccount.client_id == client.client_id)
            .order_by(AdAccount.platform, AdAccount.id)
        )
    ]
    latest = db.scalars(
        select(JobRun)
        .where(JobRun.client_id == client.client_id)
        .order_by(JobRun.job, JobRun.step.nulls_first(), JobRun.started_at.desc())
        .ext(distinct_on(JobRun.job, JobRun.step))
    ).all()
    failures = db.scalars(
        select(JobRun)
        .where(
            JobRun.client_id == client.client_id,
            JobRun.status == JobStatus.FAILED.value,
            JobRun.started_at >= now - dt.timedelta(days=7),
        )
        .order_by(JobRun.started_at.desc())
        .limit(50)
    ).all()
    return SystemResponse(
        client_id=client.client_id,
        mode=Mode(client.mode),
        hard_max_mode=settings.hard_max_mode,
        kill_switch=_kill_state(settings, client),
        auth_mode=settings.auth_mode,
        app_env=settings.app_env,
        server_time=now,
        ad_accounts=accounts,
        latest_job_runs=[JobRunOut.model_validate(j) for j in latest],
        recent_failures=[JobRunOut.model_validate(j) for j in failures],
        platform_operations_today={p.value: None for p in Platform},
    )
