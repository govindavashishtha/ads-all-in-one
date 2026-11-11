"""Compact-JSON case builder (plan §13.2, §13.3): aggregated, rounded numbers with short keys;
never raw metric rows, never account/platform ids, PII or tokens (§20).

A case holds one escalated entity: its rule reason, entity facts, ≤ 3 metric windows, its
score, its recent engine changes and its peer rank, plus ``allowed_actions`` — the engine-side
whitelist for THIS entity (the model may only pick from it; the parser re-checks it).
Free text from the platforms (names, ad copy) appears only under ``untrusted_text``.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.db.models import ChangeLog, Proposal
from engine.enums import AutomationScope, BudgetLevel, ChangeStatus, EntityType
from engine.features.data import EntityInfo, EntityKey, Universe
from engine.llm.schemas import LLMAction
from engine.llm.untrusted import untrusted_block
from engine.rules.schema import Targets

RECENT_CHANGE_DAYS = 14
MAX_RECENT_CHANGES = 5


def _num(v: Any, kind: str = "money") -> float | int | None:
    """Rounded numbers (§13.3): money ≥ 100 → integer, else 2 dp; ratios → 4 significant."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    rounders: dict[str, Callable[[float], float | int]] = {
        "ratio": lambda y: float(f"{y:.4g}"),
        "p": lambda y: round(y, 2),
        "count": lambda y: int(y) if y == int(y) else round(y, 1),
    }
    if kind in rounders:
        return rounders[kind](x)
    return round(x) if abs(x) >= 100 else round(x, 2)


def _compact(d: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, raw in d.items():
        v = _compact(raw) if isinstance(raw, Mapping) else raw
        if v is None or (isinstance(v, (list, dict)) and not v):
            continue
        out[k] = v
    return out


def client_block(targets: Targets) -> dict[str, Any]:
    return _compact(
        {
            "objective": targets.objective,
            "target_cpa": _num(targets.target_cpa),
            "target_roas": _num(targets.target_roas, "p") if targets.objective == "value" else None,
            "currency": targets.currency,
        }
    )


def allowed_actions(e: EntityInfo, *, triage: bool) -> list[str]:
    """Engine-side action whitelist for one entity (§13.2). The guardrail still runs after."""
    acts = [LLMAction.NOOP]
    scope_ok = e.automation_scope != AutomationScope.CAMPAIGN_ONLY.value or (
        e.entity_type is EntityType.CAMPAIGN
    )
    learning_protected = e.learning and e.entity_type in (EntityType.AD_GROUP, EntityType.CAMPAIGN)
    if scope_ok and e.campaign_managed and not learning_protected:
        if e.entity_type in (EntityType.AD, EntityType.AD_GROUP, EntityType.CAMPAIGN):
            acts.append(LLMAction.PAUSE)
        if e.is_budget_unit and e.budget_level != BudgetLevel.SHARED.value:
            acts.append(LLMAction.BUDGET_CHANGE)
    if triage:
        acts.append(LLMAction.ESCALATE_LARGE)
    acts.append(LLMAction.FLAG_HUMAN)
    return [a.value for a in acts]


@dataclass
class Case:
    """One case: the JSON the model sees plus the engine-side context it never sees."""

    case_id: str
    escalation: Proposal
    entity: EntityInfo
    payload: dict[str, Any]
    allowed: list[str]
    extra: dict[str, Any] = field(default_factory=dict)

    def size(self) -> int:
        return len(json.dumps(self.payload, separators=(",", ":")))


def _metrics(f: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "d1": {
            "spend": _num(f.get("spend_1d")),
            "conv": _num(f.get("conversions_1d"), "count"),
            "cpa": _num(f.get("cpa_1d")),
            "ctr": _num(f.get("ctr_1d"), "ratio"),
            "impr": _num(f.get("impressions_1d"), "count"),
        },
        "d7": {
            "spend": _num(f.get("spend_7d")),
            "conv": _num(f.get("conversions_7d"), "count"),
            "cpa": _num(f.get("cpa_7d")),
            "ctr": _num(f.get("ctr_7d"), "ratio"),
            "roas": _num(f.get("roas_7d"), "p"),
            "freq": _num(f.get("frequency_7d"), "p"),
            "pace": _num(f.get("spend_pace_7d"), "p"),
        },
        "d28": {
            "cpa": _num(f.get("cpa_28d")),
            "ctr": _num(f.get("ctr_28d"), "ratio"),
            "roas": _num(f.get("roas_28d"), "p"),
        },
    }


def recent_changes(
    session: Session, client_id: str, keys: list[EntityKey], as_of: dt.date
) -> dict[EntityKey, list[dict[str, Any]]]:
    if not keys:
        return {}
    since = dt.datetime.combine(
        as_of - dt.timedelta(days=RECENT_CHANGE_DAYS), dt.time(), tzinfo=dt.UTC
    )
    rows = session.execute(
        select(ChangeLog, Proposal.params)
        .outerjoin(Proposal, Proposal.id == ChangeLog.proposal_id)
        .where(
            ChangeLog.client_id == client_id,
            ChangeLog.applied_at >= since,
            ChangeLog.status.in_([ChangeStatus.APPLIED.value, ChangeStatus.ROLLED_BACK.value]),
        )
        .order_by(ChangeLog.applied_at.desc())
    ).all()
    wanted = set(keys)
    out: dict[EntityKey, list[dict[str, Any]]] = {}
    for cl, params in rows:
        k = (EntityType(cl.entity_type), int(cl.entity_id))
        if k not in wanted or len(out.get(k, [])) >= MAX_RECENT_CHANGES:
            continue
        pct = (params or {}).get("change_pct")
        out.setdefault(k, []).append(
            _compact(
                {
                    "at": cl.applied_at.date().isoformat() if cl.applied_at else None,
                    "action": cl.action,
                    "pct": _num(pct, "p"),
                    "rolled_back": cl.status == ChangeStatus.ROLLED_BACK.value or None,
                }
            )
        )
    return out


def _parent_key(e: EntityInfo) -> EntityKey | None:
    if e.entity_type is EntityType.AD and e.ad_group_id is not None:
        return (EntityType.AD_GROUP, e.ad_group_id)
    if e.entity_type is EntityType.AD_GROUP:
        return (EntityType.CAMPAIGN, e.campaign_id)
    return None


def case_for(
    p: Proposal,
    e: EntityInfo,
    f: Mapping[str, Any],
    s: Mapping[str, Any] | None,
    *,
    changes: list[dict[str, Any]] | None = None,
    triage: bool = True,
    parent: tuple[EntityType, Mapping[str, Any]] | None = None,
) -> Case:
    """Pure: one escalation → one compact case."""
    s = s or {}
    allowed = allowed_actions(e, triage=triage)
    payload: dict[str, Any] = {
        "id": f"esc_{p.id}",
        "reason": p.rule_key or "escalation",
        "entity": {
            "platform": e.platform.value,
            "type": e.entity_type.value,
            "learning": e.learning,
            "budget_unit": e.is_budget_unit,
            "campaign_kind": e.campaign_kind,
            "days_live": _num(f.get("days_live"), "count"),
            "active_ads_in_group": _num(f.get("active_ads_in_group"), "count"),
        },
        "untrusted_text": untrusted_block(entity_name=e.name),
        "metrics": _metrics(f),
        "score": {
            "p_beats_target": _num(s.get("p_beats_target"), "p"),
            "cpa_shrunk": _num(s.get("cpa_shrunk")),
            "cpa_p90": _num(s.get("cpa_p90")),
            "data_sufficient": s.get("data_sufficient"),
        },
        "recent_changes": changes or [],
        "peer_context": {"rank": s.get("rank_in_peer"), "of": s.get("peer_size")},
    }
    if parent is not None:
        pm = _metrics(parent[1])
        payload["parent"] = {"type": parent[0].value, "d7": pm["d7"], "d28": pm["d28"]}
    payload = _compact(payload)
    payload["allowed_actions"] = allowed  # never compacted away
    return Case(f"esc_{p.id}", p, e, payload, allowed)


def build_cases(  # noqa: PLR0917
    session: Session,
    client_id: str,
    escalations: list[Proposal],
    universe: Universe,
    features: Mapping[EntityKey, Mapping[str, Any]],
    scores: Mapping[EntityKey, Mapping[str, Any]],
    *,
    triage: bool = True,
    with_parent: bool = False,
) -> tuple[list[Case], list[tuple[Proposal, str]]]:
    """Cases for escalations whose entity still exists; the rest are returned with a cause."""
    cases: list[Case] = []
    missing: list[tuple[Proposal, str]] = []
    keys = [(EntityType(p.entity_type), int(p.entity_id)) for p in escalations]
    as_of = max((p.as_of for p in escalations), default=dt.date.min)
    changes = recent_changes(session, client_id, keys, as_of)
    for p, key in zip(escalations, keys, strict=True):
        e = universe.entities.get(key)
        f = features.get(key)
        if e is None or f is None:
            missing.append((p, "entity or its features not found"))
            continue
        parent = None
        pk = _parent_key(e) if with_parent else None
        if pk is not None and pk in features:
            parent = (pk[0], features[pk])
        cases.append(
            case_for(
                p, e, f, scores.get(key), changes=changes.get(key), triage=triage, parent=parent
            )
        )
    return cases, missing


def request_json(client: Mapping[str, Any], cases: list[Case], **extra: Any) -> str:
    """One request body: client block + array of cases (§13.3 batching), compact separators."""
    body: dict[str, Any] = {"client": dict(client), **extra, "cases": [c.payload for c in cases]}
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, dt.date):
        return o.isoformat()
    raise TypeError(type(o).__name__)
