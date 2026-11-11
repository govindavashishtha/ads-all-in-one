"""Evaluation unit of a change (plan §16.1): where its effect is observable.

| change                              | unit                         |
|-------------------------------------|------------------------------|
| pause / enable / new ad             | the parent ad set / ad group |
| budget_change on a Meta ad set      | the parent campaign          |
| budget_change on a campaign         | the campaign                 |
| add_negative                        | the ad group                 |

Pausing/enabling an ad group or campaign: its parent campaign / the campaign itself (D-32;
§16.1 names only ads). Units are always a campaign or an ad group (DB check constraint).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.db.models import Ad, AdGroup, Keyword, SearchTerm
from engine.enums import Action, EntityType


class UnitError(LookupError):
    pass


def _group_of(
    session: Session, model: type[Ad] | type[Keyword] | type[SearchTerm], entity_id: int
) -> int:
    gid = session.scalar(select(model.ad_group_id).where(model.id == entity_id))
    if gid is None:
        raise UnitError(f"{model.__tablename__} {entity_id} has no ad group")
    return int(gid)


def _campaign_of_group(session: Session, group_id: int) -> int:
    cid = session.scalar(select(AdGroup.campaign_id).where(AdGroup.id == group_id))
    if cid is None:
        raise UnitError(f"ad group {group_id} not found")
    return int(cid)


def evaluation_unit(  # noqa: PLR0911
    session: Session, action: Action, entity_type: EntityType, entity_id: int
) -> tuple[EntityType, int]:
    if action is Action.BUDGET_CHANGE:
        if entity_type is EntityType.CAMPAIGN:
            return EntityType.CAMPAIGN, entity_id
        if entity_type is EntityType.AD_GROUP:
            return EntityType.CAMPAIGN, _campaign_of_group(session, entity_id)
        raise UnitError(f"budget_change on {entity_type.value}")
    if entity_type is EntityType.AD:
        return EntityType.AD_GROUP, _group_of(session, Ad, entity_id)
    if entity_type is EntityType.KEYWORD:
        return EntityType.AD_GROUP, _group_of(session, Keyword, entity_id)
    if entity_type is EntityType.SEARCH_TERM:
        return EntityType.AD_GROUP, _group_of(session, SearchTerm, entity_id)
    if entity_type is EntityType.AD_GROUP:
        if action in (Action.NEW_AD, Action.ADD_NEGATIVE):
            return EntityType.AD_GROUP, entity_id
        return EntityType.CAMPAIGN, _campaign_of_group(session, entity_id)
    if entity_type is EntityType.CAMPAIGN:
        return EntityType.CAMPAIGN, entity_id
    raise UnitError(f"no evaluation unit for {action.value} on {entity_type.value}")
