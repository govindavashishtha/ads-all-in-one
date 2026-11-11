"""Approval service — the single source of truth for approval logic (plan §14).

- ``request_approvals``: for every ``awaiting_approval`` proposal without an approvals row,
  create one and queue an approval-request notification (§14 steps 1–2). Links point at the
  dashboard page and never act (§14.3).
- ``decide``: record an approve/reject decision from an authenticated POST (dashboard / CLI via
  the API), with audit log (§14 steps 3–4, 7).
- ``recheck_approved``: §14 step 5 — re-run the guardrail with ``approved=True`` and fresh
  state; Block → ``blocked`` + notify. The executor (Phase 3) must call this immediately
  before applying; the daily ``approvals`` step also calls it for still-unexecuted approvals.
- ``expire_and_remind``: remind once at 12 h before expiry, expire at ``expires_at`` (24 h);
  expired proposals are never executed (§14.6).
"""

from __future__ import annotations

import datetime as dt
from html import escape
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from engine.config import Settings
from engine.db.models import Approval, AuditLog, Proposal
from engine.enums import NotificationKind, ProposalStatus
from engine.guardrails.check import Block, GuardrailResult, check, result_json
from engine.guardrails.state import EntityMissingError, StateBuilder, proposal_view
from engine.notify.adapters import configured_channel_names
from engine.notify.outbox import enqueue

APPROVAL_TTL = dt.timedelta(hours=24)  # §14.1
REMIND_BEFORE_EXPIRY = dt.timedelta(hours=12)  # §14.6: reminded once at 12 h

NEXT_STEP_APPROVED = (
    "recorded; the executor re-runs the guardrail with fresh state before applying "
    "(executor ships in Phase 3 — nothing is sent to an ad platform yet)"
)
NEXT_STEP_REJECTED = "recorded; rejected proposals are never executed"

Decision = Literal["approved", "rejected"]


class DecisionError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def record_audit(
    session: Session,
    *,
    client_id: str,
    actor: str,
    action: str,
    target: str | None,
    payload: dict[str, Any] | None,
    ip: str | None,
    at: dt.datetime,
) -> None:
    session.add(
        AuditLog(
            client_id=client_id,
            actor=actor,
            action=action,
            target=target,
            payload=payload,
            ip=ip,
            at=at,
        )
    )


def latest_approval(session: Session, proposal_id: int) -> Approval | None:
    return session.scalars(
        select(Approval)
        .where(Approval.proposal_id == proposal_id)
        .order_by(Approval.requested_at.desc(), Approval.id.desc())
        .limit(1)
    ).first()


def decide(
    session: Session,
    *,
    client_id: str,
    proposal_id: int,
    decision: Decision,
    actor: str,
    role: str,
    via: str,
    comment: str | None,
    now: dt.datetime,
    ip: str | None,
) -> Proposal:
    """Record one decision (caller checked the role and commits). Raises DecisionError 404/409.

    Approve sets ``approved``; the guardrail is re-run with fresh state by
    ``recheck_approved`` before anything is applied (§14.5).
    """
    proposal = session.scalars(
        select(Proposal)
        .where(Proposal.id == proposal_id, Proposal.client_id == client_id)
        .with_for_update()
    ).one_or_none()
    if proposal is None:
        raise DecisionError(404, f"proposal {proposal_id} not found")
    if proposal.status != ProposalStatus.AWAITING_APPROVAL.value:
        raise DecisionError(
            409, f"proposal {proposal_id} is {proposal.status}, not awaiting_approval"
        )
    if proposal.expires_at is not None and proposal.expires_at <= now:
        raise DecisionError(409, f"proposal {proposal_id} expired at {proposal.expires_at}")

    approval = latest_approval(session, proposal_id)
    if approval is None or approval.decided_at is not None:
        approval = Approval(client_id=client_id, proposal_id=proposal_id, channel=via)
        session.add(approval)
    approval.decided_at = now
    approval.decision = decision
    approval.decided_by = actor
    approval.comment = comment
    proposal.status = (
        ProposalStatus.APPROVED.value if decision == "approved" else ProposalStatus.REJECTED.value
    )
    record_audit(
        session,
        client_id=client_id,
        actor=actor,
        action=f"proposal.{'approve' if decision == 'approved' else 'reject'}",
        target=f"proposal:{proposal_id}",
        payload={"comment": comment, "via": via, "role": role},
        ip=ip,
        at=now,
    )
    session.flush()
    return proposal


# --------------------------------------------------------------------------- messages


def dashboard_link(settings: Settings, proposal_id: int) -> str:
    """Relative link to the approval page (host is a deployment input, I-7)."""
    return f"{settings.next_public_base_path.rstrip('/')}/approvals/{proposal_id}"


def summary_line(p: Proposal) -> str:
    gr = p.guardrail_result or {}
    params = p.params or {}
    what = p.action
    if p.action == "budget_change":
        before = (p.expected_impact or {}).get("daily_budget_before") or gr.get("detail", {}).get(
            "current"
        )
        after = gr.get("new_daily_budget")
        what = f"budget_change {params.get('change_pct')}% ({before} → {after})"
    elif p.action == "add_negative":
        what = f"add_negative '{params.get('term')}' ({params.get('match_type')})"
    return f"#{p.id} {what} on {p.entity_type.value} {p.entity_id}"


def _local(settings: Settings, ts: dt.datetime | None) -> str:
    if ts is None:
        return "-"
    return ts.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M %Z")


def _message(settings: Settings, p: Proposal, headline: str) -> tuple[str, str, str]:
    link = dashboard_link(settings, p.id)
    subject = f"[ads-autopilot] {headline}: {summary_line(p)}"
    impact = p.expected_impact or {}
    lines = [
        headline,
        summary_line(p),
        f"Reason: {p.reason or '-'}",
        f"Risk: {p.risk} · spend direction: {p.spend_direction}",
        f"Expected impact: {impact or '-'}",
        f"Expires: {_local(settings, p.expires_at)}",
        f"Review (opening the link never approves): {link}",
    ]
    html = "".join(f"<p>{escape(x)}</p>" for x in lines)
    return subject, "\n".join(lines), html


# --------------------------------------------------------------------------- jobs


def request_approvals(session: Session, settings: Settings, now: dt.datetime) -> int:
    """§14 steps 1–2 for proposals the guardrail marked ``awaiting_approval``."""
    cid = settings.client_id
    channel = ",".join(configured_channel_names(settings)) or "dashboard"
    pending = session.scalars(
        select(Proposal)
        .where(
            Proposal.client_id == cid,
            Proposal.status == ProposalStatus.AWAITING_APPROVAL.value,
            ~exists().where(Approval.proposal_id == Proposal.id),
        )
        .order_by(Proposal.id)
    ).all()
    for p in pending:
        session.add(Approval(client_id=cid, proposal_id=p.id, channel=channel, requested_at=now))
        subject, text, html = _message(settings, p, "Approval needed")
        enqueue(
            session,
            cid,
            kind=NotificationKind.APPROVAL_REQUEST,
            dedupe_key=f"approval_request:{p.id}",
            subject=subject,
            body_text=text,
            body_html=html,
            payload={"proposal_id": p.id, "link": dashboard_link(settings, p.id)},
            proposal_id=p.id,
        )
    session.flush()
    return len(pending)


def expire_and_remind(session: Session, settings: Settings, now: dt.datetime) -> dict[str, int]:
    """§14.6. Runs every 15 min (``approval_expiry``) and in the daily ``approvals`` step."""
    cid = settings.client_id
    out = {"expired": 0, "reminded": 0}
    rows = session.scalars(
        select(Proposal)
        .where(
            Proposal.client_id == cid,
            Proposal.status == ProposalStatus.AWAITING_APPROVAL.value,
            Proposal.expires_at.is_not(None),
        )
        .order_by(Proposal.id)
        .with_for_update(skip_locked=True)
    ).all()
    for p in rows:
        assert p.expires_at is not None
        if p.expires_at <= now:
            p.status = ProposalStatus.EXPIRED.value
            subject, text, html = _message(settings, p, "Approval expired (not executed)")
            enqueue(
                session,
                cid,
                kind=NotificationKind.APPROVAL_EXPIRED,
                dedupe_key=f"approval_expired:{p.id}",
                subject=subject,
                body_text=text,
                body_html=html,
                proposal_id=p.id,
            )
            out["expired"] += 1
            continue
        appr = latest_approval(session, p.id)
        if (
            appr is not None
            and appr.decided_at is None
            and appr.reminded_at is None
            and now >= p.expires_at - REMIND_BEFORE_EXPIRY
        ):
            appr.reminded_at = now
            subject, text, html = _message(settings, p, "Reminder: approval needed")
            enqueue(
                session,
                cid,
                kind=NotificationKind.APPROVAL_REMINDER,
                dedupe_key=f"approval_reminder:{p.id}",
                subject=subject,
                body_text=text,
                body_html=html,
                proposal_id=p.id,
            )
            out["reminded"] += 1
    session.flush()
    return out


def recheck_approved(
    session: Session,
    settings: Settings,
    proposal_id: int,
    now: dt.datetime,
    builder: StateBuilder | None = None,
) -> GuardrailResult:
    """§14.5: guardrail with ``approved=True`` on fresh state. Block → ``blocked`` + notify.

    Returns the result; the caller (executor) applies only on Allow. Never executes anything.
    """
    p = session.get(Proposal, proposal_id)
    if p is None or p.client_id != settings.client_id:
        raise DecisionError(404, f"proposal {proposal_id} not found")
    if p.status != ProposalStatus.APPROVED.value:
        raise DecisionError(409, f"proposal {proposal_id} is {p.status}, not approved")
    b = builder or StateBuilder(session, settings, now)
    view = proposal_view(p)
    try:
        result: GuardrailResult = check(
            view, b.state(view.entity_type, view.entity_id), approved=True
        )
    except EntityMissingError as exc:
        from engine.guardrails.check import BlockReason

        result = Block(BlockReason.UNSUPPORTED_SCOPE, {"why": str(exc)})
    p.guardrail_result = result_json(result, approved=True, mode=b.mode, checked_at=now)
    if isinstance(result, Block):
        p.status = ProposalStatus.BLOCKED.value
        subject, text, html = _message(
            settings, p, f"Approved proposal blocked by guardrail ({result.reason.value})"
        )
        enqueue(
            session,
            settings.client_id,
            kind=NotificationKind.APPROVAL_BLOCKED,
            dedupe_key=f"approval_blocked:{p.id}",
            subject=subject,
            body_text=text,
            body_html=html,
            proposal_id=p.id,
        )
    session.flush()
    return result
