"""The write path: one platform write with a write-ahead ``change_log`` row (plan §15 3–6).

``perform`` is shared by every writer (approved proposals, emergency stop, pause-all,
rollback, restore). Sequence:

1. Read ``before`` from the platform (read credential).
2. Write-ahead: insert ``change_log`` (``status = applying``, ``idempotency_key``) and COMMIT.
   ``ON CONFLICT (client_id, idempotency_key) DO NOTHING`` — a key already present means this
   write was attempted before: nothing is sent again (D-27).
3. Validate-only call (skipped on the emergency path). Failure → ``failed`` + ``warn``.
4. Apply. A definite rejection → ``failed``; an ambiguous outcome (timeout, 5xx, unknown
   exception) leaves the row ``applying`` for the reconciler. Never retried blindly here.
5. Read back → ``after``. Match → ``applied`` + ``entity_history`` (+ entity row); no match
   → ``verify_failed`` + ``critical`` alert, no automatic retry.

If ``before`` already equals ``intended`` the row is recorded ``applied`` as a no-op without
calling the platform (``validate_result = {"noop": true}``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from engine.config import Settings
from engine.connectors.base import AdDraft, EntityStatus, WriteConnector, WriteResult
from engine.connectors.write_common import PlatformWriteError
from engine.db.models import ChangeLog, EntityHistory
from engine.enums import ChangeOrigin, ChangeStatus, EntityType, HistorySource, Severity
from engine.executor.clients import (
    PlatformClients,
    RealWriteClients,
    StateReader,
    WriteClientProvider,
    WriteUnavailableError,
)
from engine.executor.plan import (
    _ENTITY_MODELS,
    OpKind,
    WriteOp,
    matches,
    public,
    rollback_payload,
    tracked,
)
from engine.ingest.alerts import raise_alert
from engine.jobs.runner import JobContext

log = structlog.get_logger(__name__)


@dataclass
class WriteEnv:
    settings: Settings
    sessions: sessionmaker[Session]
    clients: WriteClientProvider
    clock: Callable[[], dt.datetime]

    @property
    def client_id(self) -> str:
        return self.settings.client_id

    def now(self) -> dt.datetime:
        return self.clock().astimezone(dt.UTC)


def env_from_ctx(ctx: JobContext) -> WriteEnv:
    provider = getattr(ctx, "write_clients", None) or RealWriteClients(ctx.settings, ctx.connectors)
    return WriteEnv(ctx.settings, ctx.sessions, provider, ctx.clock)


class Outcome(StrEnum):
    APPLIED = "applied"
    NOOP = "noop"  # already in the intended state; recorded applied, nothing sent
    FAILED = "failed"
    VERIFY_FAILED = "verify_failed"
    UNKNOWN = "unknown"  # row left 'applying' → reconciler
    DUPLICATE = "duplicate"  # idempotency key already used; nothing sent
    UNAVAILABLE = "unavailable"  # no write path (credentials); no row written
    PREFLIGHT_FAILED = "preflight_failed"  # could not read 'before'; no row written


@dataclass(frozen=True, slots=True)
class ChangeOutcome:
    outcome: Outcome
    change_id: int | None = None
    error: str | None = None
    created_external_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (Outcome.APPLIED, Outcome.NOOP)


# ----------------------------------------------------------------------------- platform calls


def call_writer(writer: WriteConnector, op: WriteOp, *, validate_only: bool) -> WriteResult:
    a = op.args
    if op.kind is OpKind.SET_STATUS:
        return writer.set_status(
            op.entity_type, op.external_id, EntityStatus(a["status"]), validate_only=validate_only
        )
    if op.kind is OpKind.SET_DAILY_BUDGET:
        return writer.set_daily_budget(
            op.entity_type, op.external_id, Decimal(str(a["amount"])), validate_only=validate_only
        )
    if op.kind is OpKind.ADD_NEGATIVE:
        return writer.add_negative_keyword(
            a["ad_group_external_id"], a["text"], a["match_type"], validate_only=validate_only
        )
    if op.kind is OpKind.REMOVE_NEGATIVE:
        return writer.remove_negative_keyword(
            a["ad_group_external_id"], op.external_id, validate_only=validate_only
        )
    extra: dict[str, Any] = {"template_ad_id": a["template_ad_id"]}
    for k in ("headlines", "descriptions"):
        if a.get(k):
            extra[k] = list(a[k])
    draft = AdDraft(
        op.platform, a["ad_group_external_id"], a.get("headline"), a.get("body"), None, extra
    )
    return writer.create_ad(draft, validate_only=validate_only)


def find_negative(reader: StateReader, op: WriteOp) -> str | None:
    """An active negative with the same text/match type already in the ad group, if any."""
    a = op.args
    for k in reader.list_keywords():
        if (
            k.is_negative
            and k.ad_group_external_id == a["ad_group_external_id"]
            and k.text.strip().lower() == str(a["text"]).strip().lower()
            and (k.match_type or "").lower() == str(a["match_type"]).lower()
            and k.status is EntityStatus.ACTIVE
        ):
            return k.external_id
    return None


def read_before(reader: StateReader, op: WriteOp) -> dict[str, Any]:
    if op.kind is OpKind.ADD_NEGATIVE:
        existing = find_negative(reader, op)
        if existing is None:
            return {"negative": None}
        return {"negative": True, "status": EntityStatus.ACTIVE.value, "external_id": existing}
    if op.kind is OpKind.CREATE_AD:
        return {"ad": None}
    return tracked(reader.read_state(op.entity_type, op.external_id))


def read_created(reader: StateReader, op: WriteOp, created: str) -> dict[str, Any]:
    if op.kind is OpKind.ADD_NEGATIVE:
        st = reader.read_state(EntityType.KEYWORD, created)
        return {"negative": True, "status": st.status.value, "external_id": created}
    st = reader.read_state(EntityType.AD, created)
    return {"ad": True, "status": st.status.value, "external_id": created}


def read_after(reader: StateReader, op: WriteOp, created: str | None) -> dict[str, Any]:
    if op.kind in (OpKind.ADD_NEGATIVE, OpKind.CREATE_AD):
        if not created:
            raise LookupError("platform returned no id for the created object")
        return read_created(reader, op, created)
    return tracked(reader.read_state(op.entity_type, op.external_id))


# ----------------------------------------------------------------------------- DB updates


def append_history(
    s: Session,
    client_id: str,
    op: WriteOp,
    after: dict[str, Any],
    *,
    source: HistorySource,
    now: dt.datetime,
) -> None:
    """``entity_history`` row + entity row update for writes on a synced entity (§15 step 6).
    Creations are picked up by the next sync."""
    if op.kind not in (OpKind.SET_STATUS, OpKind.SET_DAILY_BUDGET):
        return
    if op.entity_type is not op.log_entity_type or op.entity_type not in _ENTITY_MODELS:
        return
    model = _ENTITY_MODELS[op.entity_type]
    row = s.get(model, op.log_entity_id)
    if row is None:
        return
    latest = s.scalars(
        select(EntityHistory)
        .where(
            EntityHistory.client_id == client_id,
            EntityHistory.entity_type == op.entity_type,
            EntityHistory.entity_id == op.log_entity_id,
        )
        .order_by(EntityHistory.observed_at.desc(), EntityHistory.id.desc())
        .limit(1)
    ).first()
    status = after.get("status") or (latest.status if latest else row.status)
    budget = after.get("daily_budget")
    has_budget = hasattr(row, "daily_budget")
    daily_budget = (
        Decimal(budget)
        if budget is not None
        else (latest.daily_budget if latest else (row.daily_budget if has_budget else None))
    )
    s.add(
        EntityHistory(
            client_id=client_id,
            entity_type=op.entity_type,
            entity_id=op.log_entity_id,
            observed_at=now,
            status=status,
            daily_budget=daily_budget if has_budget else None,
            learning=latest.learning if latest else getattr(row, "learning", None),
            source=source.value,
        )
    )
    row.status = status
    if has_budget and budget is not None:
        row.daily_budget = Decimal(budget)


def _alert(
    s: Session,
    client_id: str,
    *,
    key: str,
    severity: Severity,
    kind: str,
    message: str,
    op: WriteOp | None = None,
) -> None:
    raise_alert(
        s,
        client_id,
        dedupe_key=key,
        severity=severity,
        kind=kind,
        message=message,
        entity_type=op.log_entity_type if op else None,
        entity_id=op.log_entity_id if op else None,
    )


def mark_failed(
    env: WriteEnv,
    change_id: int,
    op: WriteOp,
    error: str,
    *,
    validate_result: dict[str, Any] | None = None,
) -> None:
    with env.sessions() as s, s.begin():
        s.execute(
            update(ChangeLog)
            .where(ChangeLog.id == change_id)
            .values(
                status=ChangeStatus.FAILED.value,
                error=error[:4000],
                validate_result=validate_result,
            )
        )
        _alert(
            s,
            env.client_id,
            key=f"write_failed:{change_id}",
            severity=Severity.WARN,
            kind="write_failed",
            message=f"{op.platform.value} {op.kind.value} on {op.entity_type.value} "
            f"{op.external_id} failed (change #{change_id}): {error}"[:2000],
            op=op,
        )


def mark_unknown(env: WriteEnv, change_id: int, error: str) -> None:
    with env.sessions() as s, s.begin():
        s.execute(
            update(ChangeLog)
            .where(ChangeLog.id == change_id)
            .values(error=f"outcome unknown, reconciler will resolve: {error}"[:4000])
        )


def mark_result(
    env: WriteEnv,
    change_id: int,
    op: WriteOp,
    after: dict[str, Any],
    *,
    history_source: HistorySource,
    created: str | None = None,
    noop: bool = False,
) -> Outcome:
    """Record the read-back: ``applied`` (+ history) or ``verify_failed`` (+ critical alert)."""
    now = env.now()
    with env.sessions() as s, s.begin():
        row = s.get(ChangeLog, change_id, with_for_update=True)
        assert row is not None
        if row.status != ChangeStatus.APPLYING.value:
            return Outcome.DUPLICATE  # resolved concurrently (reconciler)
        row.after = after
        if matches(after, op.intended):
            row.status = ChangeStatus.APPLIED.value
            row.applied_at = now
            row.error = None
            if noop:
                row.validate_result = {"noop": True, "note": "already in the intended state"}
            rb = dict(row.rollback_payload or {})
            if created and op.kind is OpKind.ADD_NEGATIVE:
                rb["criterion_external_id"] = created
            elif created and op.kind is OpKind.CREATE_AD:
                rb["external_id"] = created
            elif noop and op.kind is OpKind.ADD_NEGATIVE and after.get("external_id"):
                rb = {}  # nothing we created: nothing to undo
            row.rollback_payload = rb
            append_history(s, env.client_id, op, after, source=history_source, now=now)
            return Outcome.NOOP if noop else Outcome.APPLIED
        row.status = ChangeStatus.VERIFY_FAILED.value
        row.error = f"read-back {public(after)} != intended {op.intended}"
        _alert(
            s,
            env.client_id,
            key=f"verify_failed:{change_id}",
            severity=Severity.CRITICAL,
            kind="verify_failed",
            message=f"{op.platform.value} {op.kind.value} on {op.entity_type.value} "
            f"{op.external_id}: read-back {public(after)} does not match intended "
            f"{op.intended} (change #{change_id}); not retried",
            op=op,
        )
        return Outcome.VERIFY_FAILED


# ----------------------------------------------------------------------------- perform


def perform(  # noqa: PLR0911 (the §15 sequence, one exit per outcome)
    env: WriteEnv,
    op: WriteOp,
    *,
    origin: ChangeOrigin,
    key: str,
    applied_by: str,
    proposal_id: int | None = None,
    validate: bool = True,
    history_source: HistorySource = HistorySource.EXECUTOR,
    rollback_of: int | None = None,
    payload: dict[str, Any] | None = None,
    conversion_action: str | None = None,
    clients: PlatformClients | None = None,
) -> ChangeOutcome:
    try:
        pc = clients or env.clients.get(op.platform, conversion_action=conversion_action)
    except WriteUnavailableError as exc:
        return ChangeOutcome(Outcome.UNAVAILABLE, error=str(exc))

    # 1. before (platform)
    try:
        before = read_before(pc.reader, op)
    except Exception as exc:
        return ChangeOutcome(Outcome.PREFLIGHT_FAILED, error=f"read before failed: {exc}")

    # 2. write-ahead + commit
    with env.sessions() as s, s.begin():
        change_id = s.execute(
            pg_insert(ChangeLog)
            .values(
                client_id=env.client_id,
                proposal_id=proposal_id,
                platform=op.platform,
                entity_type=op.log_entity_type,
                entity_id=op.log_entity_id,
                action=op.action.value,
                status=ChangeStatus.APPLYING.value,
                before=before,
                intended=op.intended_json(),
                rollback_payload=payload if payload is not None else rollback_payload(op, before),
                started_at=env.now(),
                applied_by=applied_by,
                rollback_of=rollback_of,
                idempotency_key=key,
                origin=origin.value,
            )
            .on_conflict_do_nothing(index_elements=["client_id", "idempotency_key"])
            .returning(ChangeLog.id)
        ).scalar()
        if change_id is None:
            existing = s.execute(
                select(ChangeLog.id, ChangeLog.status).where(
                    ChangeLog.client_id == env.client_id, ChangeLog.idempotency_key == key
                )
            ).one()
            log.warning("executor.duplicate", key=key, change_id=existing.id)
            return ChangeOutcome(
                Outcome.DUPLICATE, int(existing.id), f"already attempted ({existing.status})"
            )
    bound = log.bind(change_id=change_id, op=op.kind.value, platform=op.platform.value)

    if matches(before, op.intended):
        out = mark_result(env, change_id, op, before, history_source=history_source, noop=True)
        return ChangeOutcome(out, change_id)

    # 3. validate-only
    if validate:
        try:
            v = call_writer(pc.writer, op, validate_only=True)
        except PlatformWriteError as exc:
            v = WriteResult(
                ok=False, validate_only=True, error=str(exc), platform_response=exc.as_json()
            )
        if not v.ok:
            err = f"validate-only rejected: {v.error}"
            mark_failed(
                env,
                change_id,
                op,
                err,
                validate_result={"ok": False, "error": v.error, "response": v.platform_response},
            )
            return ChangeOutcome(Outcome.FAILED, change_id, err)

    # 4. apply
    try:
        res = call_writer(pc.writer, op, validate_only=False)
    except PlatformWriteError as exc:
        if exc.applied is False:
            mark_failed(env, change_id, op, str(exc))
            return ChangeOutcome(Outcome.FAILED, change_id, str(exc))
        bound.warning("executor.outcome_unknown", error=str(exc))
        mark_unknown(env, change_id, str(exc))
        return ChangeOutcome(Outcome.UNKNOWN, change_id, str(exc))
    except Exception as exc:  # the request may have been sent: never assume either way
        bound.exception("executor.apply_error")
        mark_unknown(env, change_id, f"{type(exc).__name__}: {exc}")
        return ChangeOutcome(Outcome.UNKNOWN, change_id, str(exc))
    if not res.ok:
        mark_failed(env, change_id, op, res.error or "rejected")
        return ChangeOutcome(Outcome.FAILED, change_id, res.error)

    # 5. read back
    try:
        after = read_after(pc.reader, op, res.created_external_id)
    except Exception as exc:
        mark_unknown(env, change_id, f"read-back failed: {exc}")
        return ChangeOutcome(Outcome.UNKNOWN, change_id, str(exc), res.created_external_id)
    out = mark_result(
        env,
        change_id,
        op,
        after,
        history_source=history_source,
        created=res.created_external_id,
    )
    bound.info("executor.write_done", outcome=out.value)
    return ChangeOutcome(out, change_id, None, res.created_external_id)
