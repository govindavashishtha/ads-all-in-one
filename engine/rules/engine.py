"""Rules engine (plan §11): registry variables + ``simpleeval`` expressions, first match by
priority wins. Outputs ``noop`` (log only), a proposal, or ``escalate``.

Semantics (§11.1):
- an expression that raises (``None`` comparisons, unknown name for this entity type, …)
  evaluates to **false** for that entity (debug log, not an error);
- rules are sorted by ``priority`` ascending (then key, for determinism); for each entity the
  first rule whose ``applies_to`` matches and whose ``when`` is true wins;
- numbers are passed to expressions as ``float`` (comparisons only — no money is computed
  here), ``bool`` stays ``bool``, ``None`` stays ``None``.
"""

from __future__ import annotations

import ast
import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Any

import structlog
from simpleeval import SimpleEval

from engine.enums import Action, EntityType, ProposalSource, ProposalStatus, Risk
from engine.features.data import EntityInfo
from engine.proposals import ProposalDraft, change_pct_of
from engine.rules.registry import names_from
from engine.rules.schema import Rule, RulesConfig, Targets, expression_names

log = structlog.get_logger(__name__)

FUNCTIONS = {"min": min, "max": max, "abs": abs}  # §5: only these
SCORE_VARS = names_from("scores")


def _num(x: Any) -> Any:
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, Decimal | int):
        return float(x)
    return x


def target_vars(t: Targets) -> dict[str, Any]:
    return {
        "objective": t.objective,
        "target_cpa": float(t.target_cpa),
        "target_roas": float(t.target_roas) if t.target_roas is not None else None,
    }


def entity_vars(
    e: EntityInfo,
    features: Mapping[str, Any] | None,
    score: Mapping[str, Any] | None,
    targets: Targets,
) -> dict[str, Any]:
    """Registry variables for one entity. Unscored entities have ``data_sufficient=False``."""
    names: dict[str, Any] = {
        "platform": e.platform.value,
        "entity_type": e.entity_type.value,
        "campaign_kind": e.campaign_kind,
        **target_vars(targets),
    }
    for k, v in (features or {}).items():
        names[k] = _num(v)
    for k in SCORE_VARS:
        names[k] = _num((score or {}).get(k))
    names["data_sufficient"] = bool((score or {}).get("data_sufficient", False))
    names["learning"] = e.learning  # NULL -> true (§8.2)
    return names


@lru_cache(maxsize=512)
def _parsed(expr: str) -> ast.Expr:
    parsed: ast.Expr = SimpleEval().parse(expr)
    return parsed


def evaluate(expr: str, names: Mapping[str, Any]) -> bool:
    """Evaluate one ``when`` expression; any exception → False (§11.1)."""
    evaluator = SimpleEval(names=dict(names), functions=FUNCTIONS)
    try:
        return bool(evaluator.eval(expr, previously_parsed=_parsed(expr)))
    except Exception as exc:  # §11.1: raising means false, not an error
        log.debug("rule.eval_false", expr=expr, error=f"{type(exc).__name__}: {exc}")
        return False


def ordered(rules: Iterable[Rule]) -> list[Rule]:
    return sorted(rules, key=lambda r: (r.priority, r.key))


@dataclass(frozen=True, slots=True)
class Match:
    rule: Rule
    entity_type: EntityType
    entity_id: int
    names: dict[str, Any]


def first_match(
    rules: list[Rule], entity_type: EntityType, entity_id: int, names: dict[str, Any]
) -> Match | None:
    for rule in rules:
        if entity_type in rule.applies_to and evaluate(rule.when, names):
            return Match(rule, entity_type, entity_id, names)
    return None


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4g}" if abs(v) < 1 else f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def explain(rule: Rule, names: Mapping[str, Any]) -> str:
    """Reason text: the rule key, its expression and the values it saw (no free text)."""
    used = sorted(expression_names(rule.when) & set(names))
    values = ", ".join(f"{n}={_fmt(names[n])}" for n in used)
    tag = f" [{rule.reason}]" if rule.reason else ""
    return f"{rule.key}{tag}: {rule.when} — {values}" if values else f"{rule.key}{tag}"


def draft_for(
    match: Match, as_of: dt.date, current_budget: Decimal | None, extra_params: dict[str, Any]
) -> ProposalDraft | None:
    """Turn a match into a proposal draft. ``noop`` → None (log only, 0 tokens)."""
    rule = match.rule
    if rule.action is Action.NOOP:
        return None
    params: dict[str, Any] = {**(rule.params or {}), **extra_params}
    names = match.names
    impact: dict[str, Any] = {}
    if rule.action is Action.BUDGET_CHANGE:
        pct = change_pct_of(params)
        if current_budget is not None and pct is not None:
            impact = {
                "daily_budget_before": str(current_budget),
                "change_pct": str(pct),
                "daily_budget_delta_est": str(
                    (current_budget * pct / 100).quantize(Decimal("0.01"))
                ),
            }
    elif rule.action is Action.PAUSE and names.get("spend_7d") is not None:
        impact = {
            "avg_daily_spend_7d": round(float(names["spend_7d"]) / 7, 2),
            "conversions_7d": names.get("conversions_7d"),
        }
    if rule.action is Action.ESCALATE:
        status = ProposalStatus.ESCALATED
        risk = rule.risk or Risk.MEDIUM
    else:
        status = ProposalStatus.PENDING
        risk = rule.risk or Risk.MEDIUM  # DECISIONS D-7: unspecified risk → medium
    return ProposalDraft(
        as_of=as_of,
        source=ProposalSource.RULE,
        rule_key=rule.key,
        entity_type=match.entity_type,
        entity_id=match.entity_id,
        action=rule.action,
        risk=risk,
        reason=explain(rule, names),
        params=params,
        expected_impact=impact or None,
        status=status,
    )


@dataclass
class RulesRun:
    matches: list[Match]
    drafts: list[ProposalDraft]

    def by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.matches:
            out[m.rule.key] = out.get(m.rule.key, 0) + 1
        return dict(sorted(out.items()))


def run_rules(
    config: RulesConfig,
    entities: Iterable[EntityInfo],
    features: Mapping[tuple[EntityType, int], Mapping[str, Any]],
    scores: Mapping[tuple[EntityType, int], Mapping[str, Any]],
    as_of: dt.date,
) -> RulesRun:
    """Evaluate every effectively-active entity that has features (DECISIONS D-4)."""
    rules = ordered(config.rules)
    run = RulesRun([], [])
    for e in entities:
        if not e.effective_active or e.key not in features:
            continue
        names = entity_vars(e, features[e.key], scores.get(e.key), config.targets)
        m = first_match(rules, e.entity_type, e.id, names)
        if m is None:
            continue
        run.matches.append(m)
        d = draft_for(m, as_of, e.daily_budget, {})
        if d is not None:
            run.drafts.append(d)
    return run
