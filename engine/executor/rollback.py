"""Rollback of an applied change (plan §15 "Rollback", §16.2/§16.3 action ``worse``).

1. The original must be ``applied`` with a non-empty ``rollback_payload``.
2. Write gate (D-28) and the rollback guard (``guardrails.check.rollback_checks``: steps 1–4,
   plus 9 when the undo increases spend).
3. Read the current platform state. It must equal the original ``after`` on the tracked
   fields; otherwise (someone changed it since) nothing is written and a
   ``rollback_with_state_drift`` proposal is created (approval required). Approving that
   proposal runs this again with ``force=True``.
4. The undo goes through ``executor.apply.perform`` (write-ahead row with ``rollback_of``,
   validate-only, read-back) with idempotency key ``rollback:<change_id>``.
5. On success the original becomes ``rolled_back``, its open evaluations are closed
   (``action_taken = 'cancelled: rolled back'``) and a ``warn`` alert says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import update
from sqlalchemy.orm import Session

from engine.approvals.service import APPROVAL_TTL
from engine.db.models import ChangeLog, Evaluation, Proposal
from engine.enums import (
    Action,
    ChangeOrigin,
    ChangeStatus,
    EntityType,
    ProposalSource,
    ProposalStatus,
    Risk,
    Severity,
)
from engine.executor.apply import ChangeOutcome, Outcome, WriteEnv, perform, read_created
from engine.executor.clients import WriteUnavailableError
from engine.executor.gate import write_gate
from engine.executor.plan import OpKind, WriteOp, matches, money_str, public, tracked
from engine.guardrails.check import (
    Block,
    ProposalView,
    check,
    derived_direction,
    result_json,
    rollback_checks,
)
from engine.guardrails.state import EntityMissingError, StateBuilder
from engine.ingest.alerts import raise_alert
from engine.proposals import ProposalDraft, insert_proposal

log = structlog.get_logger(__name__)

ROLLBACKABLE_ORIGINS = frozenset(
    {
        ChangeOrigin.PROPOSAL.value,
        ChangeOrigin.EMERGENCY.value,
        ChangeOrigin.PAUSE_ALL.value,
        ChangeOrigin.RESTORE.value,
    }
)


@dataclass
class RollbackResult:
    status: str  # applied | noop | drift_proposal | blocked | failed | unknown | skipped
    detail: str | None = None
    outcome: ChangeOutcome | None = None
    drift_proposal_id: int | None = None
    change_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("applied", "noop")


class _Skip(Exception):
    pass


def undo_op(orig: ChangeLog) -> WriteOp:
    """The write that restores ``orig.rollback_payload`` (raises _Skip when impossible)."""
    intended = dict(orig.intended or {})
    if "_op" not in intended:
        raise _Skip("change has no recorded write target")
    op = WriteOp.from_change(intended, EntityType(orig.entity_type), int(orig.entity_id))
    rb = dict(orig.rollback_payload or {})
    kind = rb.get("kind")
    if not kind:
        raise _Skip("nothing to undo (empty rollback_payload)")
    if kind == OpKind.SET_STATUS.value:
        if op.kind is OpKind.CREATE_AD:
            ext = rb.get("external_id")
            if not ext:
                raise _Skip("created ad id unknown")
            return WriteOp(
                OpKind.SET_STATUS,
                Action.PAUSE,
                op.platform,
                EntityType.AD,
                str(ext),
                op.log_entity_type,
                op.log_entity_id,
                {"status": rb["status"]},
                {"status": rb["status"]},
            )
        action = Action.PAUSE if rb["status"] == "paused" else Action.ENABLE
        return WriteOp(
            OpKind.SET_STATUS,
            action,
            op.platform,
            op.entity_type,
            op.external_id,
            op.log_entity_type,
            op.log_entity_id,
            {"status": rb["status"]},
            {"status": rb["status"]},
        )
    if kind == OpKind.SET_DAILY_BUDGET.value:
        amount = rb.get("daily_budget")
        if amount is None:
            raise _Skip("previous daily budget unknown")
        amount = money_str(Decimal(str(amount)))
        return WriteOp(
            OpKind.SET_DAILY_BUDGET,
            Action.BUDGET_CHANGE,
            op.platform,
            op.entity_type,
            op.external_id,
            op.log_entity_type,
            op.log_entity_id,
            {"daily_budget": amount},
            {"amount": amount},
        )
    if kind == OpKind.REMOVE_NEGATIVE.value:
        crit = rb.get("criterion_external_id")
        if not crit:
            raise _Skip("negative keyword id unknown")
        return WriteOp(
            OpKind.REMOVE_NEGATIVE,
            Action.REMOVE_NEGATIVE,
            op.platform,
            EntityType.KEYWORD,
            str(crit),
            op.log_entity_type,
            op.log_entity_id,
            {"status": "removed"},
            {"ad_group_external_id": rb["ad_group_external_id"]},
        )
    raise _Skip(f"unknown rollback kind {kind!r}")


def expected_now(orig: ChangeLog, undo: WriteOp) -> dict[str, Any]:
    """Tracked fields the target must still show for the rollback to be safe (= ``after``)."""
    after = public(orig.after)
    after.pop("external_id", None)
    if undo.kind is OpKind.REMOVE_NEGATIVE or (
        undo.kind is OpKind.SET_STATUS and undo.entity_type is EntityType.AD and "ad" in after
    ):
        return {"status": after.get("status")}
    return {k: v for k, v in after.items() if k in undo.intended}


def current_state(reader: Any, undo: WriteOp) -> dict[str, Any]:
    if undo.kind is OpKind.REMOVE_NEGATIVE:
        st = reader.read_state(EntityType.KEYWORD, undo.external_id)
        return {"status": st.status.value}
    return tracked(reader.read_state(undo.entity_type, undo.external_id))


def guard_view(orig: ChangeLog, undo: WriteOp, current_budget: Decimal | None) -> ProposalView:
    params: dict[str, Any] = {}
    if undo.kind is OpKind.SET_DAILY_BUDGET and current_budget:
        target = Decimal(undo.args["amount"])
        params["change_pct"] = str((target - current_budget) / current_budget * 100)
    action = undo.action
    return ProposalView(
        id=None,
        source=ProposalSource.EVALUATOR,
        rule_key=None,
        entity_type=EntityType(orig.entity_type),
        entity_id=int(orig.entity_id),
        action=action,
        params=params,
        risk=Risk.LOW,
        spend_direction=derived_direction(action, params),
    )


def _drift_proposal(  # noqa: PLR0917
    s: Session,
    env: WriteEnv,
    orig: ChangeLog,
    expected: dict[str, Any],
    current: dict[str, Any],
    requested_by: str,
) -> int | None:
    now = env.now()
    params = {
        "change_id": orig.id,
        "expected": expected,
        "current": current,
        "requested_by": requested_by,
    }
    pid = insert_proposal(
        s,
        env.client_id,
        ProposalDraft(
            as_of=now.date(),
            source=ProposalSource.EVALUATOR,
            rule_key=f"rollback:{orig.id}",
            entity_type=EntityType(orig.entity_type),
            entity_id=int(orig.entity_id),
            action=Action.ROLLBACK_WITH_STATE_DRIFT,
            risk=Risk.HIGH,
            reason=f"Rollback of change #{orig.id} not applied: platform state {current} no "
            f"longer equals the change's result {expected} (changed since). Approve to force "
            "the rollback anyway.",
            params=params,
        ),
    )
    if pid is None:
        return None
    p = s.get(Proposal, pid)
    assert p is not None
    try:
        b = StateBuilder(s, env.settings, now)
        from engine.guardrails.state import proposal_view

        result = check(proposal_view(p), b.state(EntityType(p.entity_type), int(p.entity_id)))
        p.guardrail_result = result_json(result, approved=False, mode=b.mode, checked_at=now)
        blocked = isinstance(result, Block)
    except EntityMissingError as exc:
        p.guardrail_result = {
            "result": "block",
            "reason": "unsupported_scope",
            "detail": {"why": str(exc)},
        }
        blocked = True
    if blocked:
        p.status = ProposalStatus.BLOCKED.value
    else:
        p.status = ProposalStatus.AWAITING_APPROVAL.value  # approval required (§15)
        p.expires_at = now + APPROVAL_TTL
    return pid


def rollback_change(  # noqa: PLR0911
    env: WriteEnv,
    change_id: int,
    *,
    requested_by: str,
    force: bool = False,
    proposal_id: int | None = None,
) -> RollbackResult:
    now = env.now()
    with env.sessions() as s, s.begin():
        orig = s.get(ChangeLog, change_id)
        if orig is None or orig.client_id != env.client_id:
            return RollbackResult("skipped", f"change {change_id} not found")
        if orig.status != ChangeStatus.APPLIED.value:
            return RollbackResult("skipped", f"change {change_id} is {orig.status}, not applied")
        if orig.origin not in ROLLBACKABLE_ORIGINS:
            return RollbackResult("skipped", f"a {orig.origin} change is not rolled back")
        try:
            undo = undo_op(orig)
        except _Skip as exc:
            return RollbackResult("skipped", str(exc))
        expected = expected_now(orig, undo)
        try:
            builder = StateBuilder(s, env.settings, now)
            state = builder.state(EntityType(orig.entity_type), int(orig.entity_id))
        except EntityMissingError as exc:
            return RollbackResult("blocked", str(exc))
        view = guard_view(orig, undo, state.entity.current_daily_budget)
        gate = write_gate(s, env.settings, reducing=view.spend_direction.value == "reducing")
        if not gate.open:
            return RollbackResult("blocked", gate.reason)
        new_budget = Decimal(undo.args["amount"]) if "amount" in undo.args else None
        if undo.kind is OpKind.SET_DAILY_BUDGET and not view.params:
            return RollbackResult("blocked", "current budget unknown")
        guard = rollback_checks(view, state, new_daily_budget=new_budget)
        if isinstance(guard, Block):
            return RollbackResult("blocked", f"guardrail: {guard.reason.value} {guard.detail}")
    try:
        pc = env.clients.get(undo.platform, conversion_action=None)
    except WriteUnavailableError as exc:
        return RollbackResult("blocked", str(exc))
    try:
        current = (
            {"status": read_created(pc.reader, _created_view(orig), undo.external_id)["status"]}
            if undo.entity_type is EntityType.AD and "ad" in public(orig.after)
            else current_state(pc.reader, undo)
        )
    except Exception as exc:
        return RollbackResult("failed", f"could not read current state: {exc}")
    if not force and not matches(current, expected):
        with env.sessions() as s, s.begin():
            orig2 = s.get(ChangeLog, change_id)
            assert orig2 is not None
            pid = _drift_proposal(s, env, orig2, expected, current, requested_by)
            raise_alert(
                s,
                env.client_id,
                dedupe_key=f"rollback_drift:{change_id}",
                severity=Severity.WARN,
                kind="rollback_drift",
                message=f"rollback of change #{change_id} not applied: state drifted "
                f"({current} != {expected}); proposal #{pid} needs approval",
            )
        return RollbackResult("drift_proposal", "state drifted", drift_proposal_id=pid)

    out = perform(
        env,
        undo,
        origin=ChangeOrigin.ROLLBACK,
        key=f"rollback:{change_id}",
        applied_by=requested_by,
        proposal_id=proposal_id,
        rollback_of=change_id,
        payload={},
        clients=pc,
    )
    if out.outcome in (Outcome.APPLIED, Outcome.NOOP):
        with env.sessions() as s, s.begin():
            s.execute(
                update(ChangeLog)
                .where(ChangeLog.id == change_id, ChangeLog.status == ChangeStatus.APPLIED.value)
                .values(status=ChangeStatus.ROLLED_BACK.value)
            )
            s.execute(
                update(Evaluation)
                .where(Evaluation.change_id == change_id, Evaluation.done_at.is_(None))
                .values(
                    done_at=env.now(),
                    action_taken=f"cancelled: rolled back by change #{out.change_id}",
                )
            )
            raise_alert(
                s,
                env.client_id,
                dedupe_key=f"rollback_executed:{change_id}",
                severity=Severity.WARN,
                kind="rollback_executed",
                entity_type=EntityType(undo.log_entity_type),
                entity_id=undo.log_entity_id,
                message=f"rollback executed: change #{change_id} undone by change "
                f"#{out.change_id} ({undo.kind.value} → {undo.intended}); by {requested_by}",
            )
        return RollbackResult(out.outcome.value, None, out, change_id=out.change_id)
    status = {
        Outcome.UNKNOWN: "unknown",
        Outcome.DUPLICATE: "skipped",
        Outcome.UNAVAILABLE: "blocked",
    }.get(out.outcome, "failed")
    return RollbackResult(status, out.error, out, change_id=out.change_id)


def _created_view(orig: ChangeLog) -> WriteOp:
    return WriteOp.from_change(
        dict(orig.intended or {}), EntityType(orig.entity_type), int(orig.entity_id)
    )
