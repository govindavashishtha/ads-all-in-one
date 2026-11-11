"""GET /rankings (§17): latest scores per level, ranked within peer group, with intervals."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from sqlalchemy import and_, func, select

from api.auth.adapter import Viewer
from api.deps import DbDep, SettingsDep, load_client
from api.schemas import RankingRow, RankingsResponse, ScoreOut
from api.services.config import active_targets
from api.services.entities import resolve
from engine.db.models import Ad, AdGroup, Campaign, Feature, Keyword, Score
from engine.enums import EntityType, Platform

router = APIRouter(tags=["rankings"])

_LEVELS: dict[str, tuple[EntityType, Any]] = {
    "campaign": (EntityType.CAMPAIGN, Campaign),
    "ad_group": (EntityType.AD_GROUP, AdGroup),
    "ad": (EntityType.AD, Ad),
    "keyword": (EntityType.KEYWORD, Keyword),
}


def _name(row: Any) -> str | None:
    return getattr(row, "name", None) or getattr(row, "text", None)


@router.get("/rankings", response_model=RankingsResponse)
def rankings(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    level: Literal["ad", "ad_group", "keyword", "campaign"] = "ad",
    platform: Platform | None = None,
    sufficient_only: bool = False,
    top: int = Query(500, ge=1, le=2000),
) -> RankingsResponse:
    """Rows from the latest ``scores.as_of`` for the level (empty until scoring runs), with
    the settled-window spend/conversions and fatigue flag from ``features`` of the same
    ``as_of``, the owning campaign / direct parent, and the targets they were ranked against."""
    client = load_client(db, settings)
    targets = active_targets(db, client.client_id)
    entity_type, model = _LEVELS[level]
    as_of = db.scalar(
        select(func.max(Score.as_of)).where(
            Score.client_id == client.client_id, Score.entity_type == entity_type
        )
    )
    if as_of is None:
        return RankingsResponse(level=entity_type, as_of=None, rows=[], targets=targets)
    q = (
        select(Score, model, Feature)
        .join(model, model.id == Score.entity_id)
        .outerjoin(
            Feature,
            and_(
                Feature.client_id == Score.client_id,
                Feature.entity_type == Score.entity_type,
                Feature.entity_id == Score.entity_id,
                Feature.as_of == Score.as_of,
            ),
        )
        .where(
            Score.client_id == client.client_id,
            Score.entity_type == entity_type,
            Score.as_of == as_of,
        )
    )
    if platform:
        q = q.where(model.platform == platform)
    if sufficient_only:
        q = q.where(Score.data_sufficient.is_(True))
    q = q.order_by(
        Score.peer_group.nulls_last(),
        Score.rank_in_peer.nulls_last(),
        Score.rank_metric.desc().nulls_last(),
    ).limit(top)
    result = db.execute(q).all()
    infos = resolve(db, client.client_id, [(entity_type, e.id) for _, e, _ in result])
    rows = []
    for score, entity, feat in result:
        info = infos.get((entity_type, entity.id))
        rows.append(
            RankingRow(
                **ScoreOut.model_validate(score).model_dump(),
                entity_type=entity_type,
                entity_id=entity.id,
                platform=Platform(entity.platform),
                name=_name(entity),
                status=entity.status,
                campaign_id=info.campaign_id if info else None,
                campaign_name=info.campaign_name if info else None,
                parent_id=info.parent_id if info else None,
                parent_name=info.parent_name if info else None,
                spend=feat.spend_s14 if feat else None,
                conversions=feat.conversions_s14 if feat else None,
                spend_7d=feat.spend_7d if feat else None,
                conversions_7d=feat.conversions_7d if feat else None,
                fatigue_flag=bool(feat.fatigue_flag) if feat else False,
                learning=feat.learning if feat else None,
            )
        )
    return RankingsResponse(level=entity_type, as_of=as_of, rows=rows, targets=targets)
