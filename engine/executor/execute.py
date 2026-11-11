"""Execute approved proposals (plan §14.5, §15). Runs as daily step 9 ``execute`` and as the
``execute_approved`` job (every 5 min) so a human approval is applied promptly.

Per proposal (row-locked, ``SKIP LOCKED``):
1. ``status = approved`` only; the write gate (D-28) must be open, else it stays approved.
2. ``approvals.service.recheck_approved`` on fresh state — Block → ``blocked`` + notify.
   The guardrail's clamped ``new_daily_budget`` is what gets written.
3. Plan the ops; check a write path exists for each platform (else it stays approved +
   ``warn``); ``status = executing`` (commit). This transition is the duplicate-execution
   guard: a second executor never sees the proposal as ``approved`` again.
4. ``perform`` each op (write-ahead, validate, apply, read back). Stops at the first op that
   did not end applied.
5. Finalize: all ops applied → ``executed`` + D+3/D+7 ``evaluations``; any unresolved write
   → stays ``executing`` until the reconciler resolves it; otherwise ``failed`` + ``warn``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from engine.approvals.service import DecisionError, recheck_approved
from engine.db.models import ChangeLog, Evaluation, Proposal
from engine.enums import (
    Action,
    ChangeOrigin,
    ChangeStatus,
    EntityType,
    ProposalStatus,
    Severity,
    SpendDirection,
)
from engine.errors import MissingInputError
from engine.evaluation.units import UnitError, evaluation_unit
from engine.executor.apply import ChangeOutcome, Outcome, WriteEnv, perform
from engine.executor.clients import WriteUnavailableError
from engine.executor.gate import write_gate
from engine.executor.plan import PlanError, build_plan, idempotency_key
from engine.guardrails.check import Block
from engine.ingest.alerts import raise_alert
from engine.jobs.common import active_rules

log = structlog.get_logger(__name__)

H3, H7, H14 = 3, 7, 14


@dataclass
class ExecResult:
    proposal_id: int
    status: str  # final proposal status, or 'skipped'
    detail: str | None = None
    changes: list[ChangeOutcome] = field(default_factory=list)


def _conversion_action(s: Session, client_id: str) -> str | None:
    try:
        return active_rules(s, client_id).targets.meta_conversion_action
    except MissingInputError:
        return None


def settle_days(s: Session, client_id: str) -> int:
    return active_rules(s, client_id).stats.settle_days


# ----------------------------------------------------------------------------- evaluations


def create_evaluations(s: Session, client_id: str, p: Proposal, change: ChangeLog) -> int:
    """§15 step 7: D+3 and D+7(+settle_days) rows on the evaluation unit (§16.1)."""
    assert change.applied_at is not None
    try:
        unit_type, unit_id = evaluation_unit(
            s, Action(p.action), EntityType(change.entity_type), int(change.entity_id)
        )
        settle = settle_days(s, client_id)
    except (UnitError, MissingInputError) as exc:
        raise_alert(
            s,
            client_id,
            dedupe_key=f"evaluation_not_scheduled:{change.id}",
            severity=Severity.WARN,
            kind="evaluation_not_scheduled",
            message=f"change #{change.id} applied but not evaluated: {exc}",
        )
        return 0
    rows = [
        {"horizon_days": H3, "due_at": change.applied_at + dt.timedelta(days=H3)},
        {"horizon_days": H7, "due_at": change.applied_at + dt.timedelta(days=H7 + settle)},
    ]
    n = 0
    for r in rows:
        n += len(
            s.execute(
                pg_insert(Evaluation)
                .values(
                    client_id=client_id,
                    change_id=change.id,
                    unit_entity_type=unit_type,
                    unit_entity_id=unit_id,
                    **r,
                )
                .on_conflict_do_nothing(index_elements=["change_id", "horizon_days"])
                .returning(Evaluation.id)
            ).all()
        )
    return n


def is_noop(change: ChangeLog) -> bool:
    return bool((change.validate_result or {}).get("noop"))


# ----------------------------------------------------------------------------- finalize


def finalize_proposal(env: WriteEnv, proposal_id: int, *, reason: str | None = None) -> str:
    """Settle an ``executing`` proposal from its change rows (also used by the reconciler)."""
    with env.sessions() as s, s.begin():
        p = s.get(Proposal, proposal_id, with_for_update=True)
        if p is None or p.status != ProposalStatus.EXECUTING.value:
            return p.status if p is not None else "missing"
        rows = s.scalars(
            select(ChangeLog)
            .where(
                ChangeLog.proposal_id == proposal_id,
                ChangeLog.origin == ChangeOrigin.PROPOSAL.value,
            )
            .order_by(ChangeLog.id)
        ).all()
        if any(r.status == ChangeStatus.APPLYING.value for r in rows):
            return p.status
        planned = int((p.guardrail_result or {}).get("planned_ops", len(rows)))
        applied = [r for r in rows if r.status == ChangeStatus.APPLIED.value]
        if reason is None and rows and len(applied) == len(rows) == planned:
            p.status = ProposalStatus.EXECUTED.value
            first = next((r for r in applied if not is_noop(r)), None)
            if first is not None:
                create_evaluations(s, env.client_id, p, first)
            return p.status
        p.status = ProposalStatus.FAILED.value
        why = (
            reason
            or "; ".join(
                f"#{r.id} {r.status}: {r.error or ''}".strip() for r in rows if r not in applied
            )
            or f"only {len(rows)} of {planned} writes attempted"
        )
        raise_alert(
            s,
            env.client_id,
            dedupe_key=f"proposal_failed:{proposal_id}",
            severity=Severity.WARN,
            kind="execution_failed",
            entity_type=EntityType(p.entity_type),
            entity_id=int(p.entity_id),
            message=f"proposal #{proposal_id} ({p.action}) failed: {why}"[:2000],
        )
        return p.status


# ----------------------------------------------------------------------------- execute


def execute_proposal(env: WriteEnv, proposal_id: int) -> ExecResult:  # noqa: PLR0911, PLR0912
    now = env.now()
    with env.sessions() as s, s.begin():
        p = s.scalars(
            select(Proposal)
            .where(Proposal.id == proposal_id, Proposal.client_id == env.client_id)
            .with_for_update(skip_locked=True)
        ).first()
        if p is None or p.status != ProposalStatus.APPROVED.value:
            return ExecResult(proposal_id, "skipped", "not approved (or locked by another run)")
        reducing = p.spend_direction == SpendDirection.REDUCING.value
        gate = write_gate(s, env.settings, reducing=reducing)
        if not gate.open:
            return ExecResult(proposal_id, "skipped", gate.reason)
        if Action(p.action) is Action.ROLLBACK_WITH_STATE_DRIFT:
            change_id = int((p.params or {}).get("change_id", 0))
            p.status = ProposalStatus.EXECUTING.value
            p.guardrail_result = {**(p.guardrail_result or {}), "planned_ops": 1}
            drift = True
        else:
            drift = False
            try:
                result = recheck_approved(s, env.settings, proposal_id, now)
            except DecisionError as exc:
                return ExecResult(proposal_id, "skipped", exc.detail)
            if isinstance(result, Block):
                return ExecResult(proposal_id, ProposalStatus.BLOCKED.value, result.reason.value)
            try:
                ops = build_plan(s, env.client_id, p, result.new_daily_budget)
            except PlanError as exc:
                p.status = ProposalStatus.FAILED.value
                raise_alert(
                    s,
                    env.client_id,
                    dedupe_key=f"proposal_failed:{proposal_id}",
                    severity=Severity.WARN,
                    kind="execution_failed",
                    message=f"proposal #{proposal_id} ({p.action}) cannot be executed: {exc}",
                )
                return ExecResult(proposal_id, p.status, str(exc))
            conversion = _conversion_action(s, env.client_id)
            try:
                clients = {
                    op.platform: env.clients.get(op.platform, conversion_action=conversion)
                    for op in ops
                }
            except WriteUnavailableError as exc:
                raise_alert(
                    s,
                    env.client_id,
                    dedupe_key="write_unavailable",
                    severity=Severity.WARN,
                    kind="credential_invalid",
                    message=f"approved proposals waiting: {exc}",
                )
                return ExecResult(proposal_id, "skipped", str(exc))
            p.status = ProposalStatus.EXECUTING.value
            p.guardrail_result = {**(p.guardrail_result or {}), "planned_ops": len(ops)}
    if drift:
        from engine.executor.rollback import rollback_change

        rb = rollback_change(
            env,
            change_id,
            requested_by=f"proposal:{proposal_id}",
            force=True,
            proposal_id=proposal_id,
        )
        with env.sessions() as s, s.begin():
            p2 = s.get(Proposal, proposal_id, with_for_update=True)
            assert p2 is not None
            if rb.outcome is not None and rb.outcome.outcome is Outcome.UNKNOWN:
                return ExecResult(proposal_id, p2.status, rb.detail)
            p2.status = ProposalStatus.EXECUTED.value if rb.ok else ProposalStatus.FAILED.value
            return ExecResult(proposal_id, p2.status, rb.detail)

    outcomes: list[ChangeOutcome] = []
    for op in ops:
        out = perform(
            env,
            op,
            origin=ChangeOrigin.PROPOSAL,
            key=idempotency_key(proposal_id, op),
            applied_by="executor",
            proposal_id=proposal_id,
            clients=clients[op.platform],
        )
        outcomes.append(out)
        if not out.ok:
            break
    reason: str | None = None
    last = outcomes[-1]
    if last.outcome in (Outcome.PREFLIGHT_FAILED, Outcome.UNAVAILABLE):
        reason = last.error
    status = finalize_proposal(env, proposal_id, reason=reason)
    log.info(
        "executor.proposal",
        proposal_id=proposal_id,
        status=status,
        outcomes=[o.outcome.value for o in outcomes],
    )
    return ExecResult(proposal_id, status, last.error, outcomes)


def execute_approved(env: WriteEnv, *, limit: int = 50) -> dict[str, Any]:
    with env.sessions() as s:
        ids = s.scalars(
            select(Proposal.id)
            .where(
                Proposal.client_id == env.client_id,
                Proposal.status == ProposalStatus.APPROVED.value,
            )
            .order_by(Proposal.id)
            .limit(limit)
        ).all()
    counts: dict[str, int] = {}
    skipped: list[str] = []
    for pid in ids:
        r = execute_proposal(env, int(pid))
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status == "skipped" and r.detail:
            skipped.append(r.detail)
    return {"counts": dict(sorted(counts.items())), "skipped_reasons": sorted(set(skipped))}
