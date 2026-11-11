"""Entity sync: upsert accounts/campaigns/ad groups/ads/keywords, append ``entity_history``
only when a tracked field changed, and detect manual changes (plan §8.2, §8.3, §9, §9.2).

Rules implemented here:
- Upserts on the natural keys (§8.1). ``managed`` and ``pool`` are operator-owned and never
  overwritten by a sync. ``first_live_at`` is set once (platform value, else first time seen
  active) and never moved.
- Meta campaign ``learning`` is derived from its ad sets (connector returns None): any active
  ad set learning → true; any active ad set unknown, or no active ad set → NULL (protected);
  else false. (Decision; §8.2 defines Meta learning per ad set only.)
- ``entity_history`` tracked fields: ``status``, ``daily_budget``, ``learning``. A row is
  appended for the first observation and whenever any of them differs from the latest row.
- Manual change (§9.2 fallback, always on): ``status`` or ``daily_budget`` of an entity whose
  campaign is ``managed`` differs from the latest history row, and there is no ``change_log``
  row for the entity with ``status='applied'`` and ``applied_at`` within 2 h whose ``after``
  equals the new value(s). Effect: history row (already appended), ``info`` alert
  ``kind='manual_change'``. The platform change history (Google ``change_event`` / Meta
  ``activities``) is read only when a candidate is found, to name the actor in the alert.
  The cooldown is derived from these alerts — see ``engine.ingest.manual``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from engine.connectors.base import (
    AdGroupRecord,
    CampaignRecord,
    ChangeEvent,
    EntityStatus,
    ReadConnector,
)
from engine.db.models import Ad, AdAccount, AdGroup, Campaign, EntityHistory, Keyword
from engine.enums import EntityType, HistorySource, Platform, Severity
from engine.ingest.alerts import raise_alert

log = structlog.get_logger(__name__)

MANUAL_MATCH_WINDOW = dt.timedelta(hours=2)  # §9.2
CHANGE_HISTORY_LOOKBACK = dt.timedelta(hours=3)


@dataclass
class SyncResult:
    platform: Platform
    account_id: int
    counts: dict[str, int] = field(default_factory=dict)
    orphans: int = 0
    history_appended: int = 0
    manual_changes: list[ManualChange] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return sum(self.counts.values()) + self.history_appended


@dataclass(frozen=True, slots=True)
class ManualChange:
    entity_type: EntityType
    entity_id: int
    external_id: str
    fields: tuple[str, ...]
    old: dict[str, Any]
    new: dict[str, Any]
    actor: str | None = None
    client_type: str | None = None


@dataclass(frozen=True, slots=True)
class _Tracked:
    entity_type: EntityType
    entity_id: int
    external_id: str
    status: str
    daily_budget: Decimal | None
    learning: bool | None
    managed: bool


def derive_meta_campaign_learning(ad_sets: Iterable[AdGroupRecord]) -> bool | None:
    active = [a for a in ad_sets if a.status is EntityStatus.ACTIVE]
    if not active:
        return None
    if any(a.learning is True for a in active):
        return True
    if any(a.learning is None for a in active):
        return None
    return False


def _upsert_returning(  # noqa: PLR0917
    session: Session,
    model: Any,
    rows: Sequence[dict[str, Any]],
    conflict: list[str],
    update: list[str],
    key: Callable[[Any], Any],
    extra_set: dict[str, Any] | None = None,
) -> dict[Any, int]:
    """Multi-row INSERT .. ON CONFLICT DO UPDATE .. RETURNING id (+ natural key)."""
    if not rows:
        return {}
    stmt = pg_insert(model).values(list(rows))
    set_ = {c: stmt.excluded[c] for c in update}
    set_.update(extra_set or {})
    upsert = stmt.on_conflict_do_update(index_elements=conflict, set_=set_).returning(
        *[getattr(model, c) for c in ("id", *conflict[1:])]
    )
    return {key(r): int(r.id) for r in session.execute(upsert)}


def sync_entities(  # noqa: PLR0912, PLR0915
    session: Session,
    client_id: str,
    connector: ReadConnector,
    now: dt.datetime,
    *,
    source: HistorySource = HistorySource.HOURLY_PULL,
) -> SyncResult:
    platform = connector.platform
    acct = connector.get_account()
    account_ids = _upsert_returning(
        session,
        AdAccount,
        [
            {
                "client_id": client_id,
                "platform": platform,
                "external_id": acct.external_id,
                "name": acct.name,
                "status": acct.status,
                "currency": acct.currency,
                "timezone": acct.timezone,
                "last_synced_at": now,
            }
        ],
        ["client_id", "platform", "external_id"],
        ["name", "status", "currency", "timezone", "last_synced_at"],
        key=lambda r: r.external_id,
    )
    account_id = account_ids[acct.external_id]
    result = SyncResult(platform=platform, account_id=account_id)

    campaigns = connector.list_campaigns()
    groups = connector.list_ad_groups()
    ads = connector.list_ads()
    keywords = connector.list_keywords()

    if platform is Platform.META:
        by_parent: dict[str, list[AdGroupRecord]] = {}
        for g in groups:
            by_parent.setdefault(g.campaign_external_id, []).append(g)
        learning = {
            c.external_id: derive_meta_campaign_learning(by_parent.get(c.external_id, []))
            for c in campaigns
        }
    else:
        learning = {c.external_id: c.learning for c in campaigns}

    camp_ids = _upsert_campaigns(session, client_id, account_id, campaigns, learning, now)
    result.counts["campaigns"] = len(camp_ids)

    group_rows: list[dict[str, Any]] = []
    for g in groups:
        cid = camp_ids.get(g.campaign_external_id)
        if cid is None:
            result.orphans += 1
            continue
        group_rows.append(
            {
                "client_id": client_id,
                "campaign_id": cid,
                "platform": platform,
                "external_id": g.external_id,
                "kind": g.kind.value,
                "name": g.name,
                "status": g.status.value,
                "daily_budget": g.daily_budget,
                "optimization_goal": g.optimization_goal,
                "learning": g.learning,
                "learning_status_at": now,
                "raw": g.raw,
                "updated_at": now,
            }
        )
    group_ids = _upsert_returning(
        session,
        AdGroup,
        group_rows,
        ["client_id", "platform", "external_id"],
        [
            "campaign_id",
            "kind",
            "name",
            "status",
            "daily_budget",
            "optimization_goal",
            "learning",
            "raw",
            "updated_at",
        ],
        key=lambda r: r.external_id,
        extra_set={
            "learning_status_at": text(
                "CASE WHEN ad_groups.learning IS DISTINCT FROM excluded.learning "
                "THEN excluded.learning_status_at ELSE ad_groups.learning_status_at END"
            )
        },
    )
    result.counts["ad_groups"] = len(group_ids)

    ad_rows: list[dict[str, Any]] = []
    for a in ads:
        gid = group_ids.get(a.ad_group_external_id)
        if gid is None:
            result.orphans += 1
            continue
        first_live = a.first_live_at or (now if a.status is EntityStatus.ACTIVE else None)
        ad_rows.append(
            {
                "client_id": client_id,
                "ad_group_id": gid,
                "platform": platform,
                "external_id": a.external_id,
                "name": a.name,
                "status": a.status.value,
                "format": a.format,
                "creative_id": a.creative_id,
                "headline": a.headline,
                "body": a.body,
                "cta": a.cta,
                "first_live_at": first_live,
                "raw": a.raw,
                "updated_at": now,
            }
        )
    ad_ids = _upsert_returning(
        session,
        Ad,
        ad_rows,
        ["client_id", "platform", "external_id"],
        [
            "ad_group_id",
            "name",
            "status",
            "format",
            "creative_id",
            "headline",
            "body",
            "cta",
            "raw",
            "updated_at",
        ],
        key=lambda r: r.external_id,
        extra_set={
            "first_live_at": func.coalesce(Ad.first_live_at, text("excluded.first_live_at"))
        },
    )
    result.counts["ads"] = len(ad_ids)

    kw_rows: list[dict[str, Any]] = []
    for k in keywords:
        gid = group_ids.get(k.ad_group_external_id)
        if gid is None:
            result.orphans += 1
            continue
        kw_rows.append(
            {
                "client_id": client_id,
                "ad_group_id": gid,
                "platform": Platform.GOOGLE,
                "external_id": k.external_id,
                "text": k.text,
                "match_type": k.match_type,
                "status": k.status.value,
                "is_negative": k.is_negative,
                "updated_at": now,
            }
        )
    kw_ids = _upsert_returning(
        session,
        Keyword,
        kw_rows,
        ["client_id", "ad_group_id", "external_id"],
        ["text", "match_type", "status", "is_negative", "updated_at"],
        key=lambda r: (r.ad_group_id, r.external_id),
    )
    result.counts["keywords"] = len(kw_ids)

    # ---- tracked state for history -----------------------------------------------------
    managed = _managed_campaigns(session, client_id, list(camp_ids.values()))
    camp_of_group = {g["external_id"]: g["campaign_id"] for g in group_rows}
    tracked: list[_Tracked] = []
    for c in campaigns:
        cid = camp_ids[c.external_id]
        tracked.append(
            _Tracked(
                EntityType.CAMPAIGN,
                cid,
                c.external_id,
                c.status.value,
                c.daily_budget,
                learning.get(c.external_id),
                cid in managed,
            )
        )
    for g in groups:
        if g.external_id in group_ids:
            tracked.append(
                _Tracked(
                    EntityType.AD_GROUP,
                    group_ids[g.external_id],
                    g.external_id,
                    g.status.value,
                    g.daily_budget,
                    g.learning,
                    camp_of_group[g.external_id] in managed,
                )
            )
    group_camp_by_id = {group_ids[k]: v for k, v in camp_of_group.items()}
    for row in ad_rows:
        tracked.append(
            _Tracked(
                EntityType.AD,
                ad_ids[row["external_id"]],
                row["external_id"],
                row["status"],
                None,
                None,
                group_camp_by_id.get(row["ad_group_id"]) in managed,
            )
        )
    for row in kw_rows:
        tracked.append(
            _Tracked(
                EntityType.KEYWORD,
                kw_ids[(row["ad_group_id"], row["external_id"])],
                row["external_id"],
                row["status"],
                None,
                None,
                group_camp_by_id.get(row["ad_group_id"]) in managed,
            )
        )

    appended, candidates = _append_history(session, client_id, tracked, now, source)
    result.history_appended = appended
    if candidates:
        result.manual_changes = _confirm_manual(session, client_id, connector, candidates, now)
        for mc in result.manual_changes:
            who = f" by {mc.actor}" if mc.actor else ""
            via = f" via {mc.client_type}" if mc.client_type else ""
            raise_alert(
                session,
                client_id,
                dedupe_key=f"manual_change:{mc.entity_type.value}:{mc.entity_id}:{now.isoformat()}",
                severity=Severity.INFO,
                kind="manual_change",
                entity_type=mc.entity_type,
                entity_id=mc.entity_id,
                message=(
                    f"Manual change on {platform.value} {mc.entity_type.value} "
                    f"{mc.external_id}{who}{via}: "
                    + ", ".join(f"{f} {mc.old.get(f)} → {mc.new.get(f)}" for f in mc.fields)
                    + " — cooldown started (after_manual_change_hours)."
                ),
            )
    log.info(
        "ingest.entities.synced",
        platform=platform.value,
        counts=result.counts,
        orphans=result.orphans,
        history=appended,
        manual=len(result.manual_changes),
    )
    return result


def _upsert_campaigns(  # noqa: PLR0917
    session: Session,
    client_id: str,
    account_id: int,
    campaigns: list[CampaignRecord],
    learning: dict[str, bool | None],
    now: dt.datetime,
) -> dict[str, int]:
    rows = [
        {
            "client_id": client_id,
            "ad_account_id": account_id,
            "platform": c.platform,
            "external_id": c.external_id,
            "name": c.name,
            "objective": c.objective,
            "kind": c.kind.value,
            "automation_scope": c.automation_scope.value,
            "status": c.status.value,
            "budget_level": c.budget_level.value,
            "daily_budget": c.daily_budget,
            "lifetime_budget": c.lifetime_budget,
            "shared_budget_external_id": c.shared_budget_external_id,
            "bid_strategy": c.bid_strategy,
            "learning": learning.get(c.external_id),
            "raw": c.raw,
            "updated_at": now,
        }
        for c in campaigns
    ]
    return _upsert_returning(
        session,
        Campaign,
        rows,
        ["client_id", "platform", "external_id"],
        [
            "ad_account_id",
            "name",
            "objective",
            "kind",
            "automation_scope",
            "status",
            "budget_level",
            "daily_budget",
            "lifetime_budget",
            "shared_budget_external_id",
            "bid_strategy",
            "learning",
            "raw",
            "updated_at",
        ],
        key=lambda r: r.external_id,
    )


def _managed_campaigns(session: Session, client_id: str, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    rows = session.execute(
        select(Campaign.id).where(
            Campaign.client_id == client_id, Campaign.id.in_(ids), Campaign.managed.is_(True)
        )
    )
    return {int(r[0]) for r in rows}


def _latest_history(
    session: Session, client_id: str, entity_type: EntityType, ids: list[int]
) -> dict[int, EntityHistory]:
    if not ids:
        return {}
    stmt = (
        select(EntityHistory)
        .where(
            EntityHistory.client_id == client_id,
            EntityHistory.entity_type == entity_type,
            EntityHistory.entity_id.in_(ids),
        )
        .order_by(
            EntityHistory.entity_id, EntityHistory.observed_at.desc(), EntityHistory.id.desc()
        )
        .ext(distinct_on(EntityHistory.entity_id))
    )
    return {h.entity_id: h for h in session.scalars(stmt)}


def _money_eq(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None or b is None:
        return a is b
    return Decimal(a) == Decimal(b)


def _append_history(
    session: Session,
    client_id: str,
    tracked: list[_Tracked],
    now: dt.datetime,
    source: HistorySource,
) -> tuple[int, list[tuple[_Tracked, EntityHistory]]]:
    by_type: dict[EntityType, list[_Tracked]] = {}
    for t in tracked:
        by_type.setdefault(t.entity_type, []).append(t)
    inserts: list[dict[str, Any]] = []
    candidates: list[tuple[_Tracked, EntityHistory]] = []
    for etype, items in by_type.items():
        latest = _latest_history(session, client_id, etype, [t.entity_id for t in items])
        for t in items:
            prev = latest.get(t.entity_id)
            if prev is not None and (
                prev.status == t.status
                and _money_eq(prev.daily_budget, t.daily_budget)
                and prev.learning is t.learning
            ):
                continue
            inserts.append(
                {
                    "client_id": client_id,
                    "entity_type": etype,
                    "entity_id": t.entity_id,
                    "observed_at": now,
                    "status": t.status,
                    "daily_budget": t.daily_budget,
                    "learning": t.learning,
                    "source": source.value,
                }
            )
            if (
                prev is not None
                and t.managed
                and (prev.status != t.status or not _money_eq(prev.daily_budget, t.daily_budget))
            ):
                candidates.append((t, prev))
    if inserts:
        session.execute(pg_insert(EntityHistory).values(inserts))
    return len(inserts), candidates


_ENGINE_CHANGE = text(
    """
    SELECT 1 FROM change_log
    WHERE client_id = :client_id AND entity_type = CAST(:entity_type AS entity_type)
      AND entity_id = :entity_id AND status = 'applied' AND applied_at >= :since
      AND (CAST(:status AS text) IS NULL OR after->>'status' = CAST(:status AS text))
      AND (CAST(:budget AS numeric) IS NULL
           OR (after->>'daily_budget')::numeric = CAST(:budget AS numeric))
    LIMIT 1
    """
)


def _confirm_manual(
    session: Session,
    client_id: str,
    connector: ReadConnector,
    candidates: list[tuple[_Tracked, EntityHistory]],
    now: dt.datetime,
) -> list[ManualChange]:
    out: list[ManualChange] = []
    for t, prev in candidates:
        fields: list[str] = []
        if prev.status != t.status:
            fields.append("status")
        if not _money_eq(prev.daily_budget, t.daily_budget):
            fields.append("daily_budget")
        engine_made = session.execute(
            _ENGINE_CHANGE,
            {
                "client_id": client_id,
                "entity_type": t.entity_type.value,
                "entity_id": t.entity_id,
                "since": now - MANUAL_MATCH_WINDOW,
                "status": t.status if "status" in fields else None,
                "budget": (
                    None
                    if "daily_budget" not in fields or t.daily_budget is None
                    else str(t.daily_budget)
                ),
            },
        ).first()
        if engine_made is not None:
            continue
        out.append(
            ManualChange(
                entity_type=t.entity_type,
                entity_id=t.entity_id,
                external_id=t.external_id,
                fields=tuple(fields),
                old={"status": prev.status, "daily_budget": prev.daily_budget},
                new={"status": t.status, "daily_budget": t.daily_budget},
            )
        )
    if not out:
        return out
    try:
        events = connector.change_history(now - CHANGE_HISTORY_LOOKBACK)
    except Exception as exc:  # change history is enrichment only; never fail the sync
        log.warning("ingest.change_history_failed", error=str(exc))
        return out
    return [_with_actor(mc, events) for mc in out]


def _with_actor(mc: ManualChange, events: list[ChangeEvent]) -> ManualChange:
    matches = [
        e
        for e in events
        if e.entity_type is mc.entity_type
        and e.external_id == mc.external_id
        and e.field in mc.fields
    ]
    if not matches:
        return mc
    last = max(matches, key=lambda e: e.changed_at)
    return ManualChange(
        mc.entity_type,
        mc.entity_id,
        mc.external_id,
        mc.fields,
        mc.old,
        mc.new,
        actor=last.actor,
        client_type=last.client_type,
    )
