"""The ONLY place that converts platform money units (plan §0 rule 4, §8.1).

Outside engine/connectors everything is ``Decimal`` in major units (e.g. INR), rounded to
2 decimals (NUMERIC(14,2)). Platform facts below are `VERIFY-P0` — record the doc URL and
date in docs/VERIFIED.md before relying on them:

- Google Ads amounts are micros: major = micros / 1_000_000.            VERIFY-P0
- Meta budget fields are integers in the currency's minor unit (offset per currency, e.g.
  100 for INR/USD). Meta insights ``spend`` is a decimal string in major units.  VERIFY-P0
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MICROS_PER_UNIT = 1_000_000
CENTS = Decimal("0.01")

# Meta minor-unit offsets. Add a currency only after confirming it in Meta's currency docs
# (VERIFY-P0) and recording it in docs/VERIFIED.md. Unknown currencies fail loudly.
META_CURRENCY_OFFSETS: dict[str, int] = {
    "INR": 100,  # VERIFY-P0
    "USD": 100,  # VERIFY-P0
}


class MoneyConversionError(ValueError):
    pass


def quantize_major(value: Decimal) -> Decimal:
    """Round to 2 decimals, half-up (storage precision, §8.1)."""
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


# ---- Google ----------------------------------------------------------------


def google_micros_to_major(micros: int) -> Decimal:
    if isinstance(micros, bool) or not isinstance(micros, int):
        raise MoneyConversionError(f"micros must be int, got {type(micros).__name__}")
    return quantize_major(Decimal(micros) / MICROS_PER_UNIT)


def major_to_google_micros(amount: Decimal) -> int:
    _require_decimal(amount)
    micros = amount * MICROS_PER_UNIT
    if micros != micros.to_integral_value():
        raise MoneyConversionError(f"{amount} is not representable in micros")
    return int(micros)


# ---- Meta ------------------------------------------------------------------


def meta_offset(currency: str) -> int:
    try:
        return META_CURRENCY_OFFSETS[currency.upper()]
    except KeyError as exc:
        raise MoneyConversionError(
            f"Meta minor-unit offset for {currency!r} not verified (add to META_CURRENCY_OFFSETS "
            "after VERIFY-P0)"
        ) from exc


def meta_minor_to_major(minor: int | str, currency: str) -> Decimal:
    """Meta budget field (integer minor units, sometimes serialised as a string)."""
    try:
        value = Decimal(str(minor))
    except InvalidOperation as exc:
        raise MoneyConversionError(f"bad Meta minor amount {minor!r}") from exc
    if value != value.to_integral_value():
        raise MoneyConversionError(f"Meta minor amount must be integral, got {minor!r}")
    return quantize_major(value / meta_offset(currency))


def major_to_meta_minor(amount: Decimal, currency: str) -> int:
    _require_decimal(amount)
    minor = amount * meta_offset(currency)
    if minor != minor.to_integral_value():
        raise MoneyConversionError(f"{amount} {currency} is not a whole number of minor units")
    return int(minor)


def meta_spend_to_major(spend: str) -> Decimal:
    """Meta insights ``spend`` (decimal string, already major units)."""
    if not isinstance(spend, str):
        raise MoneyConversionError("Meta insights spend must be the raw string from the API")
    try:
        return quantize_major(Decimal(spend))
    except InvalidOperation as exc:
        raise MoneyConversionError(f"bad Meta spend {spend!r}") from exc


def _require_decimal(amount: Decimal) -> None:
    if not isinstance(amount, Decimal):
        raise MoneyConversionError(f"amount must be Decimal, got {type(amount).__name__}")
