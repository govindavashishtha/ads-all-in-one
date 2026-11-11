"""Build ``GuardrailState`` snapshots from the database (plan §12.3 "state is a snapshot").

``StateBuilder`` loads the shared facts once (mode, kill switch, active limits, entities,
active budget units, MTD spend, today's engine pauses) and then snapshots one entity per
proposal. All time logic uses the ad account's local day (§0 rule 9); ``now`` is passed in.
"""

from __future__ import annotations

import calendar
import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engine.config import Settings
from engine.db.models import (
    Campaign,
    ChangeLog,
    Client,
    EntityHistory,
    MetricsDaily,
    MetricsHourly,
    Proposal,
    SearchTerm,
)
from engine.enums import (
    Action,
    AutomationScope,
    BudgetLevel,
    EntityType,
    Mode,
    Platform,
    ProposalSource,
    Risk,
    SpendDirection,
)
from engine.errors import MissingInputError
from engine.features.data import ACTIVE, EntityInfo, Universe, load_universe
from engine.guardrails.check import (
    BudgetUnitSnapshot,
    EntitySnapshot,
    GuardrailState,
    ProposalView,
)
from engine.guardrails.limits_schema import LimitsConfig
from engine.ingest.manual import last_manual_change_at
from engine.jobs.common import active_limits


def proposal_view(p: Proposal) -> ProposalView:
    return ProposalView(
        id=p.id,
        source=ProposalSource(p.source),
        rule_key=p.rule_key,
        entity_type=EntityType(p.entity_type),
        entity_id=int(p.entity_id),
        action=Action(p.action),
        params=dict(p.params or {}),
        risk=Risk(p.risk),
        spend_direction=SpendDirection(p.spend_direction),
        from_pause_all=bool((p.params or {}).get("from_pause_all", False))
        and ProposalSource(p.source) is ProposalSource.EMERGENCY,
    )


def local_midnight(now: dt.datetime, tz: str) -> dt.datetime:
    local = now.astimezone(ZoneInfo(tz))
    return dt.datetime.combine(local.date(), dt.time(), tzinfo=ZoneInfo(tz)).astimezone(dt.UTC)


class EntityMissingError(LookupError):
    pass


class StateBuilder:
    def __init__(
        self,
        session: Session,
        settings: Settings,
        now: dt.datetime,
        *,
        universe: Universe | None = None,
        limits: LimitsConfig | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.client_id = settings.client_id
        self.now = now
        client = session.get(Client, self.client_id)
        if client is None:
            raise MissingInputError("bootstrap", f"clients row {self.client_id!r} missing")
        self.mode = Mode(client.mode)
        self.kill_switch_db = bool(client.kill_switch)
        self.limits = limits or active_limits(session, self.client_id)
        self.universe = universe or load_universe(session, self.client_id)
        self._shared: dict[int, str | None] = dict(
            session.execute(
                select(Campaign.id, Campaign.shared_budget_external_id).where(
                    Campaign.client_id == self.client_id
                )
            ).all()
        )
        self.budget_units = self._active_budget_units()
        self.tz_by_platform: dict[Platform, str] = {
            pl: tz for pl, tz in self.universe.accounts.values()
        }
        self.mtd = self._mtd_spend()
        self.pauses_today = self._auto_pauses_today()

    # ------------------------------------------------------------------ shared facts

    def _active_budget_units(self) -> tuple[BudgetUnitSnapshot, ...]:
        """Every active budget unit on both platforms, managed or not (§12.3 check 9).
        Shared Google budgets count once per shared budget id."""
        out: list[BudgetUnitSnapshot] = []
        seen_shared: set[str] = set()
        for e in self.universe.of_type(EntityType.CAMPAIGN, EntityType.AD_GROUP):
            if not e.effective_active or e.daily_budget is None:
                continue
            if e.entity_type is EntityType.CAMPAIGN and e.budget_level == BudgetLevel.SHARED.value:
                sid = self._shared.get(e.id) or f"campaign:{e.id}"
                if sid in seen_shared:
                    continue
                seen_shared.add(sid)
            elif not e.is_budget_unit:
                continue
            out.append(BudgetUnitSnapshot(e.platform, e.entity_type, e.id, e.daily_budget))
        return tuple(out)

    def _mtd_spend(self) -> dict[Platform, Decimal]:
        """Month-to-date spend per platform: settled daily rows this month + today's hourly."""
        out: dict[Platform, Decimal] = {pl: Decimal(0) for pl in Platform}
        for acct_id, (pl, tz) in self.universe.accounts.items():
            today = self.now.astimezone(ZoneInfo(tz)).date()
            first = today.replace(day=1)
            daily = self.session.scalar(
                select(func.coalesce(func.sum(MetricsDaily.spend), 0)).where(
                    MetricsDaily.client_id == self.client_id,
                    MetricsDaily.entity_type == EntityType.ACCOUNT,
                    MetricsDaily.entity_id == acct_id,
                    MetricsDaily.date >= first,
                    MetricsDaily.date < today,
                )
            )
            start = dt.datetime.combine(today, dt.time())
            hourly = self.session.scalar(
                select(func.coalesce(func.sum(MetricsHourly.spend), 0)).where(
                    MetricsHourly.client_id == self.client_id,
                    MetricsHourly.entity_type == EntityType.ACCOUNT,
                    MetricsHourly.entity_id == acct_id,
                    MetricsHourly.ts_hour >= start,
                    MetricsHourly.ts_hour < start + dt.timedelta(days=1),
                )
            )
            out[pl] += Decimal(daily or 0) + Decimal(hourly or 0)
        return out

    def _auto_pauses_today(self) -> int:
        """Engine pauses applied since the client's local midnight (emergency excluded:
        those have no proposal). Counts approved ones too (conservative)."""
        since = local_midnight(self.now, self.settings.timezone)
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(ChangeLog)
                .where(
                    ChangeLog.client_id == self.client_id,
                    ChangeLog.action == Action.PAUSE.value,
                    ChangeLog.proposal_id.is_not(None),
                    ChangeLog.applied_at >= since,
                )
            )
            or 0
        )

    def remaining_days(self, tz: str) -> int:
        today = self.now.astimezone(ZoneInfo(tz)).date()
        last = calendar.monthrange(today.year, today.month)[1]
        return last - today.day + 1

    # ------------------------------------------------------------------ per entity

    def resolve(self, entity_type: EntityType, entity_id: int) -> EntityInfo:
        """The entity whose campaign/scope governs the proposal (search terms → ad group)."""
        if entity_type is EntityType.SEARCH_TERM:
            group_id = self.session.scalar(
                select(SearchTerm.ad_group_id).where(
                    SearchTerm.client_id == self.client_id, SearchTerm.id == entity_id
                )
            )
            if group_id is None:
                raise EntityMissingError(f"search_term {entity_id} not found")
            info = self.universe.entities.get((EntityType.AD_GROUP, int(group_id)))
        else:
            info = self.universe.entities.get((entity_type, entity_id))
        if info is None:
            raise EntityMissingError(f"{entity_type.value} {entity_id} not found")
        return info

    def _start_of_day_budget(self, e: EntityInfo) -> Decimal | None:
        """Budget in the latest entity_history row at/before the account day's start; if the
        history starts later, the earliest row (DECISIONS D-13); None → check uses current."""
        start = local_midnight(self.now, e.timezone)
        base = select(EntityHistory.daily_budget).where(
            EntityHistory.client_id == self.client_id,
            EntityHistory.entity_type == e.entity_type,
            EntityHistory.entity_id == e.id,
        )
        row = self.session.execute(
            base.where(EntityHistory.observed_at <= start)
            .order_by(EntityHistory.observed_at.desc(), EntityHistory.id.desc())
            .limit(1)
        ).first()
        if row is None:
            row = self.session.execute(
                base.order_by(EntityHistory.observed_at, EntityHistory.id).limit(1)
            ).first()
        return None if row is None or row[0] is None else Decimal(row[0])

    def _last_applied(self, entity_type: EntityType, entity_id: int) -> dt.datetime | None:
        return self.session.scalar(
            select(func.max(ChangeLog.applied_at)).where(
                ChangeLog.client_id == self.client_id,
                ChangeLog.entity_type == entity_type,
                ChangeLog.entity_id == entity_id,
                ChangeLog.applied_at.is_not(None),
            )
        )

    def _budget_if_enabled(self, e: EntityInfo) -> Decimal:
        if e.status == ACTIVE:
            return Decimal(0)
        if e.is_budget_unit and e.daily_budget is not None:
            return e.daily_budget
        if e.entity_type is EntityType.CAMPAIGN and e.budget_level == BudgetLevel.AD_SET.value:
            return sum(
                (
                    g.daily_budget
                    for g in self.universe.of_type(EntityType.AD_GROUP)
                    if g.campaign_id == e.id and g.status == ACTIVE and g.daily_budget
                ),
                Decimal(0),
            )
        return Decimal(0)

    def snapshot(self, entity_type: EntityType, entity_id: int) -> EntitySnapshot:
        e = self.resolve(entity_type, entity_id)
        is_term = entity_type is EntityType.SEARCH_TERM
        manual = last_manual_change_at(self.session, self.client_id, e.entity_type, e.id)
        return EntitySnapshot(
            platform=e.platform,
            entity_type=entity_type,
            entity_id=entity_id,
            campaign_id=e.campaign_id,
            campaign_managed=e.campaign_managed,
            automation_scope=AutomationScope(e.automation_scope),
            budget_level=BudgetLevel(e.budget_level),
            is_budget_unit=False if is_term else e.is_budget_unit,
            learning=e.learning_effective,
            status=e.status,
            current_daily_budget=None if is_term or not e.is_budget_unit else e.daily_budget,
            start_of_day_budget=None
            if is_term or not e.is_budget_unit
            else self._start_of_day_budget(e),
            active_ads_in_group=self.universe.active_ads_in_group.get(e.ad_group_id or -1, 0)
            if e.ad_group_id is not None
            else None,
            last_applied_change_at=self._last_applied(entity_type, entity_id),
            last_manual_change_at=manual,
            budget_if_enabled=None if is_term else self._budget_if_enabled(e),
        )

    def state(self, entity_type: EntityType, entity_id: int) -> GuardrailState:
        snap = self.snapshot(entity_type, entity_id)
        tz = self.tz_by_platform.get(snap.platform, self.settings.timezone)
        return GuardrailState(
            mode=self.mode,
            kill_switch_db=self.kill_switch_db,
            kill_switch_env=self.settings.kill_switch,
            limits=self.limits,
            entity=snap,
            active_budget_units=self.budget_units,
            mtd_spend=dict(self.mtd),
            remaining_days_in_month=self.remaining_days(tz),
            auto_pauses_applied_today=self.pauses_today,
            now=self.now,
        )
