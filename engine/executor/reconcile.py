"""Reconciler (plan §15 "Startup reconciler"): runs at engine start and hourly.

For each ``change_log`` row still ``applying`` after 5 minutes (the executor died or the
platform answer was ambiguous) re-read the platform:

- state equals ``intended`` → ``applied`` (+ ``entity_history``; the proposal is finalised,
  which creates its evaluations),
- state equals ``before``   → ``failed`` (nothing happened),
- anything else             → ``verify_failed`` + ``critical`` alert.

Created objects: a negative keyword is looked up by ad group + text + match type. A created
ad cannot be identified reliably → ``verify_failed`` + alert (it was created PAUSED, so a
possible duplicate never spends).

Proposals stuck in ``executing`` with no unresolved write (crash between the transition and
the first write-ahead row) are finalised as ``failed``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import select, update

from engine.db.models import ChangeLog, Proposal
from engine.enums import (
    ChangeOrigin,
    ChangeStatus,
    EntityType,
    HistorySource,
    ProposalStatus,
    Severity,
)
from engine.executor.apply import WriteEnv, _alert, find_negative, mark_failed, mark_result
from engine.executor.clients import WriteUnavailableError
from engine.executor.execute import finalize_proposal
from engine.executor.plan import OpKind, WriteOp, matches, public, tracked

log = structlog.get_logger(__name__)

STALE_APPLYING = dt.timedelta(minutes=5)


def _platform_state(reader: Any, op: WriteOp) -> tuple[dict[str, Any] | None, str | None]:
    if op.kind is OpKind.ADD_NEGATIVE:
        crit = find_negative(reader, op)
        if crit is None:
            return {"negative": None}, None
        return {"negative": True, "status": "active", "external_id": crit}, crit
    if op.kind is OpKind.CREATE_AD:
        return None, None
    if op.kind is OpKind.REMOVE_NEGATIVE:
        return {"status": reader.read_state(EntityType.KEYWORD, op.external_id).status.value}, None
    return tracked(reader.read_state(op.entity_type, op.external_id)), None


def resolve_row(env: WriteEnv, change_id: int) -> str:
    with env.sessions() as s:
        row = s.get(ChangeLog, change_id)
        if row is None or row.status != ChangeStatus.APPLYING.value:
            return "already_resolved"
        op = WriteOp.from_change(
            dict(row.intended or {}), EntityType(row.entity_type), int(row.entity_id)
        )
        before = public(row.before)
        source = (
            HistorySource.EMERGENCY
            if row.origin in (ChangeOrigin.EMERGENCY.value, ChangeOrigin.PAUSE_ALL.value)
            else HistorySource.EXECUTOR
        )
    try:
        pc = env.clients.get(op.platform, conversion_action=None)
    except WriteUnavailableError as exc:
        log.warning("reconcile.unavailable", change_id=change_id, error=str(exc))
        return "unavailable"
    try:
        state, created = _platform_state(pc.reader, op)
    except Exception as exc:
        log.warning("reconcile.read_failed", change_id=change_id, error=str(exc))
        return "read_failed"
    if state is not None and matches(state, op.intended):
        out = mark_result(env, change_id, op, state, history_source=source, created=created)
        return out.value
    if state is not None and matches(state, before) and public(before):
        mark_failed(env, change_id, op, "reconciler: platform state unchanged (write not applied)")
        return ChangeStatus.FAILED.value
    with env.sessions() as s, s.begin():
        s.execute(
            update(ChangeLog)
            .where(ChangeLog.id == change_id, ChangeLog.status == ChangeStatus.APPLYING.value)
            .values(
                status=ChangeStatus.VERIFY_FAILED.value,
                after=state,
                error="reconciler: state matches neither intended nor before",
            )
        )
        what = (
            "a created ad cannot be identified after an interrupted write; check the ad group "
            "for a duplicate PAUSED ad"
            if op.kind is OpKind.CREATE_AD
            else f"state {state} matches neither intended {op.intended} nor before {before}"
        )
        _alert(
            s,
            env.client_id,
            key=f"reconcile_drift:{change_id}",
            severity=Severity.CRITICAL,
            kind="reconcile_drift",
            message=f"reconciler: change #{change_id} {op.kind.value} on "
            f"{op.entity_type.value} {op.external_id}: {what}",
            op=op,
        )
    return ChangeStatus.VERIFY_FAILED.value


def reconcile(env: WriteEnv, *, stale: dt.timedelta = STALE_APPLYING) -> dict[str, int]:
    cutoff = env.now() - stale
    with env.sessions() as s:
        rows = s.execute(
            select(ChangeLog.id, ChangeLog.proposal_id)
            .where(
                ChangeLog.client_id == env.client_id,
                ChangeLog.status == ChangeStatus.APPLYING.value,
                ChangeLog.started_at < cutoff,
            )
            .order_by(ChangeLog.id)
        ).all()
    counts: dict[str, int] = {}
    for change_id, _pid in rows:
        r = resolve_row(env, int(change_id))
        counts[r] = counts.get(r, 0) + 1
    with env.sessions() as s:
        executing = s.execute(
            select(Proposal.id, Proposal.guardrail_result).where(
                Proposal.client_id == env.client_id,
                Proposal.status == ProposalStatus.EXECUTING.value,
            )
        ).all()
    for pid, gr in executing:
        checked = (gr or {}).get("checked_at")
        if checked and dt.datetime.fromisoformat(checked) > cutoff:
            continue  # an execution may be in progress right now
        status = finalize_proposal(env, int(pid))
        key = f"proposal_{status}"
        counts[key] = counts.get(key, 0) + 1
    if counts:
        log.info("reconcile.done", **counts)
    return dict(sorted(counts.items()))
