"""LLM price table (plan §13.3). Prices are INPUT I-8 — never hard-coded here.

``cost = (input_tokens × in_price + output_tokens × out_price) / 1,000,000`` with prices per
million tokens (USD) from ``LLM_{SMALL,LARGE}_{IN,OUT}_PRICE_PER_MTOK`` in the environment:

    LLM_SMALL_IN_PRICE_PER_MTOK=   # INPUT I-8
    LLM_SMALL_OUT_PRICE_PER_MTOK=  # INPUT I-8
    LLM_LARGE_IN_PRICE_PER_MTOK=   # INPUT I-8
    LLM_LARGE_OUT_PRICE_PER_MTOK=  # INPUT I-8

Cached input tokens are charged at the full input price (an upper bound: providers bill them
cheaper, so the caps are conservative).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from engine.config import Settings
from engine.enums import LLMTier
from engine.errors import MissingInputError

MTOK = Decimal(1_000_000)
COST_QUANT = Decimal("0.00001")  # llm_calls.cost_usd NUMERIC(10,5)


@dataclass(frozen=True, slots=True)
class TierPrice:
    in_per_mtok: Decimal
    out_per_mtok: Decimal

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        raw = (input_tokens * self.in_per_mtok + output_tokens * self.out_per_mtok) / MTOK
        return raw.quantize(COST_QUANT, rounding=ROUND_CEILING)


ZERO_PRICE = TierPrice(Decimal(0), Decimal(0))


def tier_price(settings: Settings, tier: LLMTier) -> TierPrice | None:
    if tier is LLMTier.SMALL:
        pin, pout = settings.llm_small_in_price_per_mtok, settings.llm_small_out_price_per_mtok
    else:
        pin, pout = settings.llm_large_in_price_per_mtok, settings.llm_large_out_price_per_mtok
    if pin is None or pout is None:
        return None
    if pin < 0 or pout < 0:
        raise ValueError(f"LLM_{tier.value.upper()}_*_PRICE_PER_MTOK must be >= 0")
    return TierPrice(pin, pout)


def require_tier_price(settings: Settings, tier: LLMTier) -> TierPrice:
    """Real providers refuse to run without prices: cost caps can't be enforced otherwise."""
    p = tier_price(settings, tier)
    if p is None:
        raise MissingInputError(
            "I-8",
            f"LLM_{tier.value.upper()}_IN/OUT_PRICE_PER_MTOK not set; the cost caps (§13.3) "
            "cannot be enforced without prices",
        )
    return p
