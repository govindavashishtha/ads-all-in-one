"""Entities with latest metrics + score (§17): campaigns, ad groups, ads, keywords."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.auth.adapter import Admin, Viewer
from api.deps import ClockDep, DbDep, SettingsDep, client_ip, load_client
from api.errors import not_found
from api.schemas import (
    AdGroupDetail,
    AdGroupOut,
    AdOut,
    CampaignDetail,
    CampaignOut,
    KeywordOut,
    ManagedRequest,
)
from api.services.queries import audit, empty_range, latest_scores, range_metrics
from api.timeutil import account_zones, complete_days_window, parse_days
from engine.db.models import Ad, AdGroup, Campaign, Client, Keyword
from engine.enums import EntityType, Platform

router = APIRouter(tags=["entities"])

RangeQ = Query("7d", alias="range", description="metrics window, complete account days")
Limit = Query(500, ge=1, le=2000)


def _windows(
    db: Session, client: Client, now: dt.datetime, days: int
) -> dict[Platform, tuple[dt.date, dt.date]]:
    zones = account_zones(db, client)
    return {p: complete_days_window(now.astimezone(zones[p]).date(), days) for p in Platform}


def _enrich[M: BaseModel](
    db: Session,
    *,
    client: Client,
    entity_type: EntityType,
    rows: Sequence[Any],
    model: type[M],
    windows: dict[Platform, tuple[dt.date, dt.date]],
) -> list[M]:
    ids = [r.id for r in rows]
    metrics = range_metrics(db, client.client_id, entity_type, ids, windows)
    scores = latest_scores(db, client.client_id, entity_type, ids)
    out = []
    for r in rows:
        rid: int = r.id
        platform = Platform(r.platform)
        item = model.model_validate(r).model_copy(
            update={
                "metrics": metrics.get(rid) or empty_range(platform, windows),
                "score": scores.get(rid),
            }
        )
        out.append(item)
    return out


@router.get("/campaigns", response_model=list[CampaignOut])
def list_campaigns(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    platform: Platform | None = None,
    managed: bool | None = None,
    status: str | None = None,
    range_: str = RangeQ,
    limit: int = Limit,
    offset: int = Query(0, ge=0),
) -> list[CampaignOut]:
    client = load_client(db, settings)
    q = select(Campaign).where(Campaign.client_id == client.client_id)
    if platform:
        q = q.where(Campaign.platform == platform)
    if managed is not None:
        q = q.where(Campaign.managed.is_(managed))
    if status:
        q = q.where(Campaign.status == status)
    rows = list(
        db.scalars(
            q.order_by(Campaign.platform, Campaign.name, Campaign.id).limit(limit).offset(offset)
        )
    )
    windows = _windows(db, client, clock(), parse_days(range_))
    return _enrich(
        db,
        client=client,
        entity_type=EntityType.CAMPAIGN,
        rows=rows,
        model=CampaignOut,
        windows=windows,
    )


def _get(db: Session, model: Any, cid: str, ident: int, what: str) -> Any:
    row = db.get(model, ident)
    if row is None or row.client_id != cid:
        not_found(what, ident)
    return row


@router.get("/campaigns/{campaign_id}", response_model=CampaignDetail)
def get_campaign(
    *,
    campaign_id: int,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    range_: str = RangeQ,
) -> CampaignDetail:
    client = load_client(db, settings)
    row = _get(db, Campaign, client.client_id, campaign_id, "campaign")
    windows = _windows(db, client, clock(), parse_days(range_))
    [base] = _enrich(
        db,
        client=client,
        entity_type=EntityType.CAMPAIGN,
        rows=[row],
        model=CampaignDetail,
        windows=windows,
    )
    groups = list(
        db.scalars(select(AdGroup).where(AdGroup.campaign_id == campaign_id).order_by(AdGroup.id))
    )
    base.ad_groups = _enrich(
        db,
        client=client,
        entity_type=EntityType.AD_GROUP,
        rows=groups,
        model=AdGroupOut,
        windows=windows,
    )
    return base


@router.put("/campaigns/{campaign_id}/managed", response_model=CampaignOut)
def set_managed(
    *,
    campaign_id: int,
    body: ManagedRequest,
    request: Request,
    user: Admin,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> CampaignOut:
    """Opt a campaign in/out of automation (INPUT I-3). DB flag only; no platform call."""
    client = load_client(db, settings)
    row = _get(db, Campaign, client.client_id, campaign_id, "campaign")
    before = row.managed
    row.managed = body.managed
    now = clock()
    row.updated_at = now
    audit(
        db,
        client_id=client.client_id,
        actor=user.username,
        action="campaign.managed",
        target=f"campaign:{campaign_id}",
        payload={"before": before, "after": body.managed, "reason": body.reason},
        ip=client_ip(request),
        at=now,
    )
    db.commit()
    windows = _windows(db, client, now, 7)
    [out] = _enrich(
        db,
        client=client,
        entity_type=EntityType.CAMPAIGN,
        rows=[row],
        model=CampaignOut,
        windows=windows,
    )
    return out


@router.get("/ad-groups/{ad_group_id}", response_model=AdGroupDetail)
def get_ad_group(
    *,
    ad_group_id: int,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    range_: str = RangeQ,
) -> AdGroupDetail:
    client = load_client(db, settings)
    row = _get(db, AdGroup, client.client_id, ad_group_id, "ad group")
    windows = _windows(db, client, clock(), parse_days(range_))
    [base] = _enrich(
        db,
        client=client,
        entity_type=EntityType.AD_GROUP,
        rows=[row],
        model=AdGroupDetail,
        windows=windows,
    )
    ads = list(db.scalars(select(Ad).where(Ad.ad_group_id == ad_group_id).order_by(Ad.id)))
    kws = list(
        db.scalars(select(Keyword).where(Keyword.ad_group_id == ad_group_id).order_by(Keyword.id))
    )
    base.ads = _enrich(
        db, client=client, entity_type=EntityType.AD, rows=ads, model=AdOut, windows=windows
    )
    base.keywords = _enrich(
        db,
        client=client,
        entity_type=EntityType.KEYWORD,
        rows=kws,
        model=KeywordOut,
        windows=windows,
    )
    return base


@router.get("/ads/{ad_id}", response_model=AdOut)
def get_ad(
    *,
    ad_id: int,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    range_: str = RangeQ,
) -> AdOut:
    client = load_client(db, settings)
    row = _get(db, Ad, client.client_id, ad_id, "ad")
    windows = _windows(db, client, clock(), parse_days(range_))
    [out] = _enrich(
        db, client=client, entity_type=EntityType.AD, rows=[row], model=AdOut, windows=windows
    )
    return out


@router.get("/keywords", response_model=list[KeywordOut])
def list_keywords(
    *,
    user: Viewer,
    db: DbDep,
    settings: SettingsDep,
    clock: ClockDep,
    ad_group_id: int | None = None,
    is_negative: bool | None = None,
    range_: str = RangeQ,
    limit: int = Limit,
    offset: int = Query(0, ge=0),
) -> list[KeywordOut]:
    client = load_client(db, settings)
    q = select(Keyword).where(Keyword.client_id == client.client_id)
    if ad_group_id is not None:
        q = q.where(Keyword.ad_group_id == ad_group_id)
    if is_negative is not None:
        q = q.where(Keyword.is_negative.is_(is_negative))
    rows = list(db.scalars(q.order_by(Keyword.id).limit(limit).offset(offset)))
    windows = _windows(db, client, clock(), parse_days(range_))
    return _enrich(
        db,
        client=client,
        entity_type=EntityType.KEYWORD,
        rows=rows,
        model=KeywordOut,
        windows=windows,
    )
