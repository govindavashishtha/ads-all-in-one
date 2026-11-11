"""Turn an approved proposal into concrete platform write operations (plan §15 steps 1–2).

PURE planning: reads entity rows from the DB to resolve external ids, never calls a platform
and never imports a write connector, so the API can use it for ``GET /proposals/{id}/dry-run``
(§17) and the executor for the real thing.

Tracked-field dictionaries (``before`` / ``intended`` / ``after`` in ``change_log``) use the
same keys the hourly sync compares for manual-change detection (``engine.ingest.entities``):
``status`` (``EntityStatus`` value) and ``daily_budget`` (decimal string, major units).
Creation ops track ``negative`` / ``ad`` instead. ``intended['_op']`` carries the write
target so the reconciler can re-read state after a crash; keys starting with ``_`` are
metadata and never compared.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.connectors.base import EntityState, EntityStatus
from engine.db.models import Ad, AdGroup, Campaign, Keyword, Proposal, SearchTerm
from engine.enums import Action, EntityType, Platform


class PlanError(ValueError):
    """The proposal cannot be turned into a platform write (→ proposal failed)."""


class OpKind(StrEnum):
    SET_STATUS = "set_status"
    SET_DAILY_BUDGET = "set_daily_budget"
    ADD_NEGATIVE = "add_negative_keyword"
    REMOVE_NEGATIVE = "remove_negative_keyword"
    CREATE_AD = "create_ad"


CREATE_OPS = frozenset({OpKind.ADD_NEGATIVE, OpKind.CREATE_AD})
_ENTITY_MODELS: dict[EntityType, Any] = {
    EntityType.CAMPAIGN: Campaign,
    EntityType.AD_GROUP: AdGroup,
    EntityType.AD: Ad,
    EntityType.KEYWORD: Keyword,
}


@dataclass(frozen=True, slots=True)
class WriteOp:
    """One platform write. ``entity_*`` = what the write targets on the platform;
    ``log_entity_*`` = the internal entity the ``change_log`` row is filed under."""

    kind: OpKind
    action: Action
    platform: Platform
    entity_type: EntityType
    external_id: str
    log_entity_type: EntityType
    log_entity_id: int
    intended: dict[str, Any]
    args: dict[str, Any] = field(default_factory=dict)
    index: int = 0

    def intended_json(self) -> dict[str, Any]:
        return {**self.intended, "_op": self.target_json()}

    def target_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "action": self.action.value,
            "platform": self.platform.value,
            "entity_type": self.entity_type.value,
            "external_id": self.external_id,
            "args": self.args,
            "index": self.index,
        }

    @classmethod
    def from_change(
        cls, intended: dict[str, Any], log_entity_type: EntityType, log_entity_id: int
    ) -> WriteOp:
        t = intended["_op"]
        return cls(
            kind=OpKind(t["kind"]),
            action=Action(t["action"]),
            platform=Platform(t["platform"]),
            entity_type=EntityType(t["entity_type"]),
            external_id=str(t["external_id"]),
            log_entity_type=log_entity_type,
            log_entity_id=log_entity_id,
            intended={k: v for k, v in intended.items() if not k.startswith("_")},
            args=dict(t.get("args") or {}),
            index=int(t.get("index", 0)),
        )


# ----------------------------------------------------------------------------- tracked fields


def money_str(x: Decimal | None) -> str | None:
    return None if x is None else str(Decimal(x).quantize(Decimal("0.01")))


def tracked(state: EntityState) -> dict[str, Any]:
    return {"status": state.status.value, "daily_budget": money_str(state.daily_budget)}


def public(d: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (d or {}).items() if not k.startswith("_")}


def matches(state: dict[str, Any] | None, target: dict[str, Any] | None) -> bool:
    """True when every (non-metadata) field of ``target`` equals ``state``'s value."""
    if state is None or target is None:
        return False
    for k, want in public(target).items():
        have = state.get(k)
        if k == "daily_budget" and want is not None and have is not None:
            if Decimal(str(have)) != Decimal(str(want)):
                return False
        elif have != want:
            return False
    return True


def idempotency_key(proposal_id: int, op: WriteOp) -> str:
    """D-27: a proposal's op is written at most once, whatever happens to the proposal."""
    body = json.dumps(
        {
            "p": proposal_id,
            "i": op.index,
            "k": op.kind.value,
            "t": op.target_json(),
            "w": op.intended,
        },
        sort_keys=True,
        default=str,
    )
    return "proposal:" + hashlib.sha256(body.encode()).hexdigest()[:32]


# ----------------------------------------------------------------------------- resolution


def entity_ref(
    session: Session, client_id: str, entity_type: EntityType, entity_id: int
) -> tuple[Platform, str]:
    model = _ENTITY_MODELS.get(entity_type)
    if model is None:
        raise PlanError(f"{entity_type.value} entities cannot be written")
    row = session.execute(
        select(model.platform, model.external_id).where(
            model.client_id == client_id, model.id == entity_id
        )
    ).first()
    if row is None:
        raise PlanError(f"{entity_type.value} {entity_id} not found")
    return Platform(row[0]), str(row[1])


def _search_term_group(session: Session, client_id: str, term_id: int) -> tuple[str, int]:
    row = session.execute(
        select(SearchTerm.term, SearchTerm.ad_group_id).where(
            SearchTerm.client_id == client_id, SearchTerm.id == term_id
        )
    ).first()
    if row is None:
        raise PlanError(f"search_term {term_id} not found")
    return str(row[0]), int(row[1])


def _status_op(
    session: Session, client_id: str, p: Proposal, status: EntityStatus
) -> list[WriteOp]:
    et = EntityType(p.entity_type)
    platform, ext = entity_ref(session, client_id, et, int(p.entity_id))
    return [
        WriteOp(
            OpKind.SET_STATUS,
            Action(p.action),
            platform,
            et,
            ext,
            et,
            int(p.entity_id),
            {"status": status.value},
            {"status": status.value},
        )
    ]


def _budget_op(
    session: Session, client_id: str, p: Proposal, new_daily_budget: Decimal | None
) -> list[WriteOp]:
    if new_daily_budget is None:
        raise PlanError("budget_change without a guardrail new_daily_budget")
    et = EntityType(p.entity_type)
    if et not in (EntityType.CAMPAIGN, EntityType.AD_GROUP):
        raise PlanError(f"budget_change on {et.value} is not supported")
    platform, ext = entity_ref(session, client_id, et, int(p.entity_id))
    if platform is Platform.GOOGLE and et is not EntityType.CAMPAIGN:
        raise PlanError("Google budgets live on the campaign")
    amount = money_str(new_daily_budget)
    return [
        WriteOp(
            OpKind.SET_DAILY_BUDGET,
            Action.BUDGET_CHANGE,
            platform,
            et,
            ext,
            et,
            int(p.entity_id),
            {"daily_budget": amount},
            {"amount": amount},
        )
    ]


def _negative_op(session: Session, client_id: str, p: Proposal) -> list[WriteOp]:
    params = dict(p.params or {})
    level = params.get("level", "ad_group")
    if level != "ad_group":
        raise PlanError(f"add_negative level {level!r}: only ad_group is executed (D-31)")
    match_type = str(params.get("match_type", "exact")).lower()
    if match_type not in ("exact", "phrase", "broad"):
        raise PlanError(f"unknown match_type {match_type!r}")
    et = EntityType(p.entity_type)
    if et is EntityType.SEARCH_TERM:
        term, group_id = _search_term_group(session, client_id, int(p.entity_id))
    elif et is EntityType.AD_GROUP:
        term, group_id = str(params.get("term") or ""), int(p.entity_id)
    else:
        raise PlanError(f"add_negative on {et.value} is not supported")
    term = str(params.get("term") or term).strip()
    if not term:
        raise PlanError("add_negative without a term")
    platform, group_ext = entity_ref(session, client_id, EntityType.AD_GROUP, group_id)
    if platform is not Platform.GOOGLE:
        raise PlanError("negative keywords exist on Google only")
    return [
        WriteOp(
            OpKind.ADD_NEGATIVE,
            Action.ADD_NEGATIVE,
            platform,
            EntityType.KEYWORD,
            group_ext,  # the ad group the criterion is created in
            et,
            int(p.entity_id),
            {"negative": True, "status": EntityStatus.ACTIVE.value},
            {"ad_group_external_id": group_ext, "text": term, "match_type": match_type},
        )
    ]


def _new_ad_ops(session: Session, client_id: str, p: Proposal) -> list[WriteOp]:
    """D-31: Meta → one ad per variant; Google → one RSA per draft (variants = assets)."""
    params = dict(p.params or {})
    drafts = params.get("drafts") or []
    if not drafts:
        raise PlanError("new_ad without drafts")
    group_id = int(p.entity_id)
    if EntityType(p.entity_type) is not EntityType.AD_GROUP:
        raise PlanError("new_ad must target an ad group / ad set")
    platform, group_ext = entity_ref(session, client_id, EntityType.AD_GROUP, group_id)
    ops: list[WriteOp] = []
    for d in drafts:
        tmpl_id = d.get("replaces_ad_id")
        if tmpl_id is None:
            raise PlanError("new_ad draft without replaces_ad_id (template ad)")
        _, tmpl_ext = entity_ref(session, client_id, EntityType.AD, int(tmpl_id))
        variants = [v for v in d.get("variants") or [] if v.get("headline") or v.get("body")]
        if not variants:
            raise PlanError("new_ad draft without variants")
        if platform is Platform.META:
            for v in variants:
                ops.append(
                    _create_op(
                        p,
                        platform,
                        group_ext,
                        group_id,
                        tmpl_ext,
                        len(ops),
                        headline=v.get("headline"),
                        body=v.get("body"),
                    )
                )
        else:
            heads = [str(v["headline"]) for v in variants if v.get("headline")]
            descs = [str(v["body"]) for v in variants if v.get("body")]
            ops.append(
                _create_op(
                    p,
                    platform,
                    group_ext,
                    group_id,
                    tmpl_ext,
                    len(ops),
                    headline=None,
                    body=None,
                    headlines=heads,
                    descriptions=descs,
                )
            )
    return ops


def _create_op(  # noqa: PLR0917 (internal helper)
    p: Proposal,
    platform: Platform,
    group_ext: str,
    group_id: int,
    template_ext: str,
    index: int,
    **copy: Any,
) -> WriteOp:
    return WriteOp(
        OpKind.CREATE_AD,
        Action.NEW_AD,
        platform,
        EntityType.AD,
        group_ext,
        EntityType.AD_GROUP,
        group_id,
        {"ad": True, "status": EntityStatus.PAUSED.value},  # always created PAUSED
        {"ad_group_external_id": group_ext, "template_ad_id": template_ext, **copy},
        index,
    )


def build_plan(
    session: Session, client_id: str, p: Proposal, new_daily_budget: Decimal | None
) -> list[WriteOp]:
    """Ops for one approved proposal, in apply order. Raises PlanError."""
    action = Action(p.action)
    if action is Action.PAUSE:
        ops = _status_op(session, client_id, p, EntityStatus.PAUSED)
    elif action is Action.ENABLE:
        ops = _status_op(session, client_id, p, EntityStatus.ACTIVE)
    elif action is Action.BUDGET_CHANGE:
        ops = _budget_op(session, client_id, p, new_daily_budget)
    elif action is Action.ADD_NEGATIVE:
        ops = _negative_op(session, client_id, p)
    elif action is Action.NEW_AD:
        ops = _new_ad_ops(session, client_id, p)
    else:
        raise PlanError(f"action {action.value} is not executable in v1")
    return [_reindex(op, i) for i, op in enumerate(ops)]


def _reindex(op: WriteOp, i: int) -> WriteOp:
    if op.index == i:
        return op
    return WriteOp(
        op.kind,
        op.action,
        op.platform,
        op.entity_type,
        op.external_id,
        op.log_entity_type,
        op.log_entity_id,
        op.intended,
        op.args,
        i,
    )


def rollback_payload(op: WriteOp, before: dict[str, Any]) -> dict[str, Any]:
    """§15 step 2: the ``before`` values of exactly the changed fields (or the undo op)."""
    if op.kind is OpKind.SET_STATUS:
        return {"kind": OpKind.SET_STATUS.value, "status": before.get("status")}
    if op.kind is OpKind.SET_DAILY_BUDGET:
        return {"kind": OpKind.SET_DAILY_BUDGET.value, "daily_budget": before.get("daily_budget")}
    if op.kind is OpKind.ADD_NEGATIVE:
        # criterion id is filled in once the platform returns it
        return {
            "kind": OpKind.REMOVE_NEGATIVE.value,
            "ad_group_external_id": op.args["ad_group_external_id"],
            "criterion_external_id": None,
        }
    # new ad: rollback = make sure the created ad is paused (it was created paused)
    return {
        "kind": OpKind.SET_STATUS.value,
        "status": EntityStatus.PAUSED.value,
        "external_id": None,
    }


def describe(op: WriteOp, before: dict[str, Any] | None) -> dict[str, Any]:
    """Dry-run line (§17): what would change, without calling the platform."""
    return {
        "op": op.kind.value,
        "platform": op.platform.value,
        "entity_type": op.entity_type.value,
        "external_id": op.external_id,
        "before": public(before),
        "intended": op.intended,
        "args": op.args,
        "would_change": not matches(before, op.intended) if before is not None else True,
    }
