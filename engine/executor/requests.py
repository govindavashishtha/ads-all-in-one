"""``write_requests`` poller (migration 0004, DECISIONS D-29): API → engine queue for platform
writes requested by a human (rollback, emergency restore, pause-all).

The API never holds a write credential (§20): it validates, audits and inserts a ``queued``
row (202). This poller (every minute, inside the engine) claims the oldest queued row with
``FOR UPDATE SKIP LOCKED``, runs it through the executor and stores the result. A request
left ``running`` for more than 30 minutes is marked failed (the engine died); the write
itself is protected by its idempotency key and the reconciler.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import select, update

from engine.db.models import WriteRequest
from engine.enums import JobRequestStatus, Platform, WriteRequestKind
from engine.executor.apply import WriteEnv
from engine.executor.rollback import rollback_change

log = structlog.get_logger(__name__)

STALE_RUNNING = dt.timedelta(minutes=30)


def _run(  # noqa: PLR0917
    env: WriteEnv,
    req_id: int,
    kind: str,
    target: int | None,
    by: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    if kind == WriteRequestKind.ROLLBACK.value:
        assert target is not None
        r = rollback_change(env, target, requested_by=by)
        return {
            "status": r.status,
            "detail": r.detail,
            "rollback_change_id": r.change_id,
            "drift_proposal_id": r.drift_proposal_id,
        }
    from engine.pacing.emergency import pause_all, restore_action

    if kind == WriteRequestKind.RESTORE.value:
        assert target is not None
        return restore_action(env, target, requested_by=by)
    if kind == WriteRequestKind.PAUSE_ALL.value:
        platform = (params or {}).get("platform")
        results = pause_all(
            env,
            requested_by=by,
            request_id=req_id,
            platforms=[Platform(platform)] if platform else None,
        )
        blocked = [r.blocked for r in results if r.blocked]
        return {
            "status": "blocked" if len(blocked) == len(results) else "done",
            "platforms": [r.as_json() for r in results],
        }
    raise ValueError(f"unknown write request kind {kind!r}")


def process_write_requests(env: WriteEnv, *, max_requests: int = 5) -> int:
    with env.sessions() as s, s.begin():
        s.execute(
            update(WriteRequest)
            .where(
                WriteRequest.client_id == env.client_id,
                WriteRequest.status == JobRequestStatus.RUNNING.value,
                WriteRequest.started_at < env.now() - STALE_RUNNING,
            )
            .values(
                status=JobRequestStatus.FAILED.value,
                finished_at=env.now(),
                error="engine stopped while the request was running",
            )
        )
    done = 0
    while done < max_requests:
        with env.sessions() as s, s.begin():
            req = s.scalars(
                select(WriteRequest)
                .where(
                    WriteRequest.client_id == env.client_id,
                    WriteRequest.status == JobRequestStatus.QUEUED.value,
                )
                .order_by(WriteRequest.requested_at, WriteRequest.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if req is None:
                return done
            req.status = JobRequestStatus.RUNNING.value
            req.started_at = env.now()
            req_id, kind, target, by = req.id, req.kind, req.target_id, req.requested_by
            params = dict(req.params) if req.params else None
        status, result, error = JobRequestStatus.SUCCESS, None, None
        try:
            result = _run(env, int(req_id), kind, target, by, params)
            if result.get("status") in ("blocked", "failed", "not_found", "skipped"):
                status, error = (
                    JobRequestStatus.FAILED,
                    str(result.get("detail") or result.get("status")),
                )
        except Exception as exc:
            log.exception("write_request.failed", request_id=req_id, kind=kind)
            status, error = JobRequestStatus.FAILED, f"{type(exc).__name__}: {exc}"[:2000]
        with env.sessions() as s, s.begin():
            s.execute(
                update(WriteRequest)
                .where(WriteRequest.id == req_id)
                .values(status=status.value, finished_at=env.now(), result=result, error=error)
            )
        log.info("write_request.done", request_id=req_id, kind=kind, status=status.value)
        done += 1
    return done
