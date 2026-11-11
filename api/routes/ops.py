"""Alerts, emergency actions, control, jobs, system (§17)."""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on, insert

from api.auth.adapter import Admin, Approver, Viewer
from api.deps import ClockDep, DbDep, SettingsDep, client_ip, load_client
from api.errors import NOT_ENABLED_RESPONSES, not_enabled, not_found
from api.schemas import (
    AdAccountOut,
    AlertOut,
    EmergencyActionOut,
    HeartbeatOut,
    JobRequestOut,
    JobRunOut,
    KillSwitchRequest,
    KillSwitchResponse,
    KillSwitchState,
    ModeRequest,
    ModeResponse,
    PauseAllRequest,
    PlatformOperationsOut,
    SystemResponse,
)
from api.services.queries import audit
from engine.connectors.factory import COUNTERS
from engine.db.models import AdAccount, Alert, Client, EmergencyAction, JobRequest, JobRun
from engine.enums import JobRequestStatus, JobStatus, Mode, Platform, Severity
from engine.jobs.manual import MANUAL_JOBS
from engine.system_status import HEARTBEAT, get_statuses, operations_key

router = APIRouter(tags=["ops"])

# §9 job names. MANUAL_JOBS (engine.jobs.manual) can be queued via POST /jobs/{name}/run;
# the others answer 501 (later phase) or 422 (operator CLI only).
JOB_NAMES = (
    *MANUAL_JOBS,
    "emergency_restore",
    "token_reset",
    "retention",
    "backfill",
)
_LATER_PHASE = {"emergency_restore": "3", "token_reset": "4"}
STALE_AFTER = dt.timedelta(hours=2)
HEARTBEAT_HEALTHY_WITHIN = dt.timedelta(minutes=30)
# Daily operation limit per platform as configured in the engine's connector counters
# (engine/connectors/factory.py COUNTERS; Google = Explorer access, VERIFY-P0 V-1).
ACCESS_LEVEL = {
    Platform.META: "no fixed daily limit (rate-limited per app/account)",
    Platform.GOOGLE: "explorer (assumed until Basic access is confirmed, VERIFY-P0 V-1)",
}


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


@router.post(
    "/jobs/{name}/run",
    status_code=202,
    response_model=JobRequestOut,
    responses={
        **NOT_ENABLED_RESPONSES,
        409: {"description": "a request for this job is already queued or running"},
    },
)
def run_job(
    *,
    name: str,
    request: Request,
    user: Admin,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> JobRequestOut:
    """Queue a manual run (§17, §19 ``ads run``). The API never runs jobs itself: it inserts a
    ``job_requests`` row that the engine scheduler picks up within about a minute (poll it via
    ``GET /jobs/requests/{id}``). Only ``MANUAL_JOBS`` are accepted; at most one open request
    per job. Jobs keep their own locks and per-window idempotency, so a duplicate run is a
    no-op. Nothing here writes to an ad platform."""
    if name not in JOB_NAMES:
        raise HTTPException(404, f"unknown job {name!r}; known: {', '.join(JOB_NAMES)}")
    if name in _LATER_PHASE:
        not_enabled(_LATER_PHASE[name], f"job {name!r} ships in Phase {_LATER_PHASE[name]}")
    if name not in MANUAL_JOBS:
        raise HTTPException(
            422,
            f"job {name!r} cannot be triggered from the API; operators run it with "
            f"`python -m engine.jobs {name}` on the engine host",
        )
    client = load_client(db, settings)
    now = clock()
    new_id = db.scalar(
        insert(JobRequest)
        .values(
            client_id=client.client_id,
            job=name,
            requested_by=user.username,
            requested_at=now,
            status=JobRequestStatus.QUEUED.value,
        )
        .on_conflict_do_nothing(
            index_elements=["client_id", "job"],
            index_where=JobRequest.status.in_(
                [JobRequestStatus.QUEUED.value, JobRequestStatus.RUNNING.value]
            ),
        )
        .returning(JobRequest.id)
    )
    if new_id is None:
        db.rollback()
        open_req = db.scalars(
            select(JobRequest).where(
                JobRequest.client_id == client.client_id,
                JobRequest.job == name,
                JobRequest.status.in_(
                    [JobRequestStatus.QUEUED.value, JobRequestStatus.RUNNING.value]
                ),
            )
        ).first()
        raise HTTPException(
            409,
            {
                "code": "already_queued",
                "message": f"a {name!r} request is already "
                f"{open_req.status if open_req else 'open'}",
                "request_id": open_req.id if open_req else None,
            },
        )
    audit(
        db,
        client_id=client.client_id,
        actor=user.username,
        action="job.run_requested",
        target=f"job_request:{new_id}",
        payload={"job": name},
        ip=client_ip(request),
        at=now,
    )
    db.commit()
    req = db.get(JobRequest, new_id)
    assert req is not None
    return JobRequestOut.model_validate(req)


@router.get("/jobs/requests", response_model=list[JobRequestOut])
def list_job_requests(
    *, user: Viewer, db: DbDep, settings: SettingsDep, limit: int = Query(50, ge=1, le=500)
) -> list[JobRequestOut]:
    rows = db.scalars(
        select(JobRequest)
        .where(JobRequest.client_id == settings.client_id)
        .order_by(JobRequest.requested_at.desc(), JobRequest.id.desc())
        .limit(limit)
    ).all()
    return [JobRequestOut.model_validate(r) for r in rows]


@router.get("/jobs/requests/{request_id}", response_model=JobRequestOut)
def get_job_request(
    *, request_id: int, user: Viewer, db: DbDep, settings: SettingsDep
) -> JobRequestOut:
    r = db.get(JobRequest, request_id)
    if r is None or r.client_id != settings.client_id:
        not_found("job request", request_id)
    return JobRequestOut.model_validate(r)


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
        heartbeat=_heartbeat(db, client.client_id, now),
        platform_operations=_operations(db, client.client_id, now, accounts),
        manual_jobs=list(MANUAL_JOBS),
        recent_job_requests=[
            JobRequestOut.model_validate(r)
            for r in db.scalars(
                select(JobRequest)
                .where(JobRequest.client_id == client.client_id)
                .order_by(JobRequest.requested_at.desc(), JobRequest.id.desc())
                .limit(10)
            )
        ],
    )


def _parse_ts(v: Any) -> dt.datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return dt.datetime.fromisoformat(v)
    except ValueError:
        return None


def _heartbeat(db: DbDep, cid: str, now: dt.datetime) -> HeartbeatOut:
    last_ok = db.scalar(
        select(JobRun.finished_at)
        .where(
            JobRun.client_id == cid,
            JobRun.job == "pacing",
            JobRun.status == JobStatus.SUCCESS.value,
        )
        .order_by(JobRun.finished_at.desc().nulls_last())
        .limit(1)
    )
    snap = get_statuses(db, cid, [HEARTBEAT]).get(HEARTBEAT)
    value = snap[0] if snap else {}
    status = value.get("last_status") if snap else "unknown"
    if status not in ("ok", "failed", "not_configured"):
        status = "unknown"
    recent = last_ok is not None and now - last_ok <= HEARTBEAT_HEALTHY_WITHIN
    return HeartbeatOut(
        configured=bool(value.get("configured")),
        last_status=status,
        last_ping_at=_parse_ts(value.get("last_ping_at")),
        last_pacing_success_at=last_ok,
        healthy=recent and status != "failed",
        error=value.get("error") if isinstance(value.get("error"), str) else None,
    )


def _operations(
    db: DbDep, cid: str, now: dt.datetime, accounts: list[AdAccountOut]
) -> list[PlatformOperationsOut]:
    snaps = get_statuses(db, cid, [operations_key(p.value) for p in Platform])
    zones = {a.platform: a.timezone for a in accounts}
    out = []
    for p in Platform:
        limit = COUNTERS[p].daily_limit
        used: int | None = None
        day: dt.date | None = None
        updated: dt.datetime | None = None
        snap = snaps.get(operations_key(p.value))
        if snap is not None:
            value, updated = snap
            try:
                day = dt.date.fromisoformat(str(value.get("day")))
                used = int(value.get("used"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                day, used = None, None
            tz = zones.get(p)
            if day is not None and tz is not None and day != now.astimezone(ZoneInfo(tz)).date():
                used = 0  # counter is from an earlier account day: nothing used today yet
                day = now.astimezone(ZoneInfo(tz)).date()
        out.append(
            PlatformOperationsOut(
                platform=p,
                day=day,
                used=used,
                limit=limit,
                fraction=(used / limit) if used is not None and limit else None,
                access_level=ACCESS_LEVEL[p],
                updated_at=updated,
            )
        )
    return out
