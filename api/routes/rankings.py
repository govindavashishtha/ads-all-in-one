"""GET /rankings (§17): latest scores per level, ranked within peer group, with intervals."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from api.auth.adapter import Viewer
from api.deps import DbDep, SettingsDep, load_client
from api.schemas import RankingRow, RankingsResponse, ScoreOut
from engine.db.models import Ad, AdGroup, Campaign, Keyword, Score
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


def _parent(row: Any, entity_type: EntityType) -> int | None:
    if entity_type is EntityType.CAMPAIGN:
        return int(row.id)
    if entity_type is EntityType.AD_GROUP:
        return int(row.campaign_id)
    return None


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
    """Rows from the latest ``scores.as_of`` for the level (empty until scoring runs)."""
    client = load_client(db, settings)
    entity_type, model = _LEVELS[level]
    as_of = db.scalar(
        select(func.max(Score.as_of)).where(
            Score.client_id == client.client_id, Score.entity_type == entity_type
        )
    )
    if as_of is None:
        return RankingsResponse(level=entity_type, as_of=None, rows=[])
    q = (
        select(Score, model)
        .join(model, model.id == Score.entity_id)
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
    rows = [
        RankingRow(
            **ScoreOut.model_validate(score).model_dump(),
            entity_type=entity_type,
            entity_id=entity.id,
            platform=Platform(entity.platform),
            name=_name(entity),
            status=entity.status,
            campaign_id=_parent(entity, entity_type),
        )
        for score, entity in db.execute(q).all()
    ]
    return RankingsResponse(level=entity_type, as_of=as_of, rows=rows)
