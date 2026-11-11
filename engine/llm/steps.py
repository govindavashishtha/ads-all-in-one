"""Job steps of the LLM layer (plan §9.1 step 7 ``triage``; §9 weekly creative refresh).

``triage_step`` (daily_pipeline, between ``rules`` and ``guardrail``): every ``escalated``
proposal of the run's ``as_of`` that is not a fatigue escalation and has not been triaged yet
goes to the small model in batches of ≤ 20; ``escalate_large`` answers go to the large model.
Results become ``pending`` proposals (judged by the next step, the guardrail), ``noop`` or
``flag_human`` (a deduped ``warn`` alert for a person). With the LLM disabled, unconfigured,
over budget or failing, every case becomes ``flag_human`` and the pipeline completes (§13.1).

The escalated proposal keeps status ``escalated`` (it records what the rule saw) and gets a
``params.triage`` annotation — outcome, action, confidence, ``llm_call_id``, the new
proposal id — which also makes the step idempotent (DECISIONS D-21).

``creative_step`` (weekly_strategy, before its guardrail): fatigue escalations of the last 7
days → ``new_ad`` drafts (see ``engine.llm.creative``); annotation ``params.creative``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.config import Settings
from engine.db.models import Ad, Proposal
from engine.enums import EntityType, ProposalSource, ProposalStatus, Severity
from engine.features.data import load_universe
from engine.ingest.alerts import raise_alert
from engine.jobs.common import active_rules
from engine.jobs.runner import JobContext, StepOutcome
from engine.jobs.scoring_steps import _features_dict, _scores_dict, as_of_for
from engine.llm import creative as cr
from engine.llm.client import LLMClient
from engine.llm.gateway import CallResult, LLMGateway
from engine.llm.providers import build_client
from engine.llm.schemas import MAX_CASES_PER_CALL, LLMAction
from engine.llm.summariser import Case, build_cases, client_block
from engine.llm.triage import Verdict, build_request, parse_verdicts, proposal_from
from engine.proposals import insert_proposal
from engine.rules.schema import RulesConfig

log = structlog.get_logger(__name__)

FATIGUE_REASON = "fatigue"
CREATIVE_LOOKBACK_DAYS = 7

# Tests (and a future DI container) may swap the client factory; production uses env config.
ClientFactory = Callable[[Settings], tuple[LLMClient | None, str]]


def _default_factory(settings: Settings) -> tuple[LLMClient | None, str]:
    choice = build_client(settings)
    return choice.client, choice.reason


client_factory: ClientFactory = _default_factory


def _chunks[T](xs: Sequence[T], n: int = MAX_CASES_PER_CALL) -> list[Sequence[T]]:
    return [xs[i : i + n] for i in range(0, len(xs), n)]


def _fatigue_rules(rules: RulesConfig) -> set[str]:
    return {r.key for r in rules.rules if r.reason == FATIGUE_REASON}


def _annotate(p: Proposal, key: str, note: dict[str, Any]) -> None:
    p.params = {**(p.params or {}), key: {k: v for k, v in note.items() if v is not None}}


def _flag_human(
    session: Session, client_id: str, p: Proposal, cause: str, now: dt.datetime, **note: Any
) -> None:
    _annotate(p, "triage", {"outcome": "flag_human", "cause": cause, "at": now.isoformat(), **note})
    raise_alert(
        session,
        client_id,
        dedupe_key=f"flag_human:escalation:{p.id}",
        severity=Severity.WARN,
        kind="flag_human",
        entity_type=EntityType(p.entity_type),
        entity_id=int(p.entity_id),
        message=(
            f"Escalation #{p.id} ({p.rule_key}) on {EntityType(p.entity_type).value} "
            f"{p.entity_id} needs a human: {cause}"
        ),
    )


# --------------------------------------------------------------------------- daily triage


def _pending_escalations(session: Session, client_id: str, as_of: dt.date) -> list[Proposal]:
    rows = session.scalars(
        select(Proposal)
        .where(
            Proposal.client_id == client_id,
            Proposal.status == ProposalStatus.ESCALATED.value,
            Proposal.as_of == as_of,
        )
        .order_by(Proposal.id)
        .with_for_update()
    ).all()
    return [p for p in rows if "triage" not in (p.params or {})]


@dataclass
class _Run:
    """State of one triage step run."""

    session: Session
    ctx: JobContext
    gw: LLMGateway
    client: dict[str, Any]
    now: dt.datetime
    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def flag(self, p: Proposal, cause: str, **note: Any) -> None:
        _flag_human(self.session, self.ctx.client_id, p, cause, self.now, **note)
        self.bump("flag_human")

    def apply(self, case: Case, v: Verdict, res: CallResult, tier: str) -> None:
        p = case.escalation
        note: dict[str, Any] = {
            "at": self.now.isoformat(),
            "tier": tier,
            "llm_call_id": res.llm_call_id,
            "cache_hit": res.status == "cache_hit" or None,
            "action": v.decision.action.value if v.decision else None,
            "confidence": v.decision.confidence if v.decision else None,
            "reason": v.decision.reason if v.decision else None,
        }
        if v.action is LLMAction.FLAG_HUMAN:
            cause = v.error or (v.decision.reason if v.decision else "model asked for a human")
            self.flag(p, f"LLM {tier}: {cause}", **note)
            return
        if v.action is LLMAction.NOOP:
            _annotate(p, "triage", {"outcome": "noop", **note})
            self.bump("noop")
            return
        source = ProposalSource.LLM_TRIAGE if tier == "triage" else ProposalSource.LLM_ANALYST
        draft = proposal_from(case, v, source=source, llm_call_id=res.llm_call_id)
        assert draft is not None
        new_id = insert_proposal(self.session, self.ctx.client_id, draft)
        _annotate(p, "triage", {"outcome": "proposal", "proposal_id": new_id, **note})
        self.bump("proposal")

    def batches(self, cases: list[Case], purpose: str) -> list[Case]:
        """Run ``purpose`` over ``cases``; returns the cases escalated to the analyst."""
        escalate: list[Case] = []
        analyst = purpose == "analyst"
        for batch in _chunks(cases):
            res = self.gw.call(build_request(purpose, self.client, list(batch)))
            self.bump(f"{purpose}_calls:{res.status}")
            if not res.has_text:
                cause = (
                    f"LLM budget cap reached ({res.detail})"
                    if res.status == "skipped_budget"
                    else f"LLM {purpose} unavailable ({res.status}: {res.detail})"
                )
                for c in batch:
                    self.flag(c.escalation, cause, tier=purpose)
                continue
            verdicts, problems = parse_verdicts(res.text, list(batch), analyst=analyst)
            if problems:
                self.gw.mark_invalid(res.llm_call_id, "; ".join(problems))
            for c in batch:
                v = verdicts[c.case_id]
                if v.action is LLMAction.ESCALATE_LARGE and not analyst:
                    escalate.append(c)
                else:
                    self.apply(c, v, res, purpose)
        return escalate


def triage_step(ctx: JobContext, session: Session) -> StepOutcome:
    as_of = as_of_for(ctx)
    now = ctx.now()
    rules = active_rules(session, ctx.client_id)
    fatigue = _fatigue_rules(rules)
    escalations = [
        p for p in _pending_escalations(session, ctx.client_id, as_of) if p.rule_key not in fatigue
    ]
    if not escalations:
        return StepOutcome(rows=0, details={"escalations": 0})
    llm, why = client_factory(ctx.settings)
    if llm is None:
        for p in escalations:
            _flag_human(session, ctx.client_id, p, f"LLM disabled: {why}", now)
        session.flush()
        n = len(escalations)
        return StepOutcome(details={"escalations": n, "llm": "disabled", "flag_human": n})
    universe = load_universe(session, ctx.client_id)
    feats = _features_dict(session, ctx.client_id, as_of)
    scores = _scores_dict(session, ctx.client_id, as_of)
    gw = LLMGateway(
        ctx.settings,
        llm,
        engine=ctx.engine,
        sessions=ctx.sessions,
        clock=ctx.clock,
        job="daily_pipeline",
    )
    run = _Run(session, ctx, gw, client_block(rules.targets), now)
    cases, missing = build_cases(session, ctx.client_id, escalations, universe, feats, scores)
    for p, cause in missing:
        run.flag(p, cause)
    to_analyst = run.batches(cases, "triage")
    if to_analyst:
        a_cases, a_missing = build_cases(
            session,
            ctx.client_id,
            [c.escalation for c in to_analyst],
            universe,
            feats,
            scores,
            triage=False,
            with_parent=True,
        )
        for p, cause in a_missing:  # pragma: no cover - same inputs as the triage cases
            run.flag(p, cause)
        run.batches(a_cases, "analyst")
    session.flush()
    return StepOutcome(
        rows=run.counts.get("proposal", 0),
        details={"escalations": len(escalations), "analyst_cases": len(to_analyst), **run.counts},
    )


# --------------------------------------------------------------------------- weekly creative


def _fatigue_escalations(
    session: Session, client_id: str, keys: set[str], as_of: dt.date
) -> dict[int, list[Proposal]]:
    rows = session.scalars(
        select(Proposal)
        .where(
            Proposal.client_id == client_id,
            Proposal.status == ProposalStatus.ESCALATED.value,
            Proposal.rule_key.in_(sorted(keys)),
            Proposal.entity_type == EntityType.AD,
            Proposal.as_of > as_of - dt.timedelta(days=CREATIVE_LOOKBACK_DAYS),
            Proposal.as_of <= as_of,
        )
        .order_by(Proposal.id)
        .with_for_update()
    ).all()
    by_ad: dict[int, list[Proposal]] = {}
    for p in rows:
        if "creative" not in (p.params or {}):
            by_ad.setdefault(int(p.entity_id), []).append(p)
    return by_ad


def _creative_cases(
    session: Session, client_id: str, by_ad: dict[int, list[Proposal]], note: dict[str, Any]
) -> list[cr.CreativeCase]:
    universe = load_universe(session, client_id)
    feats = _features_dict(session, client_id, max(p.as_of for ps in by_ad.values() for p in ps))
    ads = {
        a.id: a
        for a in session.scalars(select(Ad).where(Ad.client_id == client_id, Ad.id.in_(by_ad)))
    }
    cases: list[cr.CreativeCase] = []
    for ad_id, escs in sorted(by_ad.items()):
        key = (EntityType.AD, ad_id)
        e = universe.entities.get(key)
        why = cr.skip_reason(e) or (None if key in feats else "no features for the ad")
        if why is not None or e is None:
            for p in escs:
                _annotate(p, "creative", {**note, "outcome": "skipped", "cause": why})
            continue
        cases.append(cr.build_case(e, ads.get(ad_id), feats[key], escs))
    return cases


def _creative_disabled(
    session: Session,
    ctx: JobContext,
    *,
    as_of: dt.date,
    cases: list[cr.CreativeCase],
    why: str,
    note: dict[str, Any],
) -> None:
    for c in cases:
        for p in c.escalations:
            cause = f"LLM disabled: {why}"
            _annotate(p, "creative", {**note, "outcome": "flag_human", "cause": cause})
    if cases:
        raise_alert(
            session,
            ctx.client_id,
            dedupe_key=f"creative_refresh_manual:{as_of.isoformat()}",
            severity=Severity.INFO,
            kind="flag_human",
            message=f"{len(cases)} fatigued ad(s) need new creative; LLM disabled ({why})",
        )


def _creative_batch(
    session: Session,
    ctx: JobContext,
    *,
    gw: LLMGateway,
    req_client: dict[str, Any],
    batch: list[cr.CreativeCase],
    note: dict[str, Any],
    counts: dict[str, int],
) -> None:
    res = gw.call(cr.build_request(req_client, batch))
    counts[f"calls:{res.status}"] = counts.get(f"calls:{res.status}", 0) + 1
    drafts, problems = cr.parse_drafts(res.text, batch)
    if problems and res.has_text:
        gw.mark_invalid(res.llm_call_id, "; ".join(problems))
    for draft in cr.proposals_by_group(batch, drafts, res.llm_call_id):
        pid = insert_proposal(session, ctx.client_id, draft)
        counts["proposals"] += pid is not None
        done = {"outcome": "proposal", "proposal_id": pid, "llm_call_id": res.llm_call_id}
        for c in batch:
            if c.case_id in drafts and c.ad.ad_group_id == draft.entity_id:
                for p in c.escalations:
                    _annotate(p, "creative", {**note, **done})
    for c in batch:
        if c.case_id in drafts:
            continue
        cause = f"{res.status}: {res.detail if not res.has_text else 'no valid draft'}"
        for p in c.escalations:
            _annotate(p, "creative", {**note, "outcome": "flag_human", "cause": cause})
        counts["flag_human"] += 1


def creative_step(ctx: JobContext, as_of: dt.date, session: Session) -> StepOutcome:
    rules = active_rules(session, ctx.client_id)
    fatigue = _fatigue_rules(rules)
    if not fatigue:
        return StepOutcome(details={"skipped": "no fatigue rule in the active rules"})
    by_ad = _fatigue_escalations(session, ctx.client_id, fatigue, as_of)
    if not by_ad:
        return StepOutcome(details={"fatigued_ads": 0})
    note: dict[str, Any] = {"at": ctx.now().isoformat()}
    cases = _creative_cases(session, ctx.client_id, by_ad, note)
    llm, why_off = client_factory(ctx.settings)
    if llm is None:
        _creative_disabled(session, ctx, as_of=as_of, cases=cases, why=why_off, note=note)
        return StepOutcome(details={"fatigued_ads": len(by_ad), "llm": "disabled"})
    gw = LLMGateway(
        ctx.settings,
        llm,
        engine=ctx.engine,
        sessions=ctx.sessions,
        clock=ctx.clock,
        job="weekly_strategy",
    )
    client = client_block(rules.targets)
    counts: dict[str, int] = {"proposals": 0, "flag_human": 0}
    for batch in _chunks(cases):
        _creative_batch(
            session, ctx, gw=gw, req_client=client, batch=list(batch), note=note, counts=counts
        )
    session.flush()
    return StepOutcome(
        rows=counts["proposals"],
        details={"fatigued_ads": len(by_ad), "cases": len(cases), **counts},
    )
