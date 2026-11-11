"""Search-term rules (plan §9 ``search_terms``, §11.1 ``cost_30d``/``clicks_30d``/
``conversions_30d``, §11.2 ``negative_keyword_candidate``).

Search terms have no stable entity row, so their variables are aggregated here over the 30
account days ending ``as_of`` per ``(ad_group, term)``. The proposal's ``entity_id`` is the
latest ``search_terms.id`` for that pair (AGENT_GUIDE suggestion; DECISIONS D-9) and the term
and ad group travel in ``params``. Terms that already exist as a negative keyword in the ad
group are skipped. Only effectively-active Google ad groups are considered.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.db.models import Keyword, SearchTerm
from engine.enums import EntityType
from engine.features.data import Universe
from engine.features.windows import trailing
from engine.rules.engine import Match, draft_for, first_match, ordered, target_vars
from engine.rules.schema import RulesConfig


@dataclass(frozen=True, slots=True)
class TermAgg:
    search_term_id: int  # latest row id for (ad_group, term)
    ad_group_id: int
    term: str
    cost_30d: Decimal
    clicks_30d: int
    conversions_30d: Decimal


@dataclass(frozen=True, slots=True)
class TermDay:
    id: int
    ad_group_id: int
    term: str
    date: dt.date
    cost: Decimal
    clicks: int
    conversions: Decimal


def load_term_days(
    session: Session, client_id: str, start: dt.date | None, end: dt.date
) -> tuple[list[TermDay], set[tuple[int, str]]]:
    """Raw daily search-term rows (and the ad groups' negative keywords, lower-cased)."""
    q = select(
        SearchTerm.id,
        SearchTerm.ad_group_id,
        SearchTerm.term,
        SearchTerm.date,
        SearchTerm.cost,
        SearchTerm.clicks,
        SearchTerm.conversions,
    ).where(SearchTerm.client_id == client_id, SearchTerm.date <= end)
    if start is not None:
        q = q.where(SearchTerm.date >= start)
    days = [
        TermDay(int(i), int(g), str(t), d, Decimal(c or 0), int(k or 0), Decimal(v or 0))
        for i, g, t, d, c, k, v in session.execute(q)
    ]
    negatives = {
        (int(g), str(t).lower())
        for g, t in session.execute(
            select(Keyword.ad_group_id, Keyword.text).where(
                Keyword.client_id == client_id, Keyword.is_negative.is_(True)
            )
        )
    }
    return days, negatives


def aggregate_terms(
    days: list[TermDay], negatives: set[tuple[int, str]], as_of: dt.date
) -> list[TermAgg]:
    """30-day aggregates ending ``as_of`` per (ad_group, term), negatives excluded."""
    w = trailing(as_of, 30)
    acc: dict[tuple[int, str], list[Any]] = {}
    for r in days:
        if not (w.start <= r.date <= w.end) or (r.ad_group_id, r.term.lower()) in negatives:
            continue
        a = acc.setdefault((r.ad_group_id, r.term), [0, Decimal(0), 0, Decimal(0)])
        a[0] = max(a[0], r.id)
        a[1] += r.cost
        a[2] += r.clicks
        a[3] += r.conversions
    return [TermAgg(int(a[0]), g, t, a[1], int(a[2]), a[3]) for (g, t), a in sorted(acc.items())]


def load_term_aggregates(session: Session, client_id: str, as_of: dt.date) -> list[TermAgg]:
    days, negatives = load_term_days(session, client_id, trailing(as_of, 30).start, as_of)
    return aggregate_terms(days, negatives, as_of)


def run_search_term_rules(
    config: RulesConfig, universe: Universe, terms: list[TermAgg], as_of: dt.date
) -> tuple[list[Match], list[Any]]:
    rules = ordered(config.rules)
    matches: list[Match] = []
    drafts: list[Any] = []
    for t in terms:
        group = universe.entities.get((EntityType.AD_GROUP, t.ad_group_id))
        if group is None or not group.effective_active:
            continue
        names: dict[str, Any] = {
            "platform": group.platform.value,
            "entity_type": EntityType.SEARCH_TERM.value,
            "campaign_kind": group.campaign_kind,
            **target_vars(config.targets),
            "cost_30d": float(t.cost_30d),
            "clicks_30d": float(t.clicks_30d),
            "conversions_30d": float(t.conversions_30d),
        }
        m = first_match(rules, EntityType.SEARCH_TERM, t.search_term_id, names)
        if m is None:
            continue
        matches.append(m)
        d = draft_for(m, as_of, None, {"term": t.term, "ad_group_id": t.ad_group_id})
        if d is not None:
            drafts.append(d)
    return matches, drafts
