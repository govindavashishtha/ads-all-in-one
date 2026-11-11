"""Triage (small model) and analyst (large model) — plan §13.1, §13.2, §11 escalations.

Flow per batch of ≤ 20 cases: request → strict parse → per-case whitelist check → either a
``ProposalDraft`` (source ``llm_triage`` / ``llm_analyst``, risk ``medium``, status ``pending``
so the guardrail step judges it like any other proposal), ``noop``, ``escalate_large`` (triage
only → analyst) or ``flag_human``. Anything invalid becomes ``flag_human`` (§13.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from engine.enums import Action, LLMTier, ProposalSource, Risk
from engine.llm.client import LLMMessage, LLMRequest
from engine.llm.schemas import AnalystDecision, CaseDecision, LLMAction
from engine.llm.summariser import Case, request_json
from engine.proposals import ProposalDraft

PROMPTS = Path(__file__).resolve().parent / "prompts"
MIN_ACTION_CONFIDENCE = 0.5  # DECISIONS D-23: below this a pause/budget move goes to a human
OUT_TOKENS_PER_CASE = {"triage": 120, "analyst": 260}
OUT_TOKENS_BASE = 64


@cache
def system_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8").strip()


def build_request(purpose: str, client: dict[str, Any], cases: list[Case]) -> LLMRequest:
    tier = LLMTier.SMALL if purpose == "triage" else LLMTier.LARGE
    return LLMRequest(
        tier=tier,
        purpose=purpose,
        system=system_prompt(purpose),
        messages=(LLMMessage("user", request_json(client, cases)),),
        max_output_tokens=OUT_TOKENS_BASE + OUT_TOKENS_PER_CASE[purpose] * len(cases),
        temperature=0.0,
        json_output=True,
    )


@dataclass(frozen=True)
class Verdict:
    """Validated outcome for one case."""

    action: LLMAction  # flag_human when invalid
    decision: CaseDecision | None
    error: str | None = None  # why it became flag_human

    @property
    def valid(self) -> bool:
        return self.error is None


def _extract_json(text: str) -> Any:
    """The whole output must be ONE JSON object (code fences tolerated, nothing else)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    return json.loads(t)


def parse_verdicts(
    text: str | None, cases: list[Case], *, analyst: bool
) -> tuple[dict[str, Verdict], list[str]]:
    """Return a verdict per case id and a list of problems (any problem = not cacheable)."""
    model: type[BaseModel] = AnalystDecision if analyst else CaseDecision
    flag = LLMAction.FLAG_HUMAN
    problems: list[str] = []
    by_id = {c.case_id: c for c in cases}
    if text is None:
        return {cid: Verdict(flag, None, "no LLM output") for cid in by_id}, ["no output"]
    try:
        doc = _extract_json(text)
    except (json.JSONDecodeError, ValueError):
        return {cid: Verdict(flag, None, "output is not JSON") for cid in by_id}, ["not json"]
    if not isinstance(doc, dict) or set(doc) != {"results"} or not isinstance(doc["results"], list):
        return {cid: Verdict(flag, None, "output schema violated") for cid in by_id}, ["schema"]
    seen: dict[str, Verdict] = {}
    dupes: set[str] = set()
    for item in doc["results"][: len(cases) * 2]:
        try:
            d = model.model_validate_json(json.dumps(item))  # strict JSON mode
        except ValidationError as exc:
            problems.append(f"item invalid: {exc.errors()[0].get('msg', '?')}")
            if isinstance(item, dict) and item.get("case_id") in by_id:
                seen.setdefault(item["case_id"], Verdict(flag, None, "result failed validation"))
            continue
        assert isinstance(d, CaseDecision)
        case = by_id.get(d.case_id)
        if case is None:
            problems.append(f"unknown case_id {d.case_id!r}")
            continue
        if d.case_id in seen:
            dupes.add(d.case_id)
            continue
        seen[d.case_id] = _check(d, case)
        if not seen[d.case_id].valid:
            problems.append(f"{d.case_id}: {seen[d.case_id].error}")
    for cid in dupes:
        seen[cid] = Verdict(flag, None, "several results for one case")
        problems.append(f"{cid}: duplicate")
    out = {cid: seen.get(cid, Verdict(flag, None, "no result for this case")) for cid in by_id}
    if len(seen) < len(by_id):
        problems.append("missing results")
    return out, problems


def _check(d: CaseDecision, case: Case) -> Verdict:
    flag = LLMAction.FLAG_HUMAN
    if d.action.value not in case.allowed:
        return Verdict(flag, d, f"action {d.action.value!r} not in allowed_actions")
    if d.action is LLMAction.BUDGET_CHANGE and not d.change_pct:
        return Verdict(flag, d, "budget_change without a non-zero integer change_pct")
    if d.action in (LLMAction.PAUSE, LLMAction.BUDGET_CHANGE) and (
        d.confidence < MIN_ACTION_CONFIDENCE
    ):
        return Verdict(flag, d, f"confidence {d.confidence:.2f} < {MIN_ACTION_CONFIDENCE}")
    return Verdict(d.action, d)


def proposal_from(
    case: Case, verdict: Verdict, *, source: ProposalSource, llm_call_id: int | None
) -> ProposalDraft | None:
    """Engine-built proposal: entity, as_of and params come from the CASE, never from the
    model; only the action class and the bounded integer ``change_pct`` come from the model."""
    d = verdict.decision
    if d is None or verdict.action not in (LLMAction.PAUSE, LLMAction.BUDGET_CHANGE):
        return None
    esc = case.escalation
    e = case.entity
    params: dict[str, Any] = {"escalation_id": int(esc.id), "llm_call_id": llm_call_id}
    impact: dict[str, Any] | None = None
    if verdict.action is LLMAction.BUDGET_CHANGE:
        assert d.change_pct is not None
        params["change_pct"] = int(d.change_pct)
        action = Action.BUDGET_CHANGE
        if e.daily_budget is not None:
            pct = Decimal(d.change_pct)
            impact = {
                "daily_budget_before": str(e.daily_budget),
                "change_pct": str(pct),
                "daily_budget_delta_est": str(
                    (e.daily_budget * pct / 100).quantize(Decimal("0.01"))
                ),
            }
    else:
        action = Action.PAUSE
    tier = "triage" if source is ProposalSource.LLM_TRIAGE else "analyst"
    extra = ""
    if isinstance(d, AnalystDecision):
        extra = f" Root cause: {d.root_cause}"
    reason = (
        f"LLM {tier} on escalation #{esc.id} ({esc.rule_key}): {d.reason}{extra} "
        f"[confidence {d.confidence:.2f}]"
    )
    return ProposalDraft(
        as_of=esc.as_of,
        source=source,
        rule_key=None,
        entity_type=e.entity_type,
        entity_id=e.id,
        action=action,
        risk=Risk.MEDIUM,  # §13.2: LLM proposals are medium risk
        reason=reason,
        params=params,
        expected_impact=impact,
    )
