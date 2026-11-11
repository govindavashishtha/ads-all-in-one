"""Money conversion unit tests (plan §0 rule 4, §8.1). Synthetic values; the recorded-platform
fixture round-trips live in tests/contract/test_money_fixtures.py."""

from __future__ import annotations

from decimal import Decimal

import pytest

from engine.connectors import money


@pytest.mark.parametrize(
    ("micros", "major"),
    [
        (0, "0.00"),
        (1_000_000, "1.00"),
        (500_000_000, "500.00"),
        (1_234_567, "1.23"),
        (1_235_000, "1.24"),
        (999, "0.00"),
    ],
)
def test_google_micros_to_major(micros: int, major: str) -> None:
    assert money.google_micros_to_major(micros) == Decimal(major)


@pytest.mark.parametrize("amount", ["0", "1", "499.99", "8000.00", "12345.67"])
def test_google_round_trip(amount: str) -> None:
    d = Decimal(amount)
    assert money.google_micros_to_major(money.major_to_google_micros(d)) == d


def test_google_rejects_floats_and_bools() -> None:
    with pytest.raises(money.MoneyConversionError):
        money.google_micros_to_major(1.5)  # type: ignore[arg-type]
    with pytest.raises(money.MoneyConversionError):
        money.google_micros_to_major(True)
    with pytest.raises(money.MoneyConversionError):
        money.major_to_google_micros(1.5)  # type: ignore[arg-type]


@pytest.mark.parametrize(("minor", "major"), [(50000, "500.00"), ("30000", "300.00"), (1, "0.01")])
def test_meta_minor_to_major_inr(minor: int | str, major: str) -> None:
    assert money.meta_minor_to_major(minor, "INR") == Decimal(major)


@pytest.mark.parametrize("amount", ["300", "500.50", "8000.00"])
def test_meta_round_trip(amount: str) -> None:
    d = Decimal(amount)
    assert money.meta_minor_to_major(money.major_to_meta_minor(d, "INR"), "INR") == d


def test_meta_rejects_sub_minor_and_unknown_currency() -> None:
    with pytest.raises(money.MoneyConversionError):
        money.major_to_meta_minor(Decimal("1.005"), "INR")
    with pytest.raises(money.MoneyConversionError):
        money.meta_minor_to_major(100, "XYZ")
    with pytest.raises(money.MoneyConversionError):
        money.meta_minor_to_major("12.5", "INR")


def test_meta_spend_string_is_major_units() -> None:
    assert money.meta_spend_to_major("4200.456") == Decimal("4200.46")
    with pytest.raises(money.MoneyConversionError):
        money.meta_spend_to_major(4200.45)  # type: ignore[arg-type]
