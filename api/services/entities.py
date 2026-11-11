"""Display enrichment for proposals, changes and rankings: entity names, platform, owning
campaign, and a human-readable before → after (dashboard contract, API_CONTRACT.md)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.schemas import GuardrailResultOut, ValueChange
from engine.db.models import Ad, AdGroup, Campaign, Keyword, SearchTerm
from engine.enums import Action, EntityType, Platform

CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class EntityInfo:
    entity_name: str | None
    platform: Platform | None
    campaign_id: int | None
    campaign_name: str | None
    parent_id: int | None = None
    parent_name: str | None = None
    status: str | None = None
    daily_budget: Decimal | None = None


Ref = tuple[EntityType, int]


def resolve(db: Session, client_id: str, refs: Iterable[Ref]) -> dict[Ref, EntityInfo]:
    """Batch lookup (one query per entity type plus parents). Unknown ids are omitted."""
    want: dict[EntityType, set[int]] = defaultdict(set)
    for et, eid in refs:
        want[et].add(int(eid))
    out: dict[Ref, EntityInfo] = {}

    terms: dict[int, tuple[str, int]] = {}
    if want.get(EntityType.SEARCH_TERM):
        for tid, term, ag in db.execute(
            select(SearchTerm.id, SearchTerm.term, SearchTerm.ad_group_id).where(
                SearchTerm.client_id == client_id, SearchTerm.id.in_(want[EntityType.SEARCH_TERM])
            )
        ):
            terms[tid] = (term, ag)
            want[EntityType.AD_GROUP].add(ag)

    ads: dict[int, Ad] = {}
    if want.get(EntityType.AD):
        ads = {
            a.id: a
            for a in db.scalars(
                select(Ad).where(Ad.client_id == client_id, Ad.id.in_(want[EntityType.AD]))
            )
        }
        want[EntityType.AD_GROUP].update(a.ad_group_id for a in ads.values())
    kws: dict[int, Keyword] = {}
    if want.get(EntityType.KEYWORD):
        kws = {
            k.id: k
            for k in db.scalars(
                select(Keyword).where(
                    Keyword.client_id == client_id, Keyword.id.in_(want[EntityType.KEYWORD])
                )
            )
        }
        want[EntityType.AD_GROUP].update(k.ad_group_id for k in kws.values())
    groups: dict[int, AdGroup] = {}
    if want.get(EntityType.AD_GROUP):
        groups = {
            g.id: g
            for g in db.scalars(
                select(AdGroup).where(
                    AdGroup.client_id == client_id, AdGroup.id.in_(want[EntityType.AD_GROUP])
                )
            )
        }
        want[EntityType.CAMPAIGN].update(g.campaign_id for g in groups.values())
    camps: dict[int, Campaign] = {}
    if want.get(EntityType.CAMPAIGN):
        camps = {
            c.id: c
            for c in db.scalars(
                select(Campaign).where(
                    Campaign.client_id == client_id, Campaign.id.in_(want[EntityType.CAMPAIGN])
                )
            )
        }

    def camp_of(group_id: int | None) -> tuple[int | None, str | None]:
        g = groups.get(group_id) if group_id is not None else None
        c = camps.get(g.campaign_id) if g else None
        return (c.id, c.name) if c else ((g.campaign_id, None) if g else (None, None))

    for cid, c in camps.items():
        out[(EntityType.CAMPAIGN, cid)] = EntityInfo(
            c.name, Platform(c.platform), c.id, c.name, None, None, c.status, _dec(c.daily_budget)
        )
    for gid, g in groups.items():
        ccid, cname = camp_of(gid)
        out[(EntityType.AD_GROUP, gid)] = EntityInfo(
            g.name, Platform(g.platform), ccid, cname, ccid, cname, g.status, _dec(g.daily_budget)
        )
    for aid, a in ads.items():
        ccid, cname = camp_of(a.ad_group_id)
        grp = groups.get(a.ad_group_id)
        out[(EntityType.AD, aid)] = EntityInfo(
            a.name or a.headline,
            Platform(a.platform),
            ccid,
            cname,
            a.ad_group_id,
            grp.name if grp else None,
            a.status,
        )
    for kid, k in kws.items():
        ccid, cname = camp_of(k.ad_group_id)
        grp = groups.get(k.ad_group_id)
        out[(EntityType.KEYWORD, kid)] = EntityInfo(
            k.text,
            Platform(k.platform),
            ccid,
            cname,
            k.ad_group_id,
            grp.name if grp else None,
            k.status,
        )
    for tid, (term, ag) in terms.items():
        ccid, cname = camp_of(ag)
        grp = groups.get(ag)
        out[(EntityType.SEARCH_TERM, tid)] = EntityInfo(
            term,
            Platform(grp.platform) if grp else Platform.GOOGLE,
            ccid,
            cname,
            ag,
            grp.name if grp else None,
        )
    return out


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    return d.quantize(CENT) if d.is_finite() else None


def _money_str(v: Any) -> str | None:
    d = _dec(v)
    return None if d is None else str(d)


def parse_guardrail(raw: dict[str, Any] | None) -> GuardrailResultOut | None:
    """Stored JSON → typed model; anything unparseable becomes None (never a 500)."""
    if not raw:
        return None
    try:
        return GuardrailResultOut.model_validate(raw)
    except ValidationError:
        return None


def proposal_change(  # noqa: PLR0911 — one branch per action
    action: str,
    params: dict[str, Any] | None,
    expected_impact: dict[str, Any] | None,
    guardrail: GuardrailResultOut | None,
    info: EntityInfo | None,
) -> ValueChange | None:
    params = params or {}
    if action == Action.BUDGET_CHANGE.value:
        detail = guardrail.detail if guardrail else {}
        before = (
            _money_str((expected_impact or {}).get("daily_budget_before"))
            or _money_str(detail.get("current"))
            or _money_str(params.get("current_budget"))
            or (str(info.daily_budget) if info and info.daily_budget is not None else None)
        )
        after = (_money_str(guardrail.new_daily_budget) if guardrail else None) or _money_str(
            params.get("target_daily_budget")
        )
        if after is None and before is not None and params.get("change_pct") is not None:
            try:
                pct = Decimal(str(params["change_pct"]))
                after = str((Decimal(before) * (1 + pct / 100)).quantize(CENT))
            except InvalidOperation:
                after = None
        return ValueChange(field="daily_budget", before=before, after=after, after_is_estimate=True)
    if action == Action.PAUSE.value:
        return ValueChange(field="status", before=info.status if info else None, after="paused")
    if action == Action.ENABLE.value:
        return ValueChange(field="status", before=info.status if info else None, after="active")
    if action in (Action.ADD_NEGATIVE.value, Action.REMOVE_NEGATIVE.value):
        term = params.get("term")
        label = f"{term} [{params.get('match_type') or 'exact'}]" if term else None
        adding = action == Action.ADD_NEGATIVE.value
        return ValueChange(
            field="negative_keyword",
            before=None if adding else label,
            after=label if adding else None,
        )
    if action == Action.NEW_AD.value:
        return ValueChange(field="creative", before=None, after="new ad (draft)")
    if action == Action.NEW_CAMPAIGN.value:
        return ValueChange(field="campaign", before=None, after="new campaign (draft)")
    return None


def change_log_change(
    action: str,
    before: dict[str, Any] | None,
    intended: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> ValueChange | None:
    """``change_log.before/after`` hold executor EntityState snapshots (``status``,
    ``daily_budget``, …); ``after`` falls back to ``intended`` until read-back."""
    b, a = before or {}, after or intended or {}
    for key in ("daily_budget", "status"):
        if key in b or key in a:
            fmt = _money_str if key == "daily_budget" else _plain
            return ValueChange(
                field="daily_budget" if key == "daily_budget" else "status",
                before=fmt(b.get(key)),
                after=fmt(a.get(key)),
                after_is_estimate=not after,
            )
    term = a.get("term") or b.get("term")
    if term:
        label = f"{term} [{a.get('match_type') or b.get('match_type') or 'exact'}]"
        adding = action == Action.ADD_NEGATIVE.value
        return ValueChange(
            field="negative_keyword",
            before=None if adding else label,
            after=label if adding else None,
        )
    return None


def _plain(v: Any) -> str | None:
    return None if v is None else str(v)
