"""Token budget controller (plan §13.3): all three caps, checked before every call.

    skip if  tokens_today + est_tokens > LLM_DAILY_TOKEN_CAP
          or cost_today  + est_cost   > LLM_DAILY_COST_CAP_USD
          or cost_month  + est_cost   > LLM_MONTHLY_COST_CAP_USD

Usage is summed from ``llm_calls`` for the current client-timezone day and month (every
recorded provider call counts, including ones whose output was then rejected). The estimate
is an upper bound: input ≈ characters ÷ 3 (pessimistic; real tokenisers average ~4) plus the
request's full ``max_output_tokens``. Because the output length is capped and the check uses
the upper bound, a call that passes cannot push usage past a cap by more than the estimate
error on the input side; once any cap is reached every later call is refused (hard stop).

Priority near a cap (§13.3: anomaly triage > analyst > digest > search terms > creative): lower
priorities may only use a fraction of each cap, so the remainder stays available for triage.
The fractions are engineering knobs (DECISIONS D-22), not business inputs.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from engine.config import Settings
from engine.db.models import LLMCall
from engine.llm.client import LLMRequest
from engine.llm.prices import TierPrice

PRIORITY_SHARE: dict[str, Decimal] = {
    "triage": Decimal("1.00"),
    "analyst": Decimal("0.90"),
    "digest": Decimal("0.80"),
    "search_terms": Decimal("0.75"),
    "creative": Decimal("0.70"),
}
CHARS_PER_TOKEN_ESTIMATE = 3


@dataclass(frozen=True, slots=True)
class Usage:
    tokens_today: int
    cost_today: Decimal
    cost_month: Decimal


@dataclass(frozen=True, slots=True)
class Estimate:
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    cap: str | None = None  # daily_tokens | daily_cost | monthly_cost
    detail: str = ""


def estimate(request: LLMRequest, price: TierPrice) -> Estimate:
    chars = len(request.system) + sum(len(m.content) for m in request.messages)
    tin = math.ceil(chars / CHARS_PER_TOKEN_ESTIMATE)
    tout = request.max_output_tokens
    return Estimate(tin, tout, price.cost(tin, tout))


def local_bounds(now: dt.datetime, tz: str) -> tuple[dt.datetime, dt.datetime, dt.datetime]:
    """(start of today, start of tomorrow, start of month) in the client timezone, as UTC."""
    z = ZoneInfo(tz)
    local = now.astimezone(z)
    day = dt.datetime(local.year, local.month, local.day, tzinfo=z)
    month = dt.datetime(local.year, local.month, 1, tzinfo=z)
    nxt = day + dt.timedelta(days=1)
    nxt = dt.datetime(nxt.year, nxt.month, nxt.day, tzinfo=z)  # DST-safe midnight
    return day.astimezone(dt.UTC), nxt.astimezone(dt.UTC), month.astimezone(dt.UTC)


def current_usage(session: Session, client_id: str, now: dt.datetime, tz: str) -> Usage:
    day, nxt, month = local_bounds(now, tz)
    base = select(
        func.coalesce(func.sum(LLMCall.input_tokens + LLMCall.output_tokens), 0),
        func.coalesce(func.sum(LLMCall.cost_usd), 0),
    ).where(LLMCall.client_id == client_id, LLMCall.created_at < nxt)
    t_today, c_today = session.execute(base.where(LLMCall.created_at >= day)).one()
    _t, c_month = session.execute(base.where(LLMCall.created_at >= month)).one()
    return Usage(int(t_today), Decimal(c_today), Decimal(c_month))


class TokenBudget:
    def __init__(self, settings: Settings) -> None:
        self.daily_tokens = settings.llm_daily_token_cap
        self.daily_cost = settings.llm_daily_cost_cap_usd
        self.monthly_cost = settings.llm_monthly_cost_cap_usd

    def check(self, usage: Usage, purpose: str, est: Estimate) -> BudgetDecision:
        share = PRIORITY_SHARE.get(purpose, min(PRIORITY_SHARE.values()))
        tok_cap = int(self.daily_tokens * share)
        if usage.tokens_today + est.tokens > tok_cap:
            return BudgetDecision(
                False,
                "daily_tokens",
                f"{usage.tokens_today} + est {est.tokens} > {tok_cap} tokens "
                f"({share:.0%} of LLM_DAILY_TOKEN_CAP for {purpose})",
            )
        day_cap = self.daily_cost * share
        if usage.cost_today + est.cost_usd > day_cap:
            return BudgetDecision(
                False,
                "daily_cost",
                f"${usage.cost_today} + est ${est.cost_usd} > ${day_cap:.5f} "
                f"({share:.0%} of LLM_DAILY_COST_CAP_USD for {purpose})",
            )
        month_cap = self.monthly_cost * share
        if usage.cost_month + est.cost_usd > month_cap:
            return BudgetDecision(
                False,
                "monthly_cost",
                f"${usage.cost_month} + est ${est.cost_usd} > ${month_cap:.5f} "
                f"({share:.0%} of LLM_MONTHLY_COST_CAP_USD for {purpose})",
            )
        return BudgetDecision(True)
