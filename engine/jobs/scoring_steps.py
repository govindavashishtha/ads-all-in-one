"""Phase 2 ``daily_pipeline`` steps (plan §9.1 steps 3, 5, 6, 8, 10, 11): features, score,
rules, guardrail, approvals, digest. Registered into ``engine.jobs.daily`` via ``STEPS``.

Each handler is ``fn(ctx, session) -> StepOutcome``; ``run_step`` commits the step's writes
and its ``job_runs`` success row in one transaction (§0 rule 7). Every write is an upsert or an
``ON CONFLICT DO NOTHING`` insert, and later steps only touch rows in the state they expect, so
re-running a step for the same ``as_of`` produces the same rows.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from engine.approvals.service import (
    APPROVAL_TTL,
    DecisionError,
    expire_and_remind,
    recheck_approved,
    request_approvals,
)
from engine.db.models import Feature, Proposal, Score
from engine.enums import EntityType, ProposalStatus, Severity
from engine.features.builder import build_features, upsert_features
from engine.features.data import load_metrics, load_universe
from engine.features.windows import lookback_start
from engine.guardrails.check import (
    Block,
    BlockReason,
    GuardrailResult,
    NeedsApproval,
    check,
    result_json,
)
from engine.guardrails.state import EntityMissingError, StateBuilder, proposal_view
from engine.ingest.alerts import raise_alert
from engine.jobs.common import active_rules, client_yesterday
from engine.jobs.runner import JobContext, StepOutcome
from engine.notify.digest import build_digest, store_digest
from engine.proposals import ACTIONABLE, insert_proposal
from engine.ranking.rank import compute_scores, upsert_scores
from engine.rules.engine import run_rules
from engine.rules.search_terms import load_term_aggregates, run_search_term_rules
from engine.rules.stats import actionable_rule_keys, ensure_rows

log = structlog.get_logger(__name__)

StepFn = Callable[[JobContext, Session], StepOutcome]


def as_of_for(ctx: JobContext) -> dt.date:
    return client_yesterday(ctx.now(), ctx.settings.timezone)


def _features_dict(session: Session, client_id: str, as_of: dt.date) -> dict[Any, dict[str, Any]]:
    from engine.rules.registry import names_from

    cols = sorted(names_from("features"))
    out: dict[Any, dict[str, Any]] = {}
    for row in session.scalars(
        select(Feature).where(Feature.client_id == client_id, Feature.as_of == as_of)
    ):
        out[(EntityType(row.entity_type), int(row.entity_id))] = {c: getattr(row, c) for c in cols}
    return out


def _scores_dict(session: Session, client_id: str, as_of: dt.date) -> dict[Any, dict[str, Any]]:
    from engine.ranking.rank import SCORE_COLUMNS

    out: dict[Any, dict[str, Any]] = {}
    for row in session.scalars(
        select(Score).where(Score.client_id == client_id, Score.as_of == as_of)
    ):
        out[(EntityType(row.entity_type), int(row.entity_id))] = {
            c: getattr(row, c) for c in SCORE_COLUMNS
        }
    return out


# --------------------------------------------------------------------------- 3. features


def features_step(ctx: JobContext, session: Session) -> StepOutcome:
    as_of = as_of_for(ctx)
    rules = active_rules(session, ctx.client_id)
    session.execute(
        delete(Feature).where(Feature.client_id == ctx.client_id, Feature.as_of == as_of)
    )
    _u, _s, rows = build_features(session, ctx.client_id, as_of, rules)
    return StepOutcome(rows=len(rows), details={"as_of": as_of.isoformat()})


# --------------------------------------------------------------------------- 5. score


def score_step(ctx: JobContext, session: Session) -> StepOutcome:
    as_of = as_of_for(ctx)
    rules = active_rules(session, ctx.client_id)
    feats = _features_dict(session, ctx.client_id, as_of)
    if not feats:
        return StepOutcome(skipped_reason=f"no features for {as_of} (features step skipped?)")
    universe = load_universe(session, ctx.client_id)
    store = load_metrics(session, ctx.client_id, lookback_start(as_of, rules.stats), as_of)
    scores = compute_scores(universe, store, feats, as_of, rules)
    session.execute(delete(Score).where(Score.client_id == ctx.client_id, Score.as_of == as_of))
    n = upsert_scores(session, ctx.client_id, as_of, scores)
    sufficient = sum(1 for s in scores.values() if s["data_sufficient"])
    return StepOutcome(rows=n, details={"data_sufficient": sufficient})


# --------------------------------------------------------------------------- 6. rules


def rules_step(ctx: JobContext, session: Session) -> StepOutcome:
    as_of = as_of_for(ctx)
    config = active_rules(session, ctx.client_id)
    feats = _features_dict(session, ctx.client_id, as_of)
    scores = _scores_dict(session, ctx.client_id, as_of)
    universe = load_universe(session, ctx.client_id)
    run = run_rules(config, universe.of_type(*{k[0] for k in feats}), feats, scores, as_of)
    t_matches, t_drafts = run_search_term_rules(
        config, universe, load_term_aggregates(session, ctx.client_id, as_of), as_of
    )
    ensure_rows(session, ctx.client_id, actionable_rule_keys(config))
    created = 0
    for d in [*run.drafts, *t_drafts]:
        if insert_proposal(session, ctx.client_id, d) is not None:
            created += 1
    by_rule = run.by_rule()
    for m in t_matches:
        by_rule[m.rule.key] = by_rule.get(m.rule.key, 0) + 1
    return StepOutcome(rows=created, details={"matches_by_rule": dict(sorted(by_rule.items()))})


# --------------------------------------------------------------------------- 8. guardrail


def guardrail_pending(
    session: Session, ctx: JobContext, now: dt.datetime, as_of: dt.date
) -> dict[str, int]:
    """Check every ``pending`` proposal (any source) and move it to blocked /
    awaiting_approval / approved (Allow = auto-approved for the executor)."""
    builder = StateBuilder(session, ctx.settings, now)
    pending = session.scalars(
        select(Proposal)
        .where(Proposal.client_id == ctx.client_id, Proposal.status == ProposalStatus.PENDING.value)
        .order_by(Proposal.id)
        .with_for_update()
    ).all()
    counts: dict[str, int] = {}
    cap_blocks = 0
    for p in pending:
        view = proposal_view(p)
        result: GuardrailResult
        if view.action not in ACTIONABLE:
            result = Block(BlockReason.UNSUPPORTED_SCOPE, {"why": f"{view.action} not executable"})
        else:
            try:
                result = check(view, builder.state(view.entity_type, view.entity_id))
            except EntityMissingError as exc:
                result = Block(BlockReason.UNSUPPORTED_SCOPE, {"why": str(exc)})
        p.guardrail_result = result_json(result, approved=False, mode=builder.mode, checked_at=now)
        if isinstance(result, Block):
            p.status = ProposalStatus.BLOCKED.value
            key = f"block:{result.reason.value}"
            if result.reason is BlockReason.CAP_EXCEEDED:
                cap_blocks += 1
        elif isinstance(result, NeedsApproval):
            p.status = ProposalStatus.AWAITING_APPROVAL.value
            p.expires_at = now + APPROVAL_TTL
            key = "needs_approval"
        else:
            p.status = ProposalStatus.APPROVED.value  # auto-allowed; executor re-checks
            key = "allow"
        counts[key] = counts.get(key, 0) + 1
    if cap_blocks:
        raise_alert(
            session,
            ctx.client_id,
            dedupe_key=f"guardrail_cap_exceeded:{as_of.isoformat()}",
            severity=Severity.WARN,
            kind="cap_exceeded",
            message=f"{cap_blocks} proposal(s) blocked: they would exceed a daily/monthly cap",
        )
    session.flush()
    return dict(sorted(counts.items()))


def guardrail_step(ctx: JobContext, session: Session) -> StepOutcome:
    counts = guardrail_pending(session, ctx, ctx.now(), as_of_for(ctx))
    return StepOutcome(rows=sum(counts.values()), details={"results": counts})


# --------------------------------------------------------------------------- 10. approvals


def approvals_step(ctx: JobContext, session: Session) -> StepOutcome:
    now = ctx.now()
    expiry = expire_and_remind(session, ctx.settings, now)
    requested = request_approvals(session, ctx.settings, now)
    rechecked = 0
    approved = session.scalars(
        select(Proposal.id)
        .where(
            Proposal.client_id == ctx.client_id, Proposal.status == ProposalStatus.APPROVED.value
        )
        .order_by(Proposal.id)
    ).all()
    if approved:
        builder = StateBuilder(session, ctx.settings, now)
        for pid in approved:
            try:
                recheck_approved(session, ctx.settings, int(pid), now, builder)
                rechecked += 1
            except DecisionError:  # pragma: no cover - status changed concurrently
                continue
    return StepOutcome(
        rows=requested, details={"requested": requested, "rechecked": rechecked, **expiry}
    )


# --------------------------------------------------------------------------- 11. digest


def digest_step(ctx: JobContext, session: Session) -> StepOutcome:
    as_of = as_of_for(ctx)
    digest = build_digest(session, ctx.settings, as_of, ctx.now())
    store_digest(session, ctx.settings, digest)
    return StepOutcome(rows=1, details={"subject": digest.subject})


STEPS: dict[str, StepFn] = {
    "features": features_step,
    "score": score_step,
    "rules": rules_step,
    "guardrail": guardrail_step,
    "approvals": approvals_step,
    "digest": digest_step,
}

__all__ = ["STEPS", "guardrail_pending", "upsert_features"]
