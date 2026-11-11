"""In-memory snapshot of entities and daily metrics used by features, scoring, rules, the
weekly allocation and the backtest (plan §10, §11).

One loader, one implementation: the daily pipeline loads the windows it needs for one
``as_of``; the backtest loads the whole history once and replays many ``as_of`` dates over it.
Money stays ``Decimal`` (major units, §0 rule 4); conversions are ``Decimal`` (NUMERIC 14,4).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.db.models import (
    Ad,
    AdAccount,
    AdGroup,
    Campaign,
    EntityHistory,
    Keyword,
    MetricsDaily,
    MetricsWindow,
)
from engine.enums import AutomationScope, BudgetLevel, EntityType, Platform

ZERO = Decimal(0)
ACTIVE = "active"
REMOVED = "removed"

EntityKey = tuple[EntityType, int]


@dataclass(frozen=True, slots=True)
class Totals:
    impressions: int = 0
    clicks: int = 0
    spend: Decimal = ZERO
    conversions: Decimal = ZERO
    conv_value: Decimal = ZERO

    def __add__(self, o: Totals) -> Totals:
        return Totals(
            self.impressions + o.impressions,
            self.clicks + o.clicks,
            self.spend + o.spend,
            self.conversions + o.conversions,
            self.conv_value + o.conv_value,
        )

    def __sub__(self, o: Totals) -> Totals:
        """Leave-one-out (§10.1); floored at zero so a parent never goes negative."""
        return Totals(
            max(self.impressions - o.impressions, 0),
            max(self.clicks - o.clicks, 0),
            max(self.spend - o.spend, ZERO),
            max(self.conversions - o.conversions, ZERO),
            max(self.conv_value - o.conv_value, ZERO),
        )


@dataclass(frozen=True, slots=True)
class EntityInfo:
    """One entity with everything rules/guardrails need from its ancestors (§8.2)."""

    entity_type: EntityType
    id: int
    platform: Platform
    name: str | None
    ad_account_id: int
    campaign_id: int
    ad_group_id: int | None  # for ads / keywords / ad groups (self)
    status: str | None
    effective_active: bool  # self and every ancestor active
    campaign_kind: str
    campaign_objective: str | None
    automation_scope: str
    budget_level: str
    campaign_managed: bool
    pool: str | None
    daily_budget: Decimal | None  # own budget (campaign or ad set); None otherwise
    learning_raw: bool | None  # as stored on the row (campaign / ad group only)
    learning_effective: (
        bool | None
    )  # §8.2 + DECISIONS: ad -> its ad group; Google group -> campaign
    is_budget_unit: bool
    first_live_at: dt.datetime | None = None  # ads
    match_type: str | None = None  # keywords
    timezone: str = "UTC"  # ad account timezone

    @property
    def key(self) -> EntityKey:
        return (self.entity_type, self.id)

    @property
    def learning(self) -> bool:
        """Registry ``learning``: NULL is treated as true (§8.2, §11.1)."""
        return True if self.learning_effective is None else self.learning_effective


@dataclass
class Universe:
    entities: dict[EntityKey, EntityInfo]
    accounts: dict[int, tuple[Platform, str]]  # ad_account_id -> (platform, timezone)
    active_ads_in_group: dict[int, int]

    def of_type(self, *types: EntityType) -> list[EntityInfo]:
        return sorted(
            (e for e in self.entities.values() if e.entity_type in types),
            key=lambda e: (e.entity_type.value, e.id),
        )

    def with_overrides(self, managed: bool | None = None) -> Universe:
        """Copy with every campaign's ``managed`` forced (backtest ``--assume-managed``)."""
        if managed is None:
            return self
        ents = {k: replace(e, campaign_managed=managed) for k, e in self.entities.items()}
        return Universe(ents, dict(self.accounts), dict(self.active_ads_in_group))


def _budget_unit(entity_type: EntityType, budget_level: str) -> bool:
    """§8.2: campaign with campaign-level (non-shared) budget, or a Meta ad set under ABO."""
    if entity_type is EntityType.CAMPAIGN:
        return budget_level == BudgetLevel.CAMPAIGN.value
    if entity_type is EntityType.AD_GROUP:
        return budget_level == BudgetLevel.AD_SET.value
    return False


def load_universe(session: Session, client_id: str) -> Universe:
    accounts = {
        a.id: (Platform(a.platform), a.timezone)
        for a in session.scalars(select(AdAccount).where(AdAccount.client_id == client_id))
    }
    camps = {
        c.id: c for c in session.scalars(select(Campaign).where(Campaign.client_id == client_id))
    }
    groups = {
        g.id: g for g in session.scalars(select(AdGroup).where(AdGroup.client_id == client_id))
    }
    ads = list(session.scalars(select(Ad).where(Ad.client_id == client_id)))
    kws = list(
        session.scalars(
            select(Keyword).where(Keyword.client_id == client_id, Keyword.is_negative.is_(False))
        )
    )
    out: dict[EntityKey, EntityInfo] = {}

    def camp_fields(c: Campaign) -> dict[str, object]:
        return {
            "ad_account_id": c.ad_account_id,
            "campaign_id": c.id,
            "campaign_kind": c.kind,
            "campaign_objective": c.objective,
            "automation_scope": c.automation_scope,
            "budget_level": c.budget_level,
            "campaign_managed": bool(c.managed),
            "pool": c.pool,
            "timezone": accounts.get(c.ad_account_id, (Platform.META, "UTC"))[1],
        }

    group_learning: dict[int, bool | None] = {}
    for c in camps.values():
        out[(EntityType.CAMPAIGN, c.id)] = EntityInfo(
            entity_type=EntityType.CAMPAIGN,
            id=c.id,
            platform=Platform(c.platform),
            name=c.name,
            ad_group_id=None,
            status=c.status,
            effective_active=c.status == ACTIVE,
            daily_budget=c.daily_budget,
            learning_raw=c.learning,
            learning_effective=c.learning,
            is_budget_unit=_budget_unit(EntityType.CAMPAIGN, c.budget_level),
            **camp_fields(c),  # type: ignore[arg-type]
        )
    for g in groups.values():
        c = camps[g.campaign_id]
        # Google learning lives on the campaign (bidding strategy status, §8.2)
        eff = g.learning if Platform(g.platform) is Platform.META else c.learning
        group_learning[g.id] = eff
        out[(EntityType.AD_GROUP, g.id)] = EntityInfo(
            entity_type=EntityType.AD_GROUP,
            id=g.id,
            platform=Platform(g.platform),
            name=g.name,
            ad_group_id=g.id,
            status=g.status,
            effective_active=g.status == ACTIVE and c.status == ACTIVE,
            daily_budget=g.daily_budget,
            learning_raw=g.learning,
            learning_effective=eff,
            is_budget_unit=_budget_unit(EntityType.AD_GROUP, c.budget_level),
            **camp_fields(c),  # type: ignore[arg-type]
        )
    active_in_group: dict[int, int] = defaultdict(int)
    for a in ads:
        if a.status == ACTIVE:
            active_in_group[a.ad_group_id] += 1
    for a in ads:
        g = groups[a.ad_group_id]
        c = camps[g.campaign_id]
        out[(EntityType.AD, a.id)] = EntityInfo(
            entity_type=EntityType.AD,
            id=a.id,
            platform=Platform(a.platform),
            name=a.name,
            ad_group_id=g.id,
            status=a.status,
            effective_active=a.status == ACTIVE and g.status == ACTIVE and c.status == ACTIVE,
            daily_budget=None,
            learning_raw=None,
            learning_effective=group_learning.get(g.id),
            is_budget_unit=False,
            first_live_at=a.first_live_at,
            **camp_fields(c),  # type: ignore[arg-type]
        )
    for k in kws:
        g = groups[k.ad_group_id]
        c = camps[g.campaign_id]
        out[(EntityType.KEYWORD, k.id)] = EntityInfo(
            entity_type=EntityType.KEYWORD,
            id=k.id,
            platform=Platform(k.platform),
            name=k.text,
            ad_group_id=g.id,
            status=k.status,
            effective_active=k.status == ACTIVE and g.status == ACTIVE and c.status == ACTIVE,
            daily_budget=None,
            learning_raw=None,
            learning_effective=group_learning.get(g.id),
            is_budget_unit=False,
            match_type=k.match_type,
            **camp_fields(c),  # type: ignore[arg-type]
        )
    return Universe(out, accounts, dict(active_in_group))


@dataclass
class MetricsStore:
    """Daily metrics keyed by entity; account rows keyed by ``(ACCOUNT, ad_account_id)``."""

    days: dict[EntityKey, dict[dt.date, Totals]] = field(default_factory=dict)
    frequency: dict[tuple[EntityKey, dt.date], float] = field(default_factory=dict)
    first_active_at: dict[EntityKey, dt.datetime] = field(default_factory=dict)
    min_date: dt.date | None = None
    max_date: dt.date | None = None

    def window(self, key: EntityKey, start: dt.date, end: dt.date) -> Totals:
        series = self.days.get(key)
        if not series or end < start:
            return Totals()
        total = Totals()
        d = start
        while d <= end:
            t = series.get(d)
            if t is not None:
                total = total + t
            d += dt.timedelta(days=1)
        return total


def load_metrics(
    session: Session, client_id: str, start: dt.date | None, end: dt.date
) -> MetricsStore:
    store = MetricsStore()
    q = select(
        MetricsDaily.entity_type,
        MetricsDaily.entity_id,
        MetricsDaily.date,
        MetricsDaily.impressions,
        MetricsDaily.clicks,
        MetricsDaily.spend,
        MetricsDaily.conversions,
        MetricsDaily.conv_value,
    ).where(MetricsDaily.client_id == client_id, MetricsDaily.date <= end)
    if start is not None:
        q = q.where(MetricsDaily.date >= start)
    for et, eid, d, imp, clk, spend, conv, val in session.execute(q):
        key = (EntityType(et), int(eid))
        store.days.setdefault(key, {})[d] = Totals(
            int(imp or 0),
            int(clk or 0),
            Decimal(spend or 0),
            Decimal(conv or 0),
            Decimal(val or 0),
        )
        store.min_date = d if store.min_date is None else min(store.min_date, d)
        store.max_date = d if store.max_date is None else max(store.max_date, d)
    wq = select(
        MetricsWindow.entity_type,
        MetricsWindow.entity_id,
        MetricsWindow.as_of,
        MetricsWindow.frequency,
    ).where(
        MetricsWindow.client_id == client_id,
        MetricsWindow.window_days == 7,
        MetricsWindow.as_of <= end,
    )
    if start is not None:
        wq = wq.where(MetricsWindow.as_of >= start)
    for et, eid, as_of, freq in session.execute(wq):
        if freq is not None:
            store.frequency[((EntityType(et), int(eid)), as_of)] = float(freq)
    for et, eid, first in session.execute(
        select(EntityHistory.entity_type, EntityHistory.entity_id, EntityHistory.observed_at)
        .where(EntityHistory.client_id == client_id, EntityHistory.status == ACTIVE)
        .order_by(EntityHistory.observed_at)
    ):
        store.first_active_at.setdefault((EntityType(et), int(eid)), first)
    return store


def account_day_end(as_of: dt.date, tz: str) -> dt.datetime:
    """UTC instant at which account day ``as_of`` ends (local midnight of the next day)."""
    nxt = dt.datetime.combine(as_of + dt.timedelta(days=1), dt.time(), tzinfo=ZoneInfo(tz))
    return nxt.astimezone(dt.UTC)


def scope_campaign_only(e: EntityInfo) -> bool:
    return e.automation_scope == AutomationScope.CAMPAIGN_ONLY.value
