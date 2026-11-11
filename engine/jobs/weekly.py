"""``weekly_strategy`` (plan §9, Mon 07:00) — Phase 2 parts, plus the small recurring
approval/notification jobs.

Steps (each a ``job_runs`` row, ``window_key`` = as_of = client-local yesterday):
1. ``allocation`` — §10.7 zero-sum budget moves inside pools → ``budget_change`` proposals
   (``source=allocation``, ``risk=low``);
2. ``rule_review`` — §16.4 learning notes from ``rule_stats`` (alerts only, never rule edits);
3. ``guardrail`` — check the new pending proposals (§12.3);
4. ``approvals`` — approval requests for the ones that need a human (§14).
The 30-day re-pull, creative refresh and LLM analyst parts belong to the connectors and LLM
agents and are not run here.

Also: ``approval_expiry`` (every 15 min, §9) and ``notify_dispatch`` (delivers the outbox).
"""

from __future__ import annotations

import datetime as dt
from functools import partial
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engine.approvals.service import expire_and_remind, request_approvals
from engine.db.models import Score
from engine.enums import Action, BudgetLevel, EntityType, ProposalSource, Risk
from engine.features.data import EntityInfo, EntityKey, Universe, load_universe
from engine.guardrails.state import StateBuilder
from engine.jobs.common import active_limits, active_rules, client_yesterday
from engine.jobs.runner import JobContext, StepOutcome, StepResult, job_lock, run_step
from engine.jobs.scoring_steps import _features_dict, _scores_dict, guardrail_pending
from engine.notify.dispatch import deliver_pending
from engine.proposals import ProposalDraft, insert_proposal
from engine.ranking.allocation import AllocationParams, Delta, Member, allocate
from engine.rules.stats import learning_notes, raise_learning_alerts

log = structlog.get_logger(__name__)
JOB = "weekly_strategy"


def _pools(universe: Universe) -> dict[str, list[EntityInfo]]:
    """§10.7 step 1 (structure only; eligibility is filtered afterwards)."""
    pools: dict[str, list[EntityInfo]] = {}
    for c in universe.of_type(EntityType.CAMPAIGN):
        if not (c.campaign_managed and c.effective_active):
            continue
        if c.budget_level == BudgetLevel.AD_SET.value and c.platform.value == "meta":
            pools[f"campaign:{c.id}:ad_sets"] = [
                g
                for g in universe.of_type(EntityType.AD_GROUP)
                if g.campaign_id == c.id and g.is_budget_unit and g.effective_active
            ]
        if c.pool and c.is_budget_unit:
            pools.setdefault(f"pool:{c.pool}", []).append(c)
    return pools


def allocation_drafts(
    session: Session, ctx: JobContext, as_of: dt.date, now: dt.datetime
) -> tuple[list[ProposalDraft], dict[str, Any]]:
    rules = active_rules(session, ctx.client_id)
    limits = active_limits(session, ctx.client_id)
    score_as_of = session.scalar(
        select(func.max(Score.as_of)).where(Score.client_id == ctx.client_id, Score.as_of <= as_of)
    )
    if score_as_of is None:
        return [], {"skipped": "no scores yet"}
    if rules.targets.objective == "traffic":
        return [], {"skipped": "objective=traffic: allocation is defined on θ (DECISIONS D-16)"}
    feats = _features_dict(session, ctx.client_id, score_as_of)
    scores = _scores_dict(session, ctx.client_id, score_as_of)
    universe = load_universe(session, ctx.client_id)
    builder = StateBuilder(session, ctx.settings, now, universe=universe, limits=limits)
    params = AllocationParams(
        objective=rules.targets.objective,
        mc_draws=rules.stats.mc_draws,
        share_floor_factor=limits.allocation.share_floor_factor,
        share_ceiling=limits.allocation.share_ceiling,
        auto_max_budget_change_pct=limits.change.auto_max_budget_change_pct,
        min_spend_pace=limits.allocation.min_spend_pace,
        max_frequency_7d=limits.allocation.max_frequency_7d,
        min_useful_change_pct=limits.change.min_useful_change_pct,
    )
    cd = limits.cooldowns
    drafts: list[ProposalDraft] = []
    detail: dict[str, Any] = {"scores_as_of": score_as_of.isoformat(), "pools": {}}
    for name, entities in sorted(_pools(universe).items()):
        members: list[Member] = []
        for e in entities:
            s, f = scores.get(e.key), feats.get(e.key)
            if s is None or f is None or not s["data_sufficient"] or e.learning:
                continue
            if e.daily_budget is None or e.daily_budget <= 0:
                continue
            snap = builder.snapshot(e.entity_type, e.id)
            if snap.last_applied_change_at and now - snap.last_applied_change_at < dt.timedelta(
                days=cd.same_entity_days
            ):
                continue
            if snap.last_manual_change_at and now - snap.last_manual_change_at < dt.timedelta(
                hours=cd.after_manual_change_hours
            ):
                continue
            aov = s.get("aov_shrunk")
            members.append(
                Member(
                    key=e.key,
                    platform=e.platform,
                    current_budget=e.daily_budget,
                    theta_a=float(s["theta_a"]),
                    theta_b=float(s["theta_b"]),
                    aov=float(aov) if aov is not None else None,
                    spend_pace_7d=f.get("spend_pace_7d"),
                    frequency_7d=f.get("frequency_7d"),
                )
            )
        if len(members) < 2:
            detail["pools"][name] = f"skipped: {len(members)} eligible member(s)"
            continue
        deltas = allocate(members, params, score_as_of)
        detail["pools"][name] = f"{len(members)} members, {len(deltas)} moves"
        drafts.extend(_draft(name, d, as_of) for d in deltas)
    return drafts, detail


def _draft(pool: str, d: Delta, as_of: dt.date) -> ProposalDraft:
    et, eid = d.key
    return ProposalDraft(
        as_of=as_of,
        source=ProposalSource.ALLOCATION,
        rule_key=None,
        entity_type=et,
        entity_id=eid,
        action=Action.BUDGET_CHANGE,
        risk=Risk.LOW,
        reason=(
            f"weekly allocation {pool}: win_share={d.win_share:.3f}, "
            f"target_share={d.target_share:.3f}, {d.current} → {d.new_budget}"
        ),
        params={
            "change_pct": float(d.change_pct),
            "pool": pool,
            "target_daily_budget": str(d.new_budget),
        },
        expected_impact={
            "daily_budget_before": str(d.current),
            "daily_budget_delta_est": str(d.delta),
        },
    )


def _allocation_step(ctx: JobContext, as_of: dt.date, session: Session) -> StepOutcome:
    drafts, detail = allocation_drafts(session, ctx, as_of, ctx.now())
    created = sum(1 for d in drafts if insert_proposal(session, ctx.client_id, d) is not None)
    return StepOutcome(rows=created, details=detail)


def _rule_review_step(ctx: JobContext, as_of: dt.date, session: Session) -> StepOutcome:
    notes = learning_notes(session, ctx.client_id)
    raise_learning_alerts(session, ctx.client_id, as_of, notes)
    return StepOutcome(rows=len(notes))


def _guardrail_step(ctx: JobContext, as_of: dt.date, session: Session) -> StepOutcome:
    counts = guardrail_pending(session, ctx, ctx.now(), as_of)
    return StepOutcome(rows=sum(counts.values()), details={"results": counts})


def _approvals_step(ctx: JobContext, as_of: dt.date, session: Session) -> StepOutcome:
    n = request_approvals(session, ctx.settings, ctx.now())
    return StepOutcome(rows=n)


def _creative_step(ctx: JobContext, as_of: dt.date, session: Session) -> StepOutcome:
    """Phase 4 (LLM agent): fatigue → ``new_ad`` drafts (approval required, PAUSED)."""
    from engine.llm.steps import creative_step

    return creative_step(ctx, as_of, session)


WEEKLY_STEPS = (
    ("allocation", _allocation_step),
    ("rule_review", _rule_review_step),
    ("creative", _creative_step),
    ("guardrail", _guardrail_step),
    ("approvals", _approvals_step),
)


def weekly_strategy(ctx: JobContext) -> list[StepResult]:
    as_of = client_yesterday(ctx.now(), ctx.settings.timezone)
    results: list[StepResult] = []
    with job_lock(ctx.engine, ctx.client_id, JOB) as got:
        if not got:
            return results
        for name, fn in WEEKLY_STEPS:
            results.append(run_step(ctx, JOB, name, as_of.isoformat(), partial(fn, ctx, as_of)))
    return results


def _key_str(k: EntityKey) -> str:
    return f"{k[0].value}:{k[1]}"


def approval_expiry(ctx: JobContext) -> StepResult | None:
    """Every 15 min (§9): remind at 12 h, expire at 24 h. Window = the quarter hour."""
    now = ctx.now()
    window = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    with job_lock(ctx.engine, ctx.client_id, "approval_expiry") as got:
        if not got:
            return None

        def body(session: Session) -> StepOutcome:
            out = expire_and_remind(session, ctx.settings, now)
            return StepOutcome(rows=sum(out.values()), details=dict[str, object](out))

        return run_step(ctx, "approval_expiry", None, window.isoformat(), body)


def notify_dispatch(ctx: JobContext) -> dict[str, int]:
    """Deliver queued notifications (log-only unless a channel transport is wired)."""
    with job_lock(ctx.engine, ctx.client_id, "notify_dispatch") as got:
        if not got:
            return {}
        with ctx.sessions() as s, s.begin():
            return deliver_pending(s, ctx.settings, ctx.now())
