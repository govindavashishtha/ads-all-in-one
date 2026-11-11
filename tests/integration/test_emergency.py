"""Emergency stop, pause-all, restore and the write_requests queue (plan §12.4 steps 5–6,
§17; DECISIONS D-28, D-29) on Postgres with fake platforms."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text

from engine.config import Settings
from engine.connectors.base import EntityStatus
from engine.connectors.factory import ConnectorFactory
from engine.enums import Action, EntityType, Mode, Platform
from engine.executor.clients import RealWriteClients, WriteUnavailableError
from engine.executor.execute import execute_proposal
from engine.executor.requests import process_write_requests
from engine.jobs import pacing
from engine.jobs.pacing import EmergencyCondition
from engine.pacing.emergency import auto_restore, emergency_handler, install
from tests.integration.exec_world import World, build_world, wipe

pytestmark = pytest.mark.integration

CID = "client_emerg_it"


@pytest.fixture
def world(migrated_engine: Engine, make_settings: Callable[..., Settings]) -> Iterator[World]:
    w = build_world(migrated_engine, make_settings(client_id=CID))
    yield w
    wipe(migrated_engine, CID)


DAILY_META = EmergencyCondition(Platform.META, "daily_cap", Decimal("11700"), Decimal("12000"))


def meta_status(w: World, ext: str) -> EntityStatus:
    return w.meta.state(EntityType.CAMPAIGN, ext).status


def test_emergency_stop_pauses_managed_active_campaigns_only(world: World) -> None:
    [res] = emergency_handler(world.ctx(), None, [DAILY_META], Mode.SUGGEST)  # type: ignore[arg-type]
    assert sorted(res.paused) == sorted([world.ids["C1"], world.ids["C2"]])
    assert meta_status(world, "c1") is EntityStatus.PAUSED
    assert meta_status(world, "c3") is EntityStatus.ACTIVE  # unmanaged: never touched
    assert world.google.state(EntityType.CAMPAIGN, "g1").status is EntityStatus.ACTIVE
    assert all(not c["validate_only"] for c in world.meta.calls)  # no dry-run on this path
    ea = world.q(
        "SELECT trigger, campaign_ids, restore_policy FROM emergency_actions WHERE client_id=:c"
    )[0]
    assert ea.trigger == "daily_cap" and ea.restore_policy == "auto_next_day"
    assert sorted(ea.campaign_ids) == sorted([world.ids["C1"], world.ids["C2"]])
    rows = world.q("SELECT origin, status FROM change_log WHERE client_id=:c")
    assert {(r.origin, r.status) for r in rows} == {("emergency", "applied")}
    assert (
        world.one("SELECT count(*) FROM entity_history WHERE client_id=:c AND source='emergency'")
        == 2
    )
    msg = world.one("SELECT message FROM alerts WHERE client_id=:c AND kind='emergency_stop'")
    assert "unmanaged campaigns still active" in msg and "C3" in msg
    # the next pacing run: nothing left to pause, no second emergency_actions row
    emergency_handler(world.ctx(), None, [DAILY_META], Mode.SUGGEST)  # type: ignore[arg-type]
    assert world.one("SELECT count(*) FROM emergency_actions WHERE client_id=:c") == 1


def test_total_breach_pauses_both_platforms_monthly_is_manual(world: World) -> None:
    cond = EmergencyCondition(None, "monthly_cap", Decimal("540000"), Decimal("550000"))
    res = emergency_handler(world.ctx(), None, [cond], Mode.SUGGEST)  # type: ignore[arg-type]
    assert {r.platform for r in res} == {Platform.META, Platform.GOOGLE}
    assert world.google.state(EntityType.CAMPAIGN, "g1").status is EntityStatus.PAUSED
    assert {
        r.restore_policy
        for r in world.q("SELECT restore_policy FROM emergency_actions WHERE client_id=:c")
    } == {"manual"}


def test_emergency_stop_writes_nothing_when_execution_disabled(world: World) -> None:
    world.set_client(execution_enabled=False)
    [res] = emergency_handler(world.ctx(), None, [DAILY_META], Mode.SUGGEST)  # type: ignore[arg-type]
    assert res.blocked and "execution disabled" in res.blocked
    assert world.meta.calls == [] and meta_status(world, "c1") is EntityStatus.ACTIVE
    msg = world.one("SELECT message FROM alerts WHERE client_id=:c AND kind='emergency_stop'")
    assert "NOTHING was paused" in msg


def test_read_only_mode_is_alert_only(world: World) -> None:
    assert emergency_handler(world.ctx(), None, [DAILY_META], Mode.READ_ONLY) == []  # type: ignore[arg-type]
    assert world.meta.calls == []


def test_emergency_pause_passes_kill_switch_but_blocks_other_writes(world: World) -> None:
    """Kill switch on: the privileged spend-reducing emergency pause still runs; every
    spend-increasing write (approved enable, restore) is blocked."""
    world.set_client(kill_switch=True)
    [res] = emergency_handler(world.ctx(), None, [DAILY_META], Mode.SUGGEST)  # type: ignore[arg-type]
    assert len(res.paused) == 2
    n_calls = len(world.meta.calls)
    pid = world.proposal("C1", Action.ENABLE)
    assert execute_proposal(world.env(), pid).status == "skipped"
    world.clock.advance(days=1)
    [restore] = auto_restore(world.env())
    assert restore["status"] == "blocked" and restore["detail"] == "kill switch on"
    assert len(world.meta.calls) == n_calls  # nothing written after the pause


def test_auto_restore_next_day_skips_touched_campaigns(world: World) -> None:
    emergency_handler(world.ctx(), None, [DAILY_META], Mode.SUGGEST)  # type: ignore[arg-type]
    assert auto_restore(world.env()) == []  # same account day: not yet
    # someone changed C2 since: a newer non-emergency history row
    world.ex(
        "INSERT INTO entity_history (client_id, entity_type, entity_id, observed_at, "
        "status, daily_budget, learning, source) VALUES (:c, 'campaign', :i, :t, "
        "'paused', 1200, false, 'hourly_pull')",
        i=world.ids["C2"],
        t=world.clock.t + dt.timedelta(minutes=30),
    )
    world.clock.advance(days=1)
    [res] = auto_restore(world.env())
    assert res["status"] == "restored" and res["restored"] == [world.ids["C1"]]
    assert str(world.ids["C2"]) in res["skipped"]
    assert meta_status(world, "c1") is EntityStatus.ACTIVE
    assert meta_status(world, "c2") is EntityStatus.PAUSED
    assert world.one("SELECT restored_by FROM emergency_actions WHERE client_id=:c") == (
        "emergency_restore"
    )
    assert (
        world.one(
            "SELECT count(*) FROM change_log WHERE client_id=:c AND "
            "origin='restore' AND status='applied'"
        )
        == 1
    )
    assert auto_restore(world.env()) == []  # restored_at set: done


def test_pause_all_and_manual_restore_through_write_requests(world: World) -> None:
    world.ex(
        "INSERT INTO write_requests (client_id, kind, dedupe_key, requested_by) "
        "VALUES (:c, 'pause_all', 'pause_all', 'admin@test')"
    )
    assert process_write_requests(world.env()) == 1
    req = world.q("SELECT status, result FROM write_requests WHERE client_id=:c")[0]
    assert req.status == "success" and req.result["status"] == "done"
    assert meta_status(world, "c1") is EntityStatus.PAUSED
    assert world.google.state(EntityType.CAMPAIGN, "g1").status is EntityStatus.PAUSED
    actions = world.q(
        "SELECT id, trigger, restore_policy FROM emergency_actions WHERE client_id=:c ORDER BY id"
    )
    assert {(a.trigger, a.restore_policy) for a in actions} == {("pause_all", "manual")}
    world.clock.advance(days=1)
    assert auto_restore(world.env()) == []  # manual rows never auto-restore
    meta_action = next(
        a.id
        for a in actions
        if world.one("SELECT platform FROM emergency_actions WHERE id=:i", i=a.id) == "meta"
    )
    world.ex(
        "INSERT INTO write_requests (client_id, kind, dedupe_key, target_id, "
        "requested_by) VALUES (:c, 'restore', 'restore:x', :t, 'approver@test')",
        t=meta_action,
    )
    process_write_requests(world.env())
    assert meta_status(world, "c1") is EntityStatus.ACTIVE
    assert world.google.state(EntityType.CAMPAIGN, "g1").status is EntityStatus.PAUSED


def test_rollback_write_request_and_blocked_request_is_failed(world: World) -> None:
    pid = world.proposal("C1", Action.BUDGET_CHANGE, params={"change_pct": -15})
    execute_proposal(world.env(), pid)
    change_id = world.one("SELECT id FROM change_log WHERE client_id=:c")
    world.set_client(execution_enabled=False)
    world.ex(
        "INSERT INTO write_requests (client_id, kind, dedupe_key, target_id, "
        "requested_by) VALUES (:c, 'rollback', 'rollback:1', :t, 'a@test')",
        t=change_id,
    )
    process_write_requests(world.env())
    req = world.q("SELECT status, error FROM write_requests WHERE client_id=:c")[0]
    assert req.status == "failed" and "execution disabled" in req.error
    world.set_client(execution_enabled=True)
    world.ex(
        "INSERT INTO write_requests (client_id, kind, dedupe_key, target_id, "
        "requested_by) VALUES (:c, 'rollback', 'rollback:2', :t, 'a@test')",
        t=change_id,
    )
    process_write_requests(world.env())
    assert world.one("SELECT status FROM change_log WHERE id=:i", i=change_id) == "rolled_back"


def test_pacing_hook_is_installed_and_alert_text(world: World) -> None:
    pacing.EMERGENCY_HANDLER = None
    install()
    assert pacing.EMERGENCY_HANDLER is emergency_handler
    pacing.EMERGENCY_HANDLER = None


def test_real_provider_without_write_credentials_is_unavailable(world: World) -> None:
    provider = RealWriteClients(world.settings, ConnectorFactory(world.settings))
    with pytest.raises(WriteUnavailableError):
        provider.get(Platform.META, conversion_action=None)


def test_handler_inside_open_pacing_transaction_does_not_block(world: World) -> None:
    """The hook is called inside the pacing step's transaction (alerts + status written but
    not committed); the handler's own committed transactions must not wait on it."""
    from engine.enums import Severity
    from engine.ingest.alerts import raise_alert
    from engine.system_status import pacing_key, put_status

    with world.sessions() as s, s.begin():
        s.execute(text("SET LOCAL lock_timeout = '5s'"))
        raise_alert(
            s,
            CID,
            dedupe_key="emergency:meta:daily_cap:2026-11-11",
            severity=Severity.CRITICAL,
            kind="emergency_stop",
            message="pacing",
        )
        put_status(s, CID, pacing_key("meta"), {"x": 1}, world.clock.t)
        [res] = emergency_handler(world.ctx(), s, [DAILY_META], Mode.SUGGEST)
    assert len(res.paused) == 2
