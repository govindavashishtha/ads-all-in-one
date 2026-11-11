"""Evaluator on Postgres (plan §16): D+3 safety → rollback, delivery-collapse warn, D+7
verdict → rule_stats / rollback, insufficient → D+14 → neutral. Fake platforms only."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from engine.config import Settings
from engine.enums import Action, EntityType
from engine.evaluation.evaluate import evaluate_due
from engine.executor.execute import execute_proposal
from tests.integration.exec_world import T0, World, build_world, wipe

pytestmark = pytest.mark.integration

CID = "client_eval_it"
DAY0 = dt.date(2026, 11, 11)  # change day (applied at T0 = 12:00 IST)


@pytest.fixture
def world(migrated_engine: Engine, make_settings: Callable[..., Settings]) -> Iterator[World]:
    w = build_world(migrated_engine, make_settings(client_id=CID))
    yield w
    wipe(migrated_engine, CID)


def metrics(w: World, key: str, days: range, spend: float, conv: float) -> None:
    for d in days:
        w.ex(
            "INSERT INTO metrics_daily (client_id, platform, entity_type, entity_id, date, "
            "spend, conversions, conv_value) VALUES (:c, 'meta', 'campaign', :i, :d, :s, :v, 0)",
            i=w.ids[key],
            d=DAY0 + dt.timedelta(days=d),
            s=spend,
            v=conv,
        )


def budget_change(w: World, pct: int) -> int:
    pid = w.proposal("C1", Action.BUDGET_CHANGE, params={"change_pct": pct})
    assert execute_proposal(w.env(), pid).status == "executed"
    return int(w.one("SELECT id FROM change_log WHERE client_id=:c AND proposal_id=:p", p=pid))


def evals(w: World) -> dict[int, object]:
    return {
        r.horizon_days: r
        for r in w.q(
            "SELECT horizon_days, verdict, action_taken, done_at, p_worse, p_better FROM "
            "evaluations WHERE client_id=:c"
        )
    }


def at(w: World, day_offset: int) -> None:
    w.clock.t = T0 + dt.timedelta(days=day_offset)


def test_d3_worse_rolls_back(world: World) -> None:
    change_id = budget_change(world, -15)
    metrics(world, "C1", range(1, 4), 600, 0)  # 1800 ≥ 3 × 500 with 0 conversions
    at(world, 3)  # due, but post day 3 (11-14) is today: not complete yet
    assert evaluate_due(world.env()) == {"not_ready": 1}
    at(world, 4)
    assert evaluate_due(world.env()) == {"worse": 1}
    e = evals(world)[3]
    assert e.verdict == "worse" and "rollback of #" in e.action_taken  # type: ignore[attr-defined]
    assert world.meta.state(EntityType.CAMPAIGN, "c1").daily_budget == Decimal("1000.00")
    assert world.one("SELECT status FROM change_log WHERE id=:i", i=change_id) == "rolled_back"
    assert evals(world)[7].action_taken.startswith("cancelled")  # type: ignore[attr-defined]
    assert world.one("SELECT count(*) FROM rule_stats WHERE client_id=:c") == 0  # D+3 not counted


def test_d3_delivery_collapse_warns_without_rollback(world: World) -> None:
    budget_change(world, 15)
    metrics(world, "C1", range(-7, 0), 1000, 3)
    metrics(world, "C1", range(1, 4), 100, 1)
    at(world, 4)
    assert evaluate_due(world.env()) == {"none": 1}
    assert evals(world)[3].action_taken == "warn: delivery collapsed"  # type: ignore[attr-defined]
    assert (
        world.one("SELECT count(*) FROM alerts WHERE client_id=:c AND kind='delivery_collapsed'")
        == 1
    )
    assert world.meta.state(EntityType.CAMPAIGN, "c1").daily_budget == Decimal("1150.00")


def test_d7_better_feeds_rule_stats(world: World) -> None:
    budget_change(world, -15)
    metrics(world, "C1", range(-7, 0), 1000, 1)
    metrics(world, "C1", range(1, 8), 1000, 4)
    at(world, 9)  # due at applied + 7 + settle_days(2); post ends 11-18 < 11-20
    assert evaluate_due(world.env()) == {"better": 1, "none": 1}
    e7 = evals(world)[7]
    assert e7.verdict == "better" and e7.p_better > 0.7  # type: ignore[attr-defined]
    row = world.q(
        "SELECT n_changes, n_better FROM rule_stats WHERE client_id=:c AND rule_key='test_rule'"
    )[0]
    assert (row.n_changes, row.n_better) == (1, 1)
    assert evaluate_due(world.env()) == {}  # done_at set: idempotent


def test_d7_worse_rolls_back_and_counts(world: World) -> None:
    change_id = budget_change(world, -15)
    metrics(world, "C1", range(-7, 0), 1000, 4)
    metrics(world, "C1", range(1, 8), 1000, 1)
    at(world, 9)
    evaluate_due(world.env())
    assert evals(world)[7].verdict == "worse"  # type: ignore[attr-defined]
    assert world.one("SELECT status FROM change_log WHERE id=:i", i=change_id) == "rolled_back"
    assert world.one("SELECT n_worse FROM rule_stats WHERE client_id=:c") == 1


def test_insufficient_data_schedules_d14_then_neutral(world: World) -> None:
    budget_change(world, -15)
    metrics(world, "C1", range(-7, 0), 1000, 2)
    metrics(world, "C1", range(1, 15), 50, 0)  # far below 2 × target CPA even over 14 days
    at(world, 9)
    evaluate_due(world.env())
    e = evals(world)
    assert e[7].verdict == "insufficient_data" and 14 in e  # type: ignore[attr-defined]
    assert world.one("SELECT count(*) FROM rule_stats WHERE client_id=:c") == 0
    at(world, 17)  # D+14 due at applied + 14 + 2
    evaluate_due(world.env())
    assert evals(world)[14].verdict == "neutral"  # type: ignore[attr-defined]
    assert world.one("SELECT n_neutral FROM rule_stats WHERE client_id=:c") == 1


def test_rolled_back_change_cancels_evaluation(world: World) -> None:
    change_id = budget_change(world, -15)
    world.ex("UPDATE change_log SET status='rolled_back' WHERE id=:i", i=change_id)
    at(world, 4)
    assert evaluate_due(world.env()) == {"cancelled": 1}
