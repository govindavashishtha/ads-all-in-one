"""Executor write path on Postgres with fake platforms (plan §15, §22 "Crash safety",
"Idempotency"; DECISIONS D-27..D-31). No network, no credentials."""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from engine.config import Settings
from engine.connectors.base import EntityStatus
from engine.connectors.fakes_write import SimulatedCrash
from engine.enums import Access, Action, ChangeOrigin, EntityType, Platform
from engine.executor.apply import Outcome, perform
from engine.executor.execute import execute_approved, execute_proposal
from engine.executor.plan import OpKind, WriteOp
from engine.executor.reconcile import reconcile
from engine.executor.rollback import rollback_change
from tests.integration.exec_world import World, build_world, wipe

pytestmark = pytest.mark.integration

CID = "client_exec_it"


@pytest.fixture
def world(migrated_engine: Engine, make_settings: Callable[..., Settings]) -> Iterator[World]:
    w = build_world(migrated_engine, make_settings(client_id=CID))
    yield w
    wipe(migrated_engine, CID)


def applied_calls(w: World, platform: Platform = Platform.META) -> list[dict]:  # type: ignore[type-arg]
    return [c for c in w.fakes[platform].calls if not c["validate_only"]]


# ----------------------------------------------------------------------------- happy paths


def test_pause_ad_write_ahead_readback_history_evaluations(world: World) -> None:
    pid = world.proposal("AD1", Action.PAUSE)
    res = execute_approved(world.env())
    assert res["counts"] == {"executed": 1}
    assert world.meta.state(EntityType.AD, "ad1").status is EntityStatus.PAUSED
    [row] = world.q(
        "SELECT status, before, after, idempotency_key, origin, proposal_id, "
        "applied_at, rollback_payload FROM change_log WHERE client_id=:c"
    )
    assert row.status == "applied" and row.origin == "proposal" and row.proposal_id == pid
    assert row.before["status"] == "active" and row.after["status"] == "paused"
    assert row.idempotency_key.startswith("proposal:")
    assert row.rollback_payload == {"kind": "set_status", "status": "active"}
    assert world.one("SELECT status FROM proposals WHERE id=:p", p=pid) == "executed"
    assert world.one("SELECT status FROM ads WHERE id=:a", a=world.ids["AD1"]) == "paused"
    assert (
        world.one(
            "SELECT source FROM entity_history WHERE entity_type='ad' AND entity_id=:a",
            a=world.ids["AD1"],
        )
        == "executor"
    )
    evals = world.q(
        "SELECT horizon_days, due_at, unit_entity_type, unit_entity_id "
        "FROM evaluations WHERE client_id=:c ORDER BY horizon_days"
    )
    assert [(e.horizon_days, e.unit_entity_type, e.unit_entity_id) for e in evals] == [
        (3, "ad_group", world.ids["AS1"]),
        (7, "ad_group", world.ids["AS1"]),
    ]
    assert evals[1].due_at - evals[0].due_at == dt.timedelta(days=4 + 2)  # 7+settle_days(2)-3
    ops = [(c["op"], c["validate_only"]) for c in world.meta.calls]
    assert ops == [("set_status", True), ("set_status", False)]  # validate-only first


def test_budget_change_writes_the_guardrail_clamped_budget(world: World) -> None:
    pid = world.proposal("C1", Action.BUDGET_CHANGE, params={"change_pct": -15})
    execute_proposal(world.env(), pid)
    assert world.meta.state(EntityType.CAMPAIGN, "c1").daily_budget == Decimal("850.00")
    assert (
        world.one("SELECT guardrail_result->>'new_daily_budget' FROM proposals WHERE id=:p", p=pid)
        == "850.00"
    )
    assert world.one(
        "SELECT daily_budget FROM campaigns WHERE id=:i", i=world.ids["C1"]
    ) == Decimal("850.00")
    # evaluation unit for a campaign budget change = the campaign (§16.1)
    assert (
        world.one("SELECT unit_entity_type FROM evaluations WHERE client_id=:c LIMIT 1")
        == "campaign"
    )


def test_google_negative_keyword_and_rollback_removes_it(world: World) -> None:
    pid = world.proposal(
        "ST1",
        Action.ADD_NEGATIVE,
        params={"match_type": "exact", "level": "ad_group", "term": "free stuff"},
        risk="medium",
    )
    r = execute_proposal(world.env(), pid)
    assert r.status == "executed", r
    [crit] = list(world.google.negatives)
    assert world.google.negatives[crit].text == "free stuff"
    change_id = world.one("SELECT id FROM change_log WHERE client_id=:c")
    assert (
        world.one(
            "SELECT rollback_payload->>'criterion_external_id' FROM change_log WHERE id=:i",
            i=change_id,
        )
        == crit
    )
    rb = rollback_change(world.env(), change_id, requested_by="test")
    assert rb.ok, rb
    assert world.google.negatives[crit].status is EntityStatus.REMOVED


def test_meta_new_ad_created_paused(world: World) -> None:
    params = {
        "drafts": [
            {
                "replaces_ad_id": world.ids["AD1"],
                "variants": [{"headline": "H1", "body": "B1"}, {"headline": "H2", "body": "B2"}],
            }
        ]
    }
    pid = world.proposal("AS1", Action.NEW_AD, params=params, risk="high", rule_key=None)
    r = execute_proposal(world.env(), pid)
    assert r.status == "executed", r
    created = [c for c in applied_calls(world) if c["op"] == "create_ad"]
    assert len(created) == 2  # one ad per variant (D-31)
    new_ads = [
        k
        for k in world.meta.entities
        if k[0] is EntityType.AD and k[1] not in ("ad1", "ad2", "ad3")
    ]
    assert all(world.meta.entities[k].status is EntityStatus.PAUSED for k in new_ads)
    assert world.one("SELECT count(*) FROM evaluations WHERE client_id=:c") == 2  # one change


# ----------------------------------------------------------------------------- gates


def test_execution_disabled_never_writes(world: World) -> None:
    world.set_client(execution_enabled=False)
    pid = world.proposal("AD1", Action.PAUSE)
    res = execute_approved(world.env())
    assert res["counts"] == {"skipped": 1}
    assert "execution disabled" in res["skipped_reasons"][0]
    assert world.one("SELECT status FROM proposals WHERE id=:p", p=pid) == "approved"
    assert (
        world.meta.calls == []
        and world.one("SELECT count(*) FROM change_log WHERE client_id=:c") == 0
    )


@pytest.mark.parametrize("mode", ["read_only", "auto"])
def test_read_only_mode_and_mode_above_ceiling_never_write(world: World, mode: str) -> None:
    world.set_client(mode=mode)  # HARD_MAX_MODE defaults to suggest
    pid = world.proposal("AD1", Action.PAUSE)
    r = execute_proposal(world.env(), pid)
    assert r.status == "skipped" and world.meta.calls == []
    assert world.one("SELECT status FROM proposals WHERE id=:p", p=pid) == "approved"


def test_read_only_credential_is_rejected(world: World) -> None:
    world.clients.access = Access.READ
    pid = world.proposal("AD1", Action.PAUSE)
    r = execute_proposal(world.env(), pid)
    assert r.status == "skipped" and "refused a read credential" in (r.detail or ""), r
    assert world.meta.calls == []
    assert world.one("SELECT status FROM proposals WHERE id=:p", p=pid) == "approved"
    assert (
        world.one("SELECT kind FROM alerts WHERE client_id=:c AND dedupe_key='write_unavailable'")
        == "credential_invalid"
    )


def test_guardrail_recheck_failure_blocks_and_notifies(world: World) -> None:
    world.ex("UPDATE campaigns SET managed=false WHERE id=:i", i=world.ids["C1"])
    pid = world.proposal("AD1", Action.PAUSE)
    r = execute_proposal(world.env(), pid)
    assert r.status == "blocked" and r.detail == "not_managed"
    assert world.meta.calls == []
    assert (
        world.one("SELECT kind FROM notifications WHERE proposal_id=:p", p=pid)
        == "approval_blocked"
    )


def test_kill_switch_blocks_spend_increasing_writes(world: World) -> None:
    world.ex("UPDATE ads SET status='paused' WHERE id=:i", i=world.ids["AD1"])
    world.meta.entities[(EntityType.AD, "ad1")].status = EntityStatus.PAUSED
    world.set_client(kill_switch=True)
    pid = world.proposal("AD1", Action.ENABLE)
    r = execute_proposal(world.env(), pid)
    assert r.status == "skipped" and r.detail == "kill switch on"
    assert world.meta.calls == []


# ----------------------------------------------------------------------------- idempotency


def test_duplicate_execution_writes_once(world: World) -> None:
    pid = world.proposal("C1", Action.BUDGET_CHANGE, params={"change_pct": -15})
    barrier = threading.Barrier(3)
    results: list[str] = []

    def run() -> None:
        barrier.wait()
        results.append(execute_proposal(world.env(), pid).status)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count("executed") == 1, results
    assert len([c for c in applied_calls(world) if c["op"] == "set_daily_budget"]) == 1
    assert world.one("SELECT count(*) FROM change_log WHERE client_id=:c") == 1
    # the same write key again (e.g. a proposal flipped back by hand): never re-sent
    op = WriteOp(
        OpKind.SET_STATUS,
        Action.PAUSE,
        Platform.META,
        EntityType.CAMPAIGN,
        "c2",
        EntityType.CAMPAIGN,
        world.ids["C2"],
        {"status": "paused"},
        {"status": "paused"},
    )
    first = perform(world.env(), op, origin=ChangeOrigin.PROPOSAL, key="k1", applied_by="t")
    again = perform(world.env(), op, origin=ChangeOrigin.PROPOSAL, key="k1", applied_by="t")
    assert first.outcome is Outcome.APPLIED and again.outcome is Outcome.DUPLICATE
    assert len([c for c in applied_calls(world) if c["op"] == "set_status"]) == 1


# ----------------------------------------------------------------------------- crash safety


def test_crash_between_intent_and_apply_reconciles_to_failed(world: World) -> None:
    world.meta.faults.crash_before_apply.add("set_status")
    pid = world.proposal("AD1", Action.PAUSE)
    with pytest.raises(SimulatedCrash):
        execute_proposal(world.env(), pid)
    assert world.one("SELECT status FROM change_log WHERE client_id=:c") == "applying"
    assert world.one("SELECT status FROM proposals WHERE id=:p", p=pid) == "executing"
    assert reconcile(world.env()) == {}  # younger than 5 minutes: left alone
    world.clock.advance(minutes=6)
    world.meta.faults.crash_before_apply.clear()
    counts = reconcile(world.env())
    assert counts["failed"] == 1
    assert world.one("SELECT status FROM change_log WHERE client_id=:c") == "failed"
    assert world.one("SELECT status FROM proposals WHERE id=:p", p=pid) == "failed"
    assert world.meta.state(EntityType.AD, "ad1").status is EntityStatus.ACTIVE
    assert world.one("SELECT count(*) FROM evaluations WHERE client_id=:c") == 0


def test_crash_between_apply_and_readback_reconciles_to_applied(world: World) -> None:
    world.meta.faults.crash_after_apply.add("set_status")
    pid = world.proposal("AD1", Action.PAUSE)
    with pytest.raises(SimulatedCrash):
        execute_proposal(world.env(), pid)
    world.clock.advance(minutes=6)
    counts = reconcile(world.env())
    assert counts["applied"] == 1 and counts["proposal_executed"] == 1
    assert world.one("SELECT status FROM change_log WHERE client_id=:c") == "applied"
    assert world.one("SELECT status FROM proposals WHERE id=:p", p=pid) == "executed"
    assert world.one("SELECT count(*) FROM evaluations WHERE client_id=:c") == 2
    assert (
        world.one(
            "SELECT source FROM entity_history WHERE entity_type='ad' AND entity_id=:a",
            a=world.ids["AD1"],
        )
        == "executor"
    )
    assert len(applied_calls(world)) == 1  # never re-sent


def test_ambiguous_platform_answer_waits_for_reconciler(world: World) -> None:
    world.meta.faults.unknown_after_apply.add("set_daily_budget")
    pid = world.proposal("C1", Action.BUDGET_CHANGE, params={"change_pct": -15})
    r = execute_proposal(world.env(), pid)
    assert r.status == "executing" and r.changes[-1].outcome is Outcome.UNKNOWN
    world.clock.advance(minutes=6)
    reconcile(world.env())
    assert world.one("SELECT status FROM proposals WHERE id=:p", p=pid) == "executed"


def test_crash_after_executing_before_first_write_is_failed(world: World) -> None:
    world.meta.faults.crash_before_apply.add("set_status")
    pid = world.proposal("AD1", Action.PAUSE)
    with pytest.raises(SimulatedCrash):
        execute_proposal(world.env(), pid)
    world.ex("DELETE FROM change_log WHERE client_id=:c")  # as if it died before the insert
    world.clock.advance(minutes=6)
    counts = reconcile(world.env())
    assert counts == {"proposal_failed": 1}


# ----------------------------------------------------------------------------- failures


def test_verify_mismatch_is_verify_failed_and_critical(world: World) -> None:
    world.meta.faults.budget_applied_as = Decimal("900")
    pid = world.proposal("C1", Action.BUDGET_CHANGE, params={"change_pct": -15})
    execute_proposal(world.env(), pid)
    assert world.one("SELECT status FROM change_log WHERE client_id=:c") == "verify_failed"
    assert world.one("SELECT status FROM proposals WHERE id=:p", p=pid) == "failed"
    assert (
        world.one("SELECT severity FROM alerts WHERE client_id=:c AND kind='verify_failed'")
        == "critical"
    )


def test_validate_only_rejection_never_applies(world: World) -> None:
    world.meta.faults.reject_validate.add("set_status")
    pid = world.proposal("AD1", Action.PAUSE)
    execute_proposal(world.env(), pid)
    assert applied_calls(world) == []
    assert world.one("SELECT status FROM change_log WHERE client_id=:c") == "failed"
    assert world.one("SELECT status FROM proposals WHERE id=:p", p=pid) == "failed"
    assert world.one("SELECT count(*) FROM alerts WHERE client_id=:c AND kind='write_failed'") == 1


# ----------------------------------------------------------------------------- rollback


def test_rollback_round_trip(world: World) -> None:
    pid = world.proposal("C1", Action.BUDGET_CHANGE, params={"change_pct": -15})
    execute_proposal(world.env(), pid)
    change_id = world.one("SELECT id FROM change_log WHERE client_id=:c")
    rb = rollback_change(world.env(), change_id, requested_by="test")
    assert rb.ok and rb.change_id is not None, rb
    assert world.meta.state(EntityType.CAMPAIGN, "c1").daily_budget == Decimal("1000.00")
    rows = {
        r.id: r
        for r in world.q(
            "SELECT id, status, origin, rollback_of FROM change_log WHERE client_id=:c"
        )
    }
    assert rows[change_id].status == "rolled_back"
    assert rows[rb.change_id].origin == "rollback" and rows[rb.change_id].rollback_of == (change_id)
    assert (
        world.one("SELECT count(*) FROM evaluations WHERE client_id=:c AND done_at IS NULL") == 0
    )  # evaluations of a rolled-back change closed
    assert (
        world.one("SELECT kind FROM alerts WHERE client_id=:c AND kind='rollback_executed'")
        == "rollback_executed"
    )
    again = rollback_change(world.env(), change_id, requested_by="test")
    assert again.status == "skipped"  # already rolled back


def test_rollback_with_state_drift_needs_approval(world: World) -> None:
    pid = world.proposal("AD1", Action.PAUSE)
    execute_proposal(world.env(), pid)
    change_id = world.one("SELECT id FROM change_log WHERE client_id=:c")
    world.meta.entities[(EntityType.AD, "ad1")].status = EntityStatus.ACTIVE  # a human acted
    rb = rollback_change(world.env(), change_id, requested_by="test")
    assert rb.status == "drift_proposal" and rb.drift_proposal_id
    drift = world.q(
        "SELECT action, status, risk, params FROM proposals WHERE id=:p", p=rb.drift_proposal_id
    )[0]
    assert (drift.action, drift.status, drift.risk) == (
        "rollback_with_state_drift",
        "awaiting_approval",
        "high",
    )
    assert len(applied_calls(world)) == 1  # nothing written by the drifted rollback
    world.ex("UPDATE proposals SET status='approved' WHERE id=:p", p=rb.drift_proposal_id)
    r = execute_proposal(world.env(), rb.drift_proposal_id)
    assert r.status == "executed", r
    assert world.one("SELECT status FROM change_log WHERE id=:i", i=change_id) == "rolled_back"


def test_increasing_rollback_respects_caps(world: World) -> None:
    pid = world.proposal("C1", Action.BUDGET_CHANGE, params={"change_pct": -15})
    execute_proposal(world.env(), pid)
    change_id = world.one("SELECT id FROM change_log WHERE client_id=:c")
    # caps.meta.daily = 12000 in the seed; inflate another active unit so +150 breaches
    world.ex("UPDATE campaigns SET daily_budget=11000 WHERE id=:i", i=world.ids["C3"])
    rb = rollback_change(world.env(), change_id, requested_by="test")
    assert rb.status == "blocked" and "cap_exceeded" in (rb.detail or "")
