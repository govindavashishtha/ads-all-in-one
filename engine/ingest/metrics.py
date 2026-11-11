"""Idempotent metric upserts (plan §8.3, §9): ``metrics_daily``, ``metrics_hourly``,
``metrics_window``, ``search_terms``.

Connectors return platform ``external_id``s; ``EntityIndex`` maps them to internal ids. Rows
for entities that are not (yet) synced are skipped and counted, never invented. All writes
are ``INSERT .. ON CONFLICT (natural key) DO UPDATE`` so re-running a window rewrites the same
rows (§0 rule 7).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from engine.connectors.base import DailyMetricRow, HourlyMetricRow, SearchTermRow, WindowMetricRow
from engine.db.models import (
    Ad,
    AdAccount,
    AdGroup,
    Campaign,
    Keyword,
    MetricsDaily,
    MetricsHourly,
    MetricsWindow,
    SearchTerm,
)
from engine.enums import EntityType, Platform

FINAL_AFTER_DAYS = 30  # metrics_daily.is_final once date is more than 30 days old (§8.3)
CHUNK = 1000


@dataclass(frozen=True, slots=True)
class UpsertCount:
    written: int
    skipped_unknown: int


class EntityIndex:
    """(entity_type, external_id) → internal id for one client + platform."""

    def __init__(self, mapping: dict[tuple[EntityType, str], int]) -> None:
        self._m = mapping

    @classmethod
    def load(cls, session: Session, client_id: str, platform: Platform) -> EntityIndex:
        m: dict[tuple[EntityType, str], int] = {}
        for etype, model in (
            (EntityType.ACCOUNT, AdAccount),
            (EntityType.CAMPAIGN, Campaign),
            (EntityType.AD_GROUP, AdGroup),
            (EntityType.AD, Ad),
            (EntityType.KEYWORD, Keyword),
        ):
            rows = session.execute(
                select(model.id, model.external_id).where(
                    model.client_id == client_id, model.platform == platform
                )
            )
            for rid, ext in rows:
                m[(etype, str(ext))] = int(rid)
        return cls(m)

    def get(self, entity_type: EntityType, external_id: str) -> int | None:
        return self._m.get((entity_type, external_id))


def _chunks(rows: Sequence[dict[str, Any]]) -> Iterable[Sequence[dict[str, Any]]]:
    for i in range(0, len(rows), CHUNK):
        yield rows[i : i + CHUNK]


def _dedupe(rows: list[dict[str, Any]], key: tuple[str, ...]) -> list[dict[str, Any]]:
    """Last row wins per natural key (a multi-row upsert can't touch one key twice)."""
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        out[tuple(r[k] for k in key)] = r
    return list(out.values())


def _upsert(session: Session, model: Any, rows: list[dict[str, Any]], key: tuple[str, ...]) -> int:
    rows = _dedupe(rows, key)
    for chunk in _chunks(rows):
        stmt = pg_insert(model).values(list(chunk))
        update = {c: stmt.excluded[c] for c in chunk[0] if c not in key}
        session.execute(stmt.on_conflict_do_update(index_elements=list(key), set_=update))
    return len(rows)


_METRIC_PK = ("client_id", "platform", "entity_type", "entity_id")


def upsert_daily(
    session: Session,
    client_id: str,
    index: EntityIndex,
    rows: Iterable[DailyMetricRow],
    *,
    now: dt.datetime,
    local_today: dt.date,
) -> UpsertCount:
    out: list[dict[str, Any]] = []
    skipped = 0
    final_before = local_today - dt.timedelta(days=FINAL_AFTER_DAYS)
    for r in rows:
        eid = index.get(r.entity_type, r.external_id)
        if eid is None:
            skipped += 1
            continue
        out.append(
            {
                "client_id": client_id,
                "platform": r.platform,
                "entity_type": r.entity_type,
                "entity_id": eid,
                "date": r.date,
                "impressions": r.impressions,
                "clicks": r.clicks,
                "spend": r.spend,
                "conversions": r.conversions,
                "conv_value": r.conv_value,
                "video_views": r.video_views,
                "pulled_at": now,
                "is_final": r.date < final_before,
            }
        )
    return UpsertCount(_upsert(session, MetricsDaily, out, (*_METRIC_PK, "date")), skipped)


def upsert_hourly(
    session: Session,
    client_id: str,
    index: EntityIndex,
    rows: Iterable[HourlyMetricRow],
    *,
    now: dt.datetime,
) -> UpsertCount:
    out: list[dict[str, Any]] = []
    skipped = 0
    for r in rows:
        if r.entity_type not in (EntityType.ACCOUNT, EntityType.CAMPAIGN):
            raise ValueError("metrics_hourly holds account/campaign rows only (§8.3)")
        if r.ts_hour.tzinfo is not None:
            raise ValueError("ts_hour must be naive account-local time (§0 rule 9)")
        eid = index.get(r.entity_type, r.external_id)
        if eid is None:
            skipped += 1
            continue
        out.append(
            {
                "client_id": client_id,
                "platform": r.platform,
                "entity_type": r.entity_type,
                "entity_id": eid,
                "ts_hour": r.ts_hour,
                "impressions": r.impressions,
                "clicks": r.clicks,
                "spend": r.spend,
                "conversions": r.conversions,
                "conv_value": r.conv_value,
                "pulled_at": now,
            }
        )
    return UpsertCount(_upsert(session, MetricsHourly, out, (*_METRIC_PK, "ts_hour")), skipped)


def upsert_window(
    session: Session, client_id: str, index: EntityIndex, rows: Iterable[WindowMetricRow]
) -> UpsertCount:
    out: list[dict[str, Any]] = []
    skipped = 0
    for r in rows:
        eid = index.get(r.entity_type, r.external_id)
        if eid is None:
            skipped += 1
            continue
        out.append(
            {
                "client_id": client_id,
                "platform": r.platform,
                "entity_type": r.entity_type,
                "entity_id": eid,
                "window_days": r.window_days,
                "as_of": r.as_of,
                "reach": r.reach,
                "frequency": r.frequency,
            }
        )
    key = (*_METRIC_PK, "window_days", "as_of")
    return UpsertCount(_upsert(session, MetricsWindow, out, key), skipped)


def upsert_search_terms(
    session: Session, client_id: str, index: EntityIndex, rows: Iterable[SearchTermRow]
) -> UpsertCount:
    out: list[dict[str, Any]] = []
    skipped = 0
    for r in rows:
        gid = index.get(EntityType.AD_GROUP, r.ad_group_external_id)
        if gid is None:
            skipped += 1
            continue
        out.append(
            {
                "client_id": client_id,
                "ad_group_id": gid,
                "term": r.term,
                "date": r.date,
                "impressions": r.impressions,
                "clicks": r.clicks,
                "cost": r.cost,
                "conversions": r.conversions,
                "conv_value": r.conv_value,
            }
        )
    key = ("client_id", "ad_group_id", "term", "date")
    return UpsertCount(_upsert(session, SearchTerm, out, key), skipped)
