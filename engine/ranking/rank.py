"""Scores and peer ranking (plan §10.1, §10.5, §10.6). Writes the ``scores`` table.

Only entities that are *effectively active* (self + ancestors active) are scored and ranked;
paused / removed entities keep their features but get no score (DECISIONS D-4).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from engine.db.models import Score
from engine.enums import EntityType, Platform
from engine.features.builder import FEATURE_TYPES, money
from engine.features.data import EntityInfo, EntityKey, MetricsStore, Totals, Universe
from engine.features.windows import prior_window, scoring_window
from engine.ranking import posteriors as post
from engine.rules.schema import RulesConfig

SCORE_COLUMNS = (
    "ctr_post_mean",
    "ctr_p10",
    "ctr_p90",
    "theta_a",
    "theta_b",
    "cpa_shrunk",
    "cpa_p10",
    "cpa_p90",
    "aov_shrunk",
    "roas_shrunk",
    "roas_p10",
    "roas_p90",
    "p_beats_target",
    "rank_metric",
    "rank_in_peer",
    "peer_size",
    "peer_group",
    "composite",
    "data_sufficient",
)

# §10.6 thresholds (fixed by the plan)
META_AD_MIN_IMPRESSIONS = 1000
META_AD_MIN_HOURS = 72
GOOGLE_KW_MIN_CLICKS = 100
GOOGLE_AD_GROUP_MIN_IMPRESSIONS = 1000
GOOGLE_AD_MIN_IMPRESSIONS = 300


def peer_group(e: EntityInfo) -> str:
    """§10.1 peer groups. Google ad groups (not listed in §10.1) peer within their campaign."""
    if e.entity_type is EntityType.AD:
        return f"ad|ad_group:{e.ad_group_id}"
    if e.entity_type is EntityType.AD_GROUP:
        return f"ad_group|campaign:{e.campaign_id}"
    if e.entity_type is EntityType.KEYWORD:
        return f"keyword|ad_group:{e.ad_group_id}|match:{(e.match_type or '').lower()}"
    return f"campaign|{e.platform.value}|objective:{e.campaign_objective or ''}"


@dataclass(frozen=True, slots=True)
class Prior:
    source: str  # campaign | account | target
    ctr: float
    theta: float
    aov: float | None


def _prior_from(
    t: Totals, target_cpa: float, fallback_aov: float | None
) -> tuple[float, float, float | None]:
    ctr = t.clicks / t.impressions if t.impressions > 0 else post.CTR_FALLBACK
    theta = float(t.conversions / t.spend) if t.spend > 0 else 1 / target_cpa
    aov = float(t.conv_value / t.conversions) if t.conversions > 0 else fallback_aov
    return ctr, theta, aov


def choose_prior(e: EntityInfo, store: MetricsStore, as_of: dt.date, rules: RulesConfig) -> Prior:
    """§10.1: parent (leave-one-out) → ad account (leave-one-out) → the target itself."""
    tg = rules.targets
    target_cpa = float(tg.target_cpa)
    fallback_aov = float(tg.target_cpa * tg.target_roas) if tg.target_roas is not None else None
    w = prior_window(as_of, rules.stats)
    own = store.window(e.key, w.start, w.end)
    floor = rules.stats.min_parent_spend_x_cpa * tg.target_cpa
    account = store.window((EntityType.ACCOUNT, e.ad_account_id), w.start, w.end) - own
    candidates: list[tuple[str, Totals]] = []
    if e.entity_type is not EntityType.CAMPAIGN:
        parent = store.window((EntityType.CAMPAIGN, e.campaign_id), w.start, w.end) - own
        candidates.append(("campaign", parent))
    candidates.append(("account", account))
    for source, totals in candidates:
        if totals.spend >= floor and totals.spend > 0:
            c, th, aov = _prior_from(totals, target_cpa, fallback_aov)
            return Prior(source, c, th, aov)
    return Prior("target", post.CTR_FALLBACK, 1 / target_cpa, fallback_aov)


def data_sufficient(
    e: EntityInfo,
    s14: Totals,
    group_s14: Totals | None,
    hours_live: float | None,
    target_cpa: Decimal,
) -> bool:
    """§10.6 minimum data, over the scoring window."""
    if e.entity_type is EntityType.CAMPAIGN:
        return s14.spend >= 5 * target_cpa and not e.learning
    if e.entity_type is EntityType.AD_GROUP:
        # Meta ad set rule; also applied to Google ad groups (DECISIONS D-5)
        return not e.learning and s14.spend >= 3 * target_cpa
    if e.entity_type is EntityType.KEYWORD:
        return s14.clicks >= GOOGLE_KW_MIN_CLICKS or s14.spend >= 2 * target_cpa
    if e.entity_type is EntityType.AD:
        if e.platform is Platform.META:
            return (
                s14.impressions >= META_AD_MIN_IMPRESSIONS
                and s14.spend >= target_cpa
                and hours_live is not None
                and hours_live >= META_AD_MIN_HOURS
            )
        return (
            group_s14 is not None
            and group_s14.impressions >= GOOGLE_AD_GROUP_MIN_IMPRESSIONS
            and s14.impressions >= GOOGLE_AD_MIN_IMPRESSIONS
        )
    return False


def _finite(x: float) -> float | None:
    return x if math.isfinite(x) else None


def _money_or_none(x: float | None) -> Decimal | None:
    if x is None or not math.isfinite(x):
        return None
    return money(Decimal(str(x)))


def score_entity(
    e: EntityInfo,
    store: MetricsStore,
    features: dict[str, Any],
    as_of: dt.date,
    rules: RulesConfig,
) -> dict[str, Any]:
    tg, st = rules.targets, rules.stats
    target_cpa = float(tg.target_cpa)
    w = scoring_window(as_of, st)
    s14 = store.window(e.key, w.start, w.end)
    group_s14 = (
        store.window((EntityType.AD_GROUP, e.ad_group_id), w.start, w.end)
        if e.ad_group_id is not None
        else None
    )
    prior = choose_prior(e, store, as_of, rules)
    ctr = post.ctr_posterior(s14.impressions, s14.clicks, prior.ctr, st.ctr_prior_impressions)
    theta = post.theta_posterior(
        float(s14.conversions),
        float(s14.spend),
        prior.theta,
        target_cpa,
        float(st.theta_prior_spend_x_cpa),
    )
    roas: post.RoasPosterior | None = None
    if tg.objective == "value" and prior.aov is not None:
        roas = post.roas_posterior(
            theta,
            float(s14.conversions),
            float(s14.conv_value),
            prior.aov,
            float(st.aov_prior_conversions),
        )
    if tg.objective == "conversions":
        p = post.p_beats_cpa(theta, target_cpa)
        metric: float | None = _finite(theta.cpa_p90)
    elif tg.objective == "value":
        if roas is None or tg.target_roas is None:
            p, metric = 0.0, None  # no value data and no target ROAS: never beats (conservative)
        else:
            p = post.p_beats_roas(theta, float(tg.target_roas), roas.aov_shrunk)
            metric = roas.roas_p10
    else:  # traffic
        p = post.p_beats_ctr(ctr, prior.ctr)
        metric = ctr.p10
    return {
        "ctr_post_mean": ctr.mean,
        "ctr_p10": ctr.p10,
        "ctr_p90": ctr.p90,
        "theta_a": theta.a,
        "theta_b": theta.b,
        "cpa_shrunk": _money_or_none(theta.cpa_shrunk),
        "cpa_p10": _money_or_none(theta.cpa_p10),
        "cpa_p90": _money_or_none(theta.cpa_p90),
        "aov_shrunk": _money_or_none(roas.aov_shrunk) if roas else None,
        "roas_shrunk": roas.roas_shrunk if roas else None,
        "roas_p10": roas.roas_p10 if roas else None,
        "roas_p90": roas.roas_p90 if roas else None,
        "p_beats_target": p,
        "rank_metric": metric,
        "rank_in_peer": None,
        "peer_size": None,
        "peer_group": peer_group(e),
        "composite": post.composite(p, bool(features.get("fatigue_flag"))),
        "data_sufficient": data_sufficient(
            e, s14, group_s14, features.get("hours_live"), tg.target_cpa
        ),
        "_spend_s14": s14.spend,  # tie-break only, not stored
        "_prior_source": prior.source,
    }


def assign_ranks(scores: dict[EntityKey, dict[str, Any]], objective: str) -> None:
    """§10.5: position by rank_metric within the peer group (1 = best); ties → higher spend,
    then lower id. ``conversions`` ranks cpa_p90 ascending; ``value``/``traffic`` descending.
    Entities with no metric rank last."""
    ascending = objective == "conversions"
    groups: dict[str, list[EntityKey]] = {}
    for key, s in scores.items():
        groups.setdefault(s["peer_group"], []).append(key)

    def sort_key(key: EntityKey) -> tuple[int, float, Decimal, int]:
        s = scores[key]
        m = s["rank_metric"]
        if m is None:
            return (1, 0.0, -s["_spend_s14"], key[1])
        return (0, m if ascending else -m, -s["_spend_s14"], key[1])

    for members in groups.values():
        ordered = sorted(members, key=sort_key)
        for i, key in enumerate(ordered, start=1):
            scores[key]["rank_in_peer"] = i
            scores[key]["peer_size"] = len(ordered)


def compute_scores(
    universe: Universe,
    store: MetricsStore,
    features: dict[EntityKey, dict[str, Any]],
    as_of: dt.date,
    rules: RulesConfig,
) -> dict[EntityKey, dict[str, Any]]:
    out: dict[EntityKey, dict[str, Any]] = {}
    for e in universe.of_type(*FEATURE_TYPES):
        if not e.effective_active or e.key not in features:
            continue
        out[e.key] = score_entity(e, store, features[e.key], as_of, rules)
    assign_ranks(out, rules.targets.objective)
    return out


def public(score: dict[str, Any]) -> dict[str, Any]:
    return {k: score[k] for k in SCORE_COLUMNS}


def upsert_scores(
    session: Session, client_id: str, as_of: dt.date, scores: dict[EntityKey, dict[str, Any]]
) -> int:
    if not scores:
        return 0
    values = [
        {"client_id": client_id, "entity_type": et, "entity_id": eid, "as_of": as_of, **public(s)}
        for (et, eid), s in sorted(scores.items(), key=lambda kv: (kv[0][0].value, kv[0][1]))
    ]
    stmt = insert(Score).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["client_id", "entity_type", "entity_id", "as_of"],
        set_={c: stmt.excluded[c] for c in SCORE_COLUMNS},
    )
    session.execute(stmt)
    return len(values)
