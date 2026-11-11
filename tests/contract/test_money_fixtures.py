"""Money round-trip against recorded platform values (plan §8.1, §23 Phase 0 acceptance)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from engine.connectors import money

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "money"


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def _check_meta(f: dict[str, Any]) -> None:
    cur = f["currency"]
    budget = money.meta_minor_to_major(f["budget"]["raw_daily_budget"], cur)
    assert budget == Decimal(f["budget"]["ui_major"])
    assert str(money.major_to_meta_minor(budget, cur)) == str(f["budget"]["raw_daily_budget"])
    assert money.meta_spend_to_major(f["spend"]["raw_spend"]) == Decimal(f["spend"]["ui_major"])


def _check_google(f: dict[str, Any]) -> None:
    budget = money.google_micros_to_major(f["budget"]["raw_amount_micros"])
    assert budget == Decimal(f["budget"]["ui_major"])
    assert money.major_to_google_micros(budget) == f["budget"]["raw_amount_micros"]
    assert money.google_micros_to_major(f["spend"]["raw_cost_micros"]) == Decimal(
        f["spend"]["ui_major"]
    )


CHECKS = {"meta": _check_meta, "google": _check_google}


@pytest.mark.contract
@pytest.mark.parametrize("platform", ["meta", "google"])
def test_example_fixture_shape_round_trips(platform: str) -> None:
    CHECKS[platform](_load(f"{platform}.example.json"))


@pytest.mark.contract
@pytest.mark.parametrize("platform", ["meta", "google"])
def test_recorded_fixture_round_trips(platform: str) -> None:
    path = FIXTURES / f"{platform}.json"
    if not path.exists():
        pytest.skip(f"Phase 0 open item: record tests/fixtures/money/{platform}.json (VERIFY-P0)")
    fixture = _load(path.name)
    assert fixture.get("synthetic") is False, "recorded fixture must have synthetic=false"
    CHECKS[platform](fixture)
