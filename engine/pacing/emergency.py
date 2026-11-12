"""Emergency stop, pause-all and restore (plan §12.4 steps 5–6, §17 ``/control/pause-all``,
``/emergency-actions/{id}/restore``).

Emergency stop (called by the ``pacing`` job through ``engine.jobs.pacing.EMERGENCY_HANDLER``):
- only when ``emergency_stop_enabled`` and mode ≠ ``read_only`` (the pacing job checks the
  first; read_only → the critical alert only);
- the write gate must be open (``clients.execution_enabled``, D-28). When it is closed a
  ``critical`` alert says that NOTHING was paused and why;
- pauses every **managed, active** campaign on the breached platform (both on a total
  breach) via the executor write path with ``validate=False`` (no dry-run, §15 step 4),
  write-ahead ``change_log`` (``origin = emergency``) and read-back;
- one ``emergency_actions`` row lists exactly the campaigns the engine paused;
  ``restore_policy = auto_next_day`` (daily trigger) or ``manual`` (monthly / pause-all);
- always a ``critical`` alert naming paused, failed and still-active unmanaged campaigns.
It pauses; it never edits budgets.

Restore: ``auto_next_day`` rows once the account-local date is later than the trigger date
(job ``emergency_restore``, hourly at :10), ``manual`` rows only via the API. Requires kill
switch off, mode ≠ read_only, no monthly-cap breach; re-enables a campaign only if it is
still PAUSED on the platform and its last ``entity_history`` row has ``source = emergency``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.connectors.base import EntityStatus
from engine.db.models import AdAccount, Campaign, EmergencyAction, EntityHistory
from engine.enums import (
    Action,
    ChangeOrigin,
    EmergencyTrigger,
    EntityType,
    HistorySource,
    Mode,
    Platform,
    ProposalSource,
    RestorePolicy,
    Risk,
    Severity,
    SpendDirection,
)
from engine.executor.apply import Outcome, WriteEnv, env_from_ctx, perform
from engine.executor.clients import WriteUnavailableError
from engine.executor.gate import write_gate
from engine.executor.plan import OpKind, WriteOp
from engine.guardrails.check import Block, ProposalView, rollback_checks
from engine.guardrails.state import EntityMissingError, StateBuilder
from engine.ingest.alerts import raise_alert
from engine.jobs.common import active_limits

if TYPE_CHECKING:
    from engine.jobs.pacing import EmergencyCondition
    from engine.jobs.runner import JobContext

log = structlog.get_logger(__name__)


@dataclass
class PauseResult:
    platform: Platform
    trigger: str
    paused: list[int] = field(default_factory=list)
    already_paused: list[int] = field(default_factory=list)
    failed: dict[int, str] = field(default_factory=dict)
    unmanaged_active: list[str] = field(default_factory=list)
    blocked: str | None = None
    emergency_action_id: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "platform": self.platform.value,
            "trigger": self.trigger,
            "paused": self.paused,
            "already_paused": self.already_paused,
            "failed": {str(k): v for k, v in self.failed.items()},
            "unmanaged_active": self.unmanaged_active,
            "blocked": self.blocked,
            "emergency_action_id": self.emergency_action_id,
        }


def _local_date(
    s: Session, client_id: str, platform: Platform, now: dt.datetime, fallback_tz: str
) -> dt.date:
    tz = s.scalar(
        select(AdAccount.timezone).where(
            AdAccount.client_id == client_id, AdAccount.platform == platform
        )
    )
    return now.astimezone(ZoneInfo(tz or fallback_tz)).date()


def pause_platform(
    env: WriteEnv,
    platform: Platform,
    trigger: EmergencyTrigger,
    *,
    origin: ChangeOrigin,
    key_prefix: str,
    requested_by: str,
) -> PauseResult:
    res = PauseResult(platform, trigger.value)
    policy = (
        RestorePolicy.AUTO_NEXT_DAY
        if trigger is EmergencyTrigger.DAILY_CAP
        else RestorePolicy.MANUAL
    )
    with env.sessions() as s:
        gate = write_gate(s, env.settings, reducing=True)
        camps = s.execute(
            select(Campaign.id, Campaign.external_id, Campaign.name, Campaign.managed)
            .where(
                Campaign.client_id == env.client_id,
                Campaign.platform == platform,
                Campaign.status == EntityStatus.ACTIVE.value,
            )
            .order_by(Campaign.id)
        ).all()
    res.unmanaged_active = [f"{c.name or c.external_id} (#{c.id})" for c in camps if not c.managed]
    managed = [c for c in camps if c.managed]
    if not gate.open:
        res.blocked = gate.reason
    else:
        try:
            pc = env.clients.get(platform, conversion_action=None)
        except WriteUnavailableError as exc:
            res.blocked = str(exc)
            pc = None
        for c in managed if pc is not None else []:
            op = WriteOp(
                OpKind.SET_STATUS,
                Action.PAUSE,
                platform,
                EntityType.CAMPAIGN,
                str(c.external_id),
                EntityType.CAMPAIGN,
                int(c.id),
                {"status": EntityStatus.PAUSED.value},
                {"status": EntityStatus.PAUSED.value},
            )
            out = perform(
                env,
                op,
                origin=origin,
                key=f"{key_prefix}:{c.id}",
                applied_by=requested_by,
                validate=False,
                history_source=HistorySource.EMERGENCY,
                clients=pc,
            )
            if out.outcome is Outcome.APPLIED:
                res.paused.append(int(c.id))
            elif out.outcome is Outcome.NOOP:
                res.already_paused.append(int(c.id))
            else:
                res.failed[int(c.id)] = f"{out.outcome.value}: {out.error or ''}".strip()
    with env.sessions() as s, s.begin():
        if res.paused:
            ea = EmergencyAction(
                client_id=env.client_id,
                platform=platform,
                trigger=trigger.value,
                campaign_ids=res.paused,
                triggered_at=env.now(),
                restore_policy=policy.value,
            )
            s.add(ea)
            s.flush()
            res.emergency_action_id = int(ea.id)
        _summary_alert(s, env, res, key_prefix)
    return res


def _summary_alert(s: Session, env: WriteEnv, r: PauseResult, key_prefix: str) -> None:
    if r.blocked:
        msg = f"{r.platform.value} {r.trigger}: NOTHING was paused — {r.blocked}"
    else:
        msg = f"{r.platform.value} {r.trigger}: paused {len(r.paused)} managed campaign(s)"
        if r.paused:
            msg += f" {r.paused}"
        if r.emergency_action_id:
            msg += f" (emergency action #{r.emergency_action_id})"
        if r.failed:
            msg += f"; FAILED to pause {r.failed}"
    if r.unmanaged_active:
        msg += "; unmanaged campaigns still active (never touched by the engine): " + ", ".join(
            r.unmanaged_active
        )
    raise_alert(
        s,
        env.client_id,
        dedupe_key=f"{key_prefix}:result",
        severity=Severity.CRITICAL,
        kind="emergency_stop",
        message=msg[:4000],
    )


# ----------------------------------------------------------------------------- pacing hook


def emergency_handler(
    ctx: JobContext, _s: Session, conditions: Sequence[EmergencyCondition], mode: Mode
) -> list[PauseResult]:
    """``EMERGENCY_HANDLER``: runs its own transactions (write-ahead must commit)."""
    if mode is Mode.READ_ONLY or not conditions:
        return []
    env = env_from_ctx(ctx)
    now = env.now()
    wanted: dict[Platform, EmergencyTrigger] = {}
    # monthly first: its manual restore policy is the conservative one
    for c in sorted(conditions, key=lambda c: c.trigger != "monthly_cap"):
        trig = EmergencyTrigger(c.trigger)
        for pl in [c.platform] if c.platform is not None else list(Platform):
            wanted.setdefault(pl, trig)
    out: list[PauseResult] = []
    for platform, trig in wanted.items():
        with env.sessions() as s:
            day = _local_date(s, env.client_id, platform, now, env.settings.timezone)
        out.append(
            pause_platform(
                env,
                platform,
                trig,
                origin=ChangeOrigin.EMERGENCY,
                key_prefix=f"emergency:{platform.value}:{trig.value}:{day}",
                requested_by="emergency",
            )
        )
    log.warning("emergency.done", results=[r.as_json() for r in out])
    return out


def install() -> None:
    """Wire the Phase 3 hook (called by the scheduler wiring)."""
    from engine.jobs import pacing

    pacing.EMERGENCY_HANDLER = emergency_handler


# ----------------------------------------------------------------------------- pause-all


def pause_all(
    env: WriteEnv,
    *,
    requested_by: str,
    request_id: int,
    platforms: Sequence[Platform] | None = None,
) -> list[PauseResult]:
    """``POST /control/pause-all`` (via the write_requests queue): both platforms unless
    given, manual restore. Privileged and spend-reducing: passes the kill switch (§12.3
    step 2)."""
    return [
        pause_platform(
            env,
            pl,
            EmergencyTrigger.PAUSE_ALL,
            origin=ChangeOrigin.PAUSE_ALL,
            key_prefix=f"pause_all:{request_id}:{pl.value}",
            requested_by=requested_by,
        )
        for pl in (platforms or list(Platform))
    ]


# ----------------------------------------------------------------------------- restore


def _monthly_breach(s: Session, env: WriteEnv, platform: Platform) -> str | None:
    limits = active_limits(s, env.client_id)
    b = StateBuilder(s, env.settings, env.now(), limits=limits)
    frac = Decimal(str(limits.pacing.emergency_spent_fraction))
    cap_p = limits.caps.for_platform(platform).monthly
    mtd_p = b.mtd.get(platform, Decimal(0))
    total = sum(b.mtd.values(), Decimal(0))
    if mtd_p >= frac * cap_p:
        return f"{platform.value} MTD {mtd_p} ≥ {frac} × monthly cap {cap_p}"
    if total >= frac * limits.caps.total.monthly:
        return f"total MTD {total} ≥ {frac} × monthly cap {limits.caps.total.monthly}"
    return None


def _last_source(s: Session, client_id: str, campaign_id: int) -> str | None:
    return s.scalar(
        select(EntityHistory.source)
        .where(
            EntityHistory.client_id == client_id,
            EntityHistory.entity_type == EntityType.CAMPAIGN,
            EntityHistory.entity_id == campaign_id,
        )
        .order_by(EntityHistory.observed_at.desc(), EntityHistory.id.desc())
        .limit(1)
    )


def restore_action(  # noqa: PLR0912, PLR0915
    env: WriteEnv, action_id: int, *, requested_by: str
) -> dict[str, Any]:
    """Re-enable the campaigns of one ``emergency_actions`` row (§12.4 step 6)."""
    result: dict[str, Any] = {"emergency_action_id": action_id, "restored": [], "skipped": {}}
    with env.sessions() as s:
        ea = s.get(EmergencyAction, action_id)
        if ea is None or ea.client_id != env.client_id:
            return {**result, "status": "not_found"}
        if ea.restored_at is not None:
            return {**result, "status": "already_restored"}
        platform = Platform(ea.platform)
        ids = list(ea.campaign_ids)
        gate = write_gate(s, env.settings, reducing=False)
        if not gate.open:
            return {**result, "status": "blocked", "detail": gate.reason}
        breach = _monthly_breach(s, env, platform)
        if breach:
            return {**result, "status": "blocked", "detail": f"monthly cap breached: {breach}"}
        camps = {int(c.id): c for c in s.scalars(select(Campaign).where(Campaign.id.in_(ids)))}
        sources = {cid: _last_source(s, env.client_id, cid) for cid in ids}
    try:
        pc = env.clients.get(platform, conversion_action=None)
    except WriteUnavailableError as exc:
        return {**result, "status": "blocked", "detail": str(exc)}
    for cid in ids:
        c = camps.get(cid)
        if c is None:
            result["skipped"][cid] = "campaign not found"
            continue
        if sources.get(cid) != HistorySource.EMERGENCY.value:
            result["skipped"][cid] = f"touched since (last history source {sources.get(cid)})"
            continue
        try:
            st = pc.reader.read_state(EntityType.CAMPAIGN, c.external_id)
        except Exception as exc:
            result["skipped"][cid] = f"read failed: {exc}"
            continue
        if st.status is not EntityStatus.PAUSED:
            result["skipped"][cid] = f"platform status is {st.status.value}, not paused"
            continue
        with env.sessions() as s:
            try:
                state = StateBuilder(s, env.settings, env.now()).state(EntityType.CAMPAIGN, cid)
            except EntityMissingError as exc:
                result["skipped"][cid] = str(exc)
                continue
            view = ProposalView(
                None,
                ProposalSource.EMERGENCY,
                None,
                EntityType.CAMPAIGN,
                cid,
                Action.ENABLE,
                {},
                Risk.LOW,
                SpendDirection.INCREASING,
            )
            guard = rollback_checks(view, state)
        if isinstance(guard, Block):
            result["skipped"][cid] = f"guardrail {guard.reason.value}: {guard.detail}"
            continue
        op = WriteOp(
            OpKind.SET_STATUS,
            Action.ENABLE,
            platform,
            EntityType.CAMPAIGN,
            str(c.external_id),
            EntityType.CAMPAIGN,
            cid,
            {"status": EntityStatus.ACTIVE.value},
            {"status": EntityStatus.ACTIVE.value},
        )
        out = perform(
            env,
            op,
            origin=ChangeOrigin.RESTORE,
            key=f"restore:{action_id}:{cid}",
            applied_by=requested_by,
            clients=pc,
        )
        if out.ok:
            result["restored"].append(cid)
        else:
            result["skipped"][cid] = f"{out.outcome.value}: {out.error or ''}".strip()
    with env.sessions() as s, s.begin():
        ea2 = s.get(EmergencyAction, action_id, with_for_update=True)
        assert ea2 is not None
        ea2.restored_at = env.now()
        ea2.restored_by = requested_by
        raise_alert(
            s,
            env.client_id,
            dedupe_key=f"emergency_restore:{action_id}",
            severity=Severity.WARN,
            kind="emergency_restore",
            message=f"emergency action #{action_id} restored by {requested_by}: re-enabled "
            f"{result['restored']}; skipped {result['skipped']}"[:4000],
        )
    result["skipped"] = {str(k): v for k, v in result["skipped"].items()}
    return {**result, "status": "restored"}


def auto_restore(env: WriteEnv) -> list[dict[str, Any]]:
    """Job ``emergency_restore``: ``auto_next_day`` rows whose account-local trigger date is
    before today's."""
    now = env.now()
    with env.sessions() as s:
        rows = s.execute(
            select(EmergencyAction.id, EmergencyAction.platform, EmergencyAction.triggered_at)
            .where(
                EmergencyAction.client_id == env.client_id,
                EmergencyAction.restore_policy == RestorePolicy.AUTO_NEXT_DAY.value,
                EmergencyAction.restored_at.is_(None),
            )
            .order_by(EmergencyAction.id)
        ).all()
        due: list[int] = []
        for rid, platform, triggered in rows:
            tz = (
                s.scalar(
                    select(AdAccount.timezone).where(
                        AdAccount.client_id == env.client_id, AdAccount.platform == platform
                    )
                )
                or env.settings.timezone
            )
            if now.astimezone(ZoneInfo(tz)).date() > triggered.astimezone(ZoneInfo(tz)).date():
                due.append(int(rid))
    return [restore_action(env, rid, requested_by="emergency_restore") for rid in due]
