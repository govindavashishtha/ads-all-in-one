"""Fake platforms mirroring the DB's synced entities (tests and the fake-connector demo).

``fake_platforms_from_db`` copies every campaign / ad group / ad / negative keyword row of a
client into in-memory ``FakePlatform``s, so the executor can run end to end against a copy
of a real (synthetic) database without any platform access.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.connectors.base import EntityStatus
from engine.connectors.fakes_write import FakeNegative, FakePlatform
from engine.db.models import Ad, AdGroup, Campaign, Keyword
from engine.enums import EntityType, Platform


def _status(v: str | None) -> EntityStatus:
    try:
        return EntityStatus(v or "other")
    except ValueError:
        return EntityStatus.OTHER


def fake_platforms_from_db(session: Session, client_id: str) -> dict[Platform, FakePlatform]:
    out = {pl: FakePlatform(pl) for pl in Platform}
    models: tuple[tuple[Any, EntityType], ...] = (
        (Campaign, EntityType.CAMPAIGN),
        (AdGroup, EntityType.AD_GROUP),
        (Ad, EntityType.AD),
    )
    for model, et in models:
        for row in session.scalars(select(model).where(model.client_id == client_id)):
            budget = getattr(row, "daily_budget", None)
            out[Platform(row.platform)].add(
                et,
                str(row.external_id),
                _status(row.status),
                Decimal(budget) if budget is not None else None,
            )
    groups = dict(
        session.execute(
            select(AdGroup.id, AdGroup.external_id).where(AdGroup.client_id == client_id)
        ).all()
    )
    for k in session.scalars(select(Keyword).where(Keyword.client_id == client_id)):
        pl = out[Platform(k.platform)]
        if k.is_negative:
            pl.negatives[str(k.external_id)] = FakeNegative(
                str(groups.get(k.ad_group_id, "")),
                k.text,
                (k.match_type or "").lower(),
                _status(k.status),
            )
        else:
            pl.add(EntityType.KEYWORD, str(k.external_id), _status(k.status))
    return out
