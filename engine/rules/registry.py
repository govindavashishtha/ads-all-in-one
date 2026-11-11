"""Variable registry (plan §11.1): the ONLY names a rule expression may use.

This file is the contract between the features/scoring code (producers) and the rules
engine (consumer). Foundation defines the names, types and where each value comes from;
builders are implemented by the ranking/rules agent.

``source`` meanings:
- ``entity``       – read from the entity row at evaluation time
- ``targets``      – from the active rules config ``targets`` block
- ``features``     – a column of the ``features`` table (one column per name; enforced by test)
- ``scores``       – a column of the ``scores`` table
- ``search_terms`` – computed in the ``search_terms`` job over 30 days (search terms have no
                     stable per-term entity row, so they are not stored in ``features``)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Source = Literal["entity", "targets", "features", "scores", "search_terms"]
VarType = Literal["str", "num", "bool", "int"]


@dataclass(frozen=True, slots=True)
class Variable:
    name: str
    type: VarType
    source: Source
    nullable: bool = False
    doc: str = ""


def _v(
    name: str, type_: VarType, source: Source, nullable: bool = False, doc: str = ""
) -> Variable:
    return Variable(name, type_, source, nullable, doc)


REGISTRY: tuple[Variable, ...] = (
    _v("platform", "str", "entity"),
    _v("entity_type", "str", "entity"),
    _v("campaign_kind", "str", "entity"),
    _v("objective", "str", "targets"),
    _v("target_cpa", "num", "targets"),
    _v("target_roas", "num", "targets"),
    _v("is_budget_unit", "bool", "features", doc="§8.2"),
    _v("learning", "bool", "features", doc="§8.2; NULL is treated as true"),
    _v("data_sufficient", "bool", "scores", doc="§10.6"),
    _v("hours_live", "num", "features", nullable=True),
    _v("days_live", "num", "features", nullable=True),
    _v("impressions_1d", "num", "features"),
    _v("clicks_1d", "num", "features"),
    _v("spend_1d", "num", "features"),
    _v("conversions_1d", "num", "features"),
    _v("spend_7d", "num", "features"),
    _v("conversions_7d", "num", "features"),
    _v("spend_s14", "num", "features"),
    _v("conversions_s14", "num", "features"),
    _v("impressions_s14", "num", "features"),
    _v("clicks_s14", "num", "features"),
    _v("ctr_1d", "num", "features", nullable=True),
    _v("ctr_7d", "num", "features", nullable=True),
    _v("ctr_28d", "num", "features", nullable=True),
    _v("cpa_1d", "num", "features", nullable=True),
    _v("cpa_7d", "num", "features", nullable=True),
    _v("cpa_28d", "num", "features", nullable=True),
    _v("roas_7d", "num", "features", nullable=True),
    _v("roas_28d", "num", "features", nullable=True),
    _v("frequency_7d", "num", "features", nullable=True, doc="Meta only, from metrics_window"),
    _v("spend_pace_7d", "num", "features", nullable=True, doc="budget units only"),
    _v("fatigue_flag", "bool", "features"),
    _v("cpa_shrunk", "num", "scores"),
    _v("cpa_p90", "num", "scores"),
    _v("roas_shrunk", "num", "scores", nullable=True),
    _v("roas_p10", "num", "scores", nullable=True),
    _v("p_beats_target", "num", "scores"),
    _v("rank_in_peer", "int", "scores"),
    _v("peer_size", "int", "scores"),
    _v("active_ads_in_group", "int", "features"),
    _v("cost_30d", "num", "search_terms"),
    _v("clicks_30d", "num", "search_terms"),
    _v("conversions_30d", "num", "search_terms"),
)

NAMES: frozenset[str] = frozenset(v.name for v in REGISTRY)
ALLOWED_FUNCTIONS: frozenset[str] = frozenset({"min", "max", "abs"})  # §5 simpleeval


def names_from(source: Source) -> frozenset[str]:
    return frozenset(v.name for v in REGISTRY if v.source == source)
