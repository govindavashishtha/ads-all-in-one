"""Evaluator (plan §16): D+3 safety check, D+7 verdict (at D+7+settle_days), D+14 follow-up.

Runs as daily step 4 ``evaluate`` and as the ``evaluate`` job (hourly at :40). An evaluation
is processed once it is due AND its whole post period is complete in the account's local
time (the last post day is before today). Each evaluation commits on its own; ``done_at``
makes it idempotent. Rollbacks (verdict ``worse``) go through ``engine.executor.rollback``
after the verdict is committed.

- D+3 (§16.2): ``spend_post ≥ 3 × target_cpa`` with 0 conversions → ``worse`` → rollback;
  a budget increase whose post average daily spend fell below 50 % of the 7 days before →
  ``warn`` alert "delivery collapsed" (no rollback); otherwise no verdict.
- D+7 (§16.3): pre = 7 days before the change day, post = 7 days after (change day
  excluded); ``insufficient_data`` → one D+14 evaluation (14-day post period), and an
  insufficient D+14 is final ``neutral``. Verdicts of D+7/D+14 feed ``rule_stats``.
- A change that is no longer ``applied`` (rolled back, …) closes its evaluation without a
  verdict.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from engine.db.models import (
    AdAccount,
    AdGroup,
    ChangeLog,
    Evaluation,
    MetricsDaily,
    Proposal,
)
from engine.enums import (
    Action,
    ChangeOrigin,
    ChangeStatus,
    EntityType,
    Platform,
    Severity,
    Verdict,
)
from engine.evaluation.units import UnitError, evaluation_unit
from engine.evaluation.verdict import Totals, d3_check, d7_verdict, insufficient
from engine.executor.apply import WriteEnv
from engine.executor.rollback import RollbackResult, rollback_change
from engine.ingest.alerts import raise_alert
from engine.jobs.common import active_rules
from engine.rules.stats import record_verdict

log = structlog.get_logger(__name__)

PRE_DAYS = 7


def _tz(s: Session, client_id: str, platform: Platform, fallback: str) -> str:
    tz = s.scalar(
        select(AdAccount.timezone).where(
            AdAccount.client_id == client_id, AdAccount.platform == platform
        )
    )
    return tz or fallback


def unit_totals(  # noqa: PLR0917
    s: Session, client_id: str, et: EntityType, eid: int, start: dt.date, end: dt.date
) -> Totals:
    row = s.execute(
        select(
            func.coalesce(func.sum(MetricsDaily.spend), 0),
            func.coalesce(func.sum(MetricsDaily.conversions), 0),
            func.coalesce(func.sum(MetricsDaily.conv_value), 0),
        ).where(
            MetricsDaily.client_id == client_id,
            MetricsDaily.entity_type == et,
            MetricsDaily.entity_id == eid,
            MetricsDaily.date >= start,
            MetricsDaily.date <= end,
        )
    ).one()
    return Totals(Decimal(row[0]), Decimal(row[1]), Decimal(row[2]))


def _campaign_of(s: Session, et: EntityType, eid: int) -> int | None:
    if et is EntityType.CAMPAIGN:
        return eid
    if et is EntityType.AD_GROUP:
        return s.scalar(select(AdGroup.campaign_id).where(AdGroup.id == eid))
    try:
        unit_t, unit_id = evaluation_unit(s, Action.PAUSE, et, eid)
    except UnitError:
        return None
    return _campaign_of(s, unit_t, unit_id)


def control_totals(  # noqa: PLR0917
    s: Session,
    client_id: str,
    platform: Platform,
    unit_campaign: int | None,
    start: dt.date,
    end: dt.date,
    tz: str,
) -> Totals:
    """Other campaigns on the platform with no engine change in [start, end] (§16.3)."""
    lo = dt.datetime.combine(start, dt.time(), tzinfo=ZoneInfo(tz))
    hi = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), tzinfo=ZoneInfo(tz))
    changed = s.execute(
        select(ChangeLog.entity_type, ChangeLog.entity_id).where(
            ChangeLog.client_id == client_id,
            ChangeLog.platform == platform,
            ChangeLog.applied_at.is_not(None),
            ChangeLog.applied_at >= lo,
            ChangeLog.applied_at < hi,
        )
    ).all()
    excluded = {c for et, eid in changed if (c := _campaign_of(s, EntityType(et), int(eid)))}
    if unit_campaign is not None:
        excluded.add(unit_campaign)
    q = select(
        func.coalesce(func.sum(MetricsDaily.spend), 0),
        func.coalesce(func.sum(MetricsDaily.conversions), 0),
        func.coalesce(func.sum(MetricsDaily.conv_value), 0),
    ).where(
        MetricsDaily.client_id == client_id,
        MetricsDaily.platform == platform,
        MetricsDaily.entity_type == EntityType.CAMPAIGN,
        MetricsDaily.date >= start,
        MetricsDaily.date <= end,
    )
    if excluded:
        q = q.where(MetricsDaily.entity_id.not_in(excluded))
    row = s.execute(q).one()
    return Totals(Decimal(row[0]), Decimal(row[1]), Decimal(row[2]))


def _budget_increase(change: ChangeLog) -> bool:
    before = (change.before or {}).get("daily_budget")
    after = (change.intended or {}).get("daily_budget")
    return before is not None and after is not None and Decimal(after) > Decimal(before)


def _changes_to_roll_back(s: Session, change: ChangeLog) -> list[int]:
    if change.proposal_id is None:
        return [int(change.id)]
    return [
        int(i)
        for i in s.scalars(
            select(ChangeLog.id)
            .where(
                ChangeLog.proposal_id == change.proposal_id,
                ChangeLog.origin == ChangeOrigin.PROPOSAL.value,
                ChangeLog.status == ChangeStatus.APPLIED.value,
            )
            .order_by(ChangeLog.id.desc())
        )
    ]


def evaluate_one(env: WriteEnv, evaluation_id: int) -> str:  # noqa: PLR0912, PLR0915
    now = env.now()
    rollback_ids: list[int] = []
    with env.sessions() as s, s.begin():
        e = s.scalars(
            select(Evaluation)
            .where(Evaluation.id == evaluation_id, Evaluation.done_at.is_(None))
            .with_for_update(skip_locked=True)
        ).first()
        if e is None:
            return "skipped"
        change = s.get(ChangeLog, e.change_id)
        assert change is not None
        if change.status != ChangeStatus.APPLIED.value or change.applied_at is None:
            e.done_at = now
            e.action_taken = f"cancelled: change is {change.status}"
            return "cancelled"
        tz = _tz(s, env.client_id, Platform(change.platform), env.settings.timezone)
        today = now.astimezone(ZoneInfo(tz)).date()
        day0 = change.applied_at.astimezone(ZoneInfo(tz)).date()
        post_start, post_end = day0 + dt.timedelta(days=1), day0 + dt.timedelta(days=e.horizon_days)
        if post_end >= today:
            return "not_ready"
        rules = active_rules(s, env.client_id)
        target_cpa = rules.targets.target_cpa
        et, eid = EntityType(e.unit_entity_type), int(e.unit_entity_id)
        pre_start, pre_end = day0 - dt.timedelta(days=PRE_DAYS), day0 - dt.timedelta(days=1)
        pre = unit_totals(s, env.client_id, et, eid, pre_start, pre_end)
        post = unit_totals(s, env.client_id, et, eid, post_start, post_end)
        e.baseline = {"from": pre_start.isoformat(), "to": pre_end.isoformat(), **pre.as_json()}
        e.observed = {"from": post_start.isoformat(), "to": post_end.isoformat(), **post.as_json()}
        proposal = s.get(Proposal, change.proposal_id) if change.proposal_id else None
        e.done_at = now
        if e.horizon_days == 3:
            r3 = d3_check(
                post,
                target_cpa=target_cpa,
                budget_increase=_budget_increase(change),
                pre_avg_daily=pre.spend / PRE_DAYS,
                post_days=e.horizon_days,
            )
            if r3.worse:
                e.verdict = Verdict.WORSE.value
                rollback_ids = _changes_to_roll_back(s, change)
                e.action_taken = "rollback pending"
            elif r3.delivery_collapsed:
                e.action_taken = "warn: delivery collapsed"
                raise_alert(
                    s,
                    env.client_id,
                    dedupe_key=f"delivery_collapsed:{change.id}",
                    severity=Severity.WARN,
                    kind="delivery_collapsed",
                    entity_type=et,
                    entity_id=eid,
                    message=f"delivery collapsed after budget increase (change #{change.id}): "
                    f"post avg daily spend {post.spend / e.horizon_days:.2f} < 50% of "
                    f"pre {pre.spend / PRE_DAYS:.2f}",
                )
            else:
                e.action_taken = "none"
            verdict = e.verdict or "none"
        else:
            if insufficient(pre, post, target_cpa):
                if e.horizon_days == 7:
                    settle = rules.stats.settle_days
                    s.execute(
                        pg_insert(Evaluation)
                        .values(
                            client_id=env.client_id,
                            change_id=change.id,
                            horizon_days=14,
                            due_at=change.applied_at + dt.timedelta(days=14 + settle),
                            unit_entity_type=et,
                            unit_entity_id=eid,
                        )
                        .on_conflict_do_nothing(index_elements=["change_id", "horizon_days"])
                    )
                    e.verdict = Verdict.INSUFFICIENT_DATA.value
                    e.action_taken = "scheduled D+14"
                else:
                    e.verdict = Verdict.NEUTRAL.value  # §16.3: insufficient again → neutral
                    e.action_taken = "none (insufficient data at D+14)"
            else:
                c_pre = control_totals(
                    s,
                    env.client_id,
                    Platform(change.platform),
                    _campaign_of(s, et, eid),
                    pre_start,
                    pre_end,
                    tz,
                )
                c_post = control_totals(
                    s,
                    env.client_id,
                    Platform(change.platform),
                    _campaign_of(s, et, eid),
                    post_start,
                    post_end,
                    tz,
                )
                r7 = d7_verdict(
                    pre,
                    post,
                    c_pre,
                    c_post,
                    target_cpa=target_cpa,
                    objective=rules.targets.objective,
                    draws=rules.stats.mc_draws,
                    seed=int(change.id),
                )
                e.verdict, e.p_worse, e.p_better = r7.verdict.value, r7.p_worse, r7.p_better
                e.observed = {
                    **e.observed,
                    "g": r7.g,
                    "control_pre": c_pre.as_json(),
                    "control_post": c_post.as_json(),
                }
                if r7.verdict is Verdict.WORSE:
                    rollback_ids = _changes_to_roll_back(s, change)
                    e.action_taken = "rollback pending"
                else:
                    e.action_taken = "none"
            verdict = str(e.verdict)
            if proposal is not None and proposal.rule_key and e.verdict:
                record_verdict(
                    s, env.client_id, proposal.rule_key, Verdict(e.verdict), e.horizon_days
                )
    if rollback_ids:
        results = [
            (cid, rollback_change(env, cid, requested_by=f"evaluator:{evaluation_id}"))
            for cid in rollback_ids
        ]
        _record_rollbacks(env, evaluation_id, results)
    return verdict


def _record_rollbacks(
    env: WriteEnv, evaluation_id: int, results: list[tuple[int, RollbackResult]]
) -> None:
    parts: list[str] = []
    failed: list[str] = []
    for cid, r in results:
        if r.ok:
            parts.append(f"rollback of #{cid}: change #{r.change_id}")
        elif r.status == "drift_proposal":
            parts.append(f"rollback of #{cid}: drift, proposal #{r.drift_proposal_id}")
        else:
            parts.append(f"rollback of #{cid} {r.status}: {r.detail}")
            failed.append(f"#{cid} {r.status}: {r.detail}")
    with env.sessions() as s, s.begin():
        e = s.get(Evaluation, evaluation_id)
        assert e is not None
        e.action_taken = "; ".join(parts)[:2000]
        if failed:
            raise_alert(
                s,
                env.client_id,
                dedupe_key=f"rollback_not_executed:{evaluation_id}",
                severity=Severity.CRITICAL,
                kind="rollback_failed",
                message=f"evaluation #{evaluation_id} verdict worse but rollback not executed: "
                + "; ".join(failed),
            )


def evaluate_due(env: WriteEnv, *, limit: int = 200) -> dict[str, Any]:
    with env.sessions() as s:
        ids = s.scalars(
            select(Evaluation.id)
            .where(
                Evaluation.client_id == env.client_id,
                Evaluation.done_at.is_(None),
                Evaluation.due_at <= env.now(),
            )
            .order_by(Evaluation.due_at, Evaluation.id)
            .limit(limit)
        ).all()
    counts: dict[str, int] = {}
    for eid in ids:
        r = evaluate_one(env, int(eid))
        counts[r] = counts.get(r, 0) + 1
    return dict(sorted(counts.items()))
