"""Every §17 endpoint: happy path, not-enabled behaviour, and authz."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from engine.config import Settings
from engine.db.models import (
    Approval,
    AuditLog,
    Campaign,
    Client,
    ConfigVersion,
    JobRequest,
    Proposal,
    SystemStatus,
)
from tests.api.conftest import BASE, CID, NOW, PASSWORD, Clock, World, bearer, build_app

pytestmark = pytest.mark.integration

V1 = "/api/v1"
H = dict[str, str]


def _audit(db: Session, action: str) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


# --------------------------------------------------------------------------- authz matrix

PUBLIC = {("GET", f"{V1}/health"), ("POST", f"{V1}/auth/login"), ("POST", f"{V1}/auth/token")}
VIEWER_WRITES = {("POST", f"{V1}/auth/logout")}


def _routes(client: TestClient) -> list[tuple[str, str]]:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    return [(m.upper(), p) for p, ops in sorted(paths.items()) for m in sorted(ops)]


def _fill(path: str) -> str:
    import re

    return re.sub(r"\{[^}]+\}", "1", path)


def test_every_route_requires_auth(client: TestClient) -> None:
    routes = _routes(client)
    assert len(routes) >= 40
    for method, path in routes:
        if (method, path) in PUBLIC:
            continue
        r = client.request(method, _fill(path))
        assert r.status_code == 401, (method, path, r.status_code)


def test_viewer_cannot_write_anything(client: TestClient, viewer_h: H) -> None:
    for method, path in _routes(client):
        if method == "GET" or (method, path) in PUBLIC | VIEWER_WRITES:
            continue
        r = client.request(method, _fill(path), headers=viewer_h, json={})
        assert r.status_code == 403, (method, path, r.status_code)


def test_viewer_can_read_everything(client: TestClient, viewer_h: H, world: World) -> None:
    for method, path in _routes(client):
        if method != "GET" or path.endswith("/sessions"):
            continue
        url = _fill(path)
        params = (
            {"entity_type": "campaign", "entity_id": world.meta_campaign}
            if path.endswith("/metrics")
            else {}
        )
        r = client.get(url, headers=viewer_h, params=params)
        assert r.status_code in (200, 404, 501), (path, r.status_code, r.text)


# --------------------------------------------------------------------------- overview


def test_overview(client: TestClient, admin_h: H, world: World) -> None:
    d = client.get(f"{V1}/overview", headers=admin_h).json()
    assert d["mode"] == "read_only" and d["hard_max_mode"] == "suggest"
    assert d["kill_switch"] == {"effective": False, "db": False, "env_override": False}
    meta, google = d["platforms"]
    assert meta["platform"] == "meta" and meta["account_date"] == "2026-11-11"
    assert meta["elapsed_day_fraction"] == 0.5
    assert meta["spend_today"] == "40.00"  # hourly wins over the partial daily row
    assert meta["mtd_spend"] == "1040.00"
    assert google["spend_today"] == "50.00"  # no hourly -> today's daily row
    assert google["mtd_spend"] == "550.00"
    assert meta["daily_cap"] == "12000" and d["total"]["daily_cap"] == "20000"
    assert meta["spent_fraction_today"] == pytest.approx(40 / 12000)
    r = meta["range"]
    assert (r["from_date"], r["to_date"]) == ("2026-11-04", "2026-11-10")
    assert (r["spend"], r["cpa"], r["roas"], r["ctr"]) == ("700.00", "100.00", 3.0, 0.05)
    assert google["range"]["cpa"] is None
    assert d["total"]["spend_today"] == "90.00" and d["total"]["mtd_spend"] == "1590.00"
    assert d["total"]["range"]["spend"] == "1050.00"
    assert d["open_alerts"] == {"info": 0, "warn": 1, "critical": 0, "total": 1}
    assert d["open_approvals"] == 3 and d["active_emergency_actions"] == 1
    assert d["last_synced_at"]["meta"] is not None
    assert d["limits_version"] == 1
    # dashboard contract additions
    assert d["timezone"] == "Asia/Kolkata" and meta["timezone"] == "Asia/Kolkata"
    assert d["targets"]["objective"] in ("conversions", "value", "traffic")
    assert d["targets"]["rules_version"] == 1
    assert d["trend_days"] == 28
    assert [t["date"] for t in d["trend"]] == [f"2026-11-{x:02d}" for x in range(1, 11)]
    last = d["trend"][-1]
    assert last["spend"] == "150.00" and last["spend_by_platform"] == {
        "meta": "100.00",
        "google": "50.00",
    }
    assert last["cpa"] == "150.00" and last["roas"] == 2.0
    pace = meta["intraday"]
    assert len(pace) == 24 and meta["curve_source"] == "linear"  # < 14 complete hourly days
    assert pace[3]["cumulative_spend"] == "40.00" and pace[12]["cumulative_spend"] == "40.00"
    assert pace[13]["spend"] is None and pace[13]["cumulative_spend"] is None  # future hour
    assert pace[11]["expected_cumulative"] == "6000.00"  # 12/24 x 12000 (linear)
    assert d["total"]["intraday"][23]["expected_cumulative"] == "20000.00"
    assert meta["projected_eod"] is None and d["total"]["projected_eod"] is None
    assert [a["kind"] for a in d["alerts"]] == ["pacing_warn"]
    assert len(d["emergency_actions"]) == 1
    assert client.get(f"{V1}/overview?range=0d", headers=admin_h).status_code == 422
    assert client.get(f"{V1}/overview?range=abc", headers=admin_h).status_code == 422


def test_overview_projected_eod_from_pacing_job(
    client: TestClient, admin_h: H, world: World, seeded: Session
) -> None:
    for platform, value in (("meta", "120.00"), ("google", "300.00")):
        seeded.add(
            SystemStatus(
                client_id=CID,
                key=f"pacing:{platform}",
                value={"local_date": "2026-11-11", "projected_eod": value},
                updated_at=NOW,
            )
        )
    seeded.flush()
    d = client.get(f"{V1}/overview", headers=admin_h).json()
    meta, google = d["platforms"]
    assert (meta["projected_eod"], google["projected_eod"]) == ("120.00", "300.00")
    assert d["total"]["projected_eod"] == "420.00" and meta["projected_eod_at"] is not None
    # a projection from another account day is not shown
    seeded.query(SystemStatus).filter(SystemStatus.key == "pacing:meta").update(
        {"value": {"local_date": "2026-11-10", "projected_eod": "999.00"}}
    )
    seeded.flush()
    d = client.get(f"{V1}/overview", headers=admin_h).json()
    assert d["platforms"][0]["projected_eod"] is None and d["total"]["projected_eod"] is None


def test_overview_without_client_is_503(
    db: Session, api_settings_factory: Callable[..., Settings], clock: Clock, seeded: Session
) -> None:
    app = build_app(seeded, api_settings_factory(client_id="other"), clock)
    with TestClient(app, base_url=BASE) as c:
        r = c.post(f"{V1}/auth/token", json={"username": "admin", "password": PASSWORD})
        assert r.status_code == 503 and "bootstrapped" in r.text


# --------------------------------------------------------------------------- entities


def test_campaigns(client: TestClient, viewer_h: H, world: World) -> None:
    rows = client.get(f"{V1}/campaigns", headers=viewer_h).json()
    assert [r["platform"] for r in rows] == ["meta", "google"]  # PG enum order
    meta = rows[0]
    assert meta["metrics"]["spend"] == "700.00" and meta["metrics"]["conversions"] == "7.0000"
    assert meta["daily_budget"] == "1000.00" and meta["managed"] is False
    assert rows[1]["metrics"]["spend"] == "0.00" and rows[1]["score"] is None
    only = client.get(f"{V1}/campaigns?platform=meta", headers=viewer_h).json()
    assert [r["id"] for r in only] == [world.meta_campaign]
    assert client.get(f"{V1}/campaigns?managed=true", headers=viewer_h).json() == []
    d = client.get(f"{V1}/campaigns/{world.meta_campaign}?range=28d", headers=viewer_h).json()
    assert d["metrics"]["spend"] == "1000.00"  # Nov 1..10 (only 10 days of data)
    assert [g["id"] for g in d["ad_groups"]] == [world.meta_adset]
    assert d["ad_groups"][0]["learning"] is None
    assert client.get(f"{V1}/campaigns/999999", headers=viewer_h).status_code == 404


def test_ad_group_ads_keywords(client: TestClient, viewer_h: H, world: World) -> None:
    g = client.get(f"{V1}/ad-groups/{world.meta_adset}", headers=viewer_h).json()
    assert [a["id"] for a in g["ads"]] == world.meta_ads
    assert g["ads"][0]["score"]["cpa_shrunk"] == "400.00"  # latest as_of, not the older 999
    assert g["ads"][0]["score"]["as_of"] == "2026-11-10"
    gg = client.get(f"{V1}/ad-groups/{world.google_adgroup}", headers=viewer_h).json()
    assert [k["text"] for k in gg["keywords"]] == ["running shoes"]
    ad = client.get(f"{V1}/ads/{world.meta_ads[1]}", headers=viewer_h).json()
    assert ad["headline"] == "h1" and ad["score"]["rank_in_peer"] == 2
    kws = client.get(f"{V1}/keywords?ad_group_id={world.google_adgroup}", headers=viewer_h).json()
    assert [k["id"] for k in kws] == [world.keyword]
    assert client.get(f"{V1}/keywords?ad_group_id=999999", headers=viewer_h).json() == []
    assert client.get(f"{V1}/ads/999999", headers=viewer_h).status_code == 404
    assert client.get(f"{V1}/ad-groups/999999", headers=viewer_h).status_code == 404


def test_set_managed(
    client: TestClient, admin_h: H, approver_h: H, world: World, seeded: Session
) -> None:
    url = f"{V1}/campaigns/{world.meta_campaign}/managed"
    assert client.put(url, headers=approver_h, json={"managed": True}).status_code == 403
    r = client.put(url, headers=admin_h, json={"managed": True, "reason": "I-3 opt-in"})
    assert r.status_code == 200 and r.json()["managed"] is True
    assert seeded.get(Campaign, world.meta_campaign).managed is True  # type: ignore[union-attr]
    [a] = _audit(seeded, "campaign.managed")
    assert a.payload == {"before": False, "after": True, "reason": "I-3 opt-in"}
    assert (
        client.put(
            f"{V1}/campaigns/999999/managed", headers=admin_h, json={"managed": True}
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- metrics / rankings


def test_metrics_series(client: TestClient, viewer_h: H, world: World) -> None:
    q = {"entity_type": "account", "entity_id": world.meta_account, "platform": "meta"}
    d = client.get(f"{V1}/metrics", headers=viewer_h, params=q | {"from": "2026-11-09"}).json()
    assert [p["date"] for p in d["points"]] == ["2026-11-09", "2026-11-10", "2026-11-11"]
    assert d["points"][0]["spend"] == "100.00" and d["platform"] == "meta"
    h = client.get(
        f"{V1}/metrics", headers=viewer_h, params=q | {"grain": "hour", "from": "2026-11-11"}
    ).json()
    assert [p["ts_hour"] for p in h["points"]][:2] == ["2026-11-11T00:00:00", "2026-11-11T01:00:00"]
    assert len(h["points"]) == 4
    bad = {"entity_type": "ad", "entity_id": 1, "grain": "hour"}
    assert client.get(f"{V1}/metrics", headers=viewer_h, params=bad).status_code == 422
    rev = q | {"from": "2026-11-10", "to": "2026-11-01"}
    assert client.get(f"{V1}/metrics", headers=viewer_h, params=rev).status_code == 422
    assert client.get(f"{V1}/metrics", headers=viewer_h).status_code == 422  # missing params


def test_rankings(client: TestClient, viewer_h: H, world: World) -> None:
    d = client.get(f"{V1}/rankings?level=ad", headers=viewer_h).json()
    assert d["as_of"] == "2026-11-10"
    assert [r["entity_id"] for r in d["rows"]] == world.meta_ads
    row = d["rows"][0]
    assert row["cpa_p10"] == "300.00" and row["name"] == "ad 0"
    assert (row["campaign_id"], row["campaign_name"]) == (world.meta_campaign, "meta-c1")
    assert (row["parent_id"], row["parent_name"]) == (world.meta_adset, "adset 1")
    assert row["spend"] is None and row["fatigue_flag"] is False  # no features row
    assert d["targets"]["objective"] is not None
    assert (
        client.get(f"{V1}/rankings?level=ad&platform=google", headers=viewer_h).json()["rows"] == []
    )
    empty = client.get(f"{V1}/rankings?level=keyword", headers=viewer_h).json()
    assert (empty["level"], empty["as_of"], empty["rows"]) == ("keyword", None, [])
    assert client.get(f"{V1}/rankings?level=bogus", headers=viewer_h).status_code == 422


# --------------------------------------------------------------------------- proposals


def test_proposals_list_and_get(client: TestClient, viewer_h: H, world: World) -> None:
    rows = client.get(f"{V1}/proposals?status=awaiting_approval", headers=viewer_h).json()
    assert {r["id"] for r in rows} == {
        world.proposal_waiting,
        world.proposal_waiting2,
        world.proposal_expired,
    }
    p = client.get(f"{V1}/proposals/{world.proposal_waiting}", headers=viewer_h).json()
    assert p["approval"]["channel"] == "slack" and p["approval"]["decided_at"] is None
    assert (p["entity_name"], p["platform"]) == ("meta-c1", "meta")
    assert (p["campaign_id"], p["campaign_name"]) == (world.meta_campaign, "meta-c1")
    assert p["guardrail_result"] is None
    # no guardrail result yet: before = current budget, after = +15 % estimate
    assert p["change"] == {
        "field": "daily_budget",
        "before": "1000.00",
        "after": "1150.00",
        "after_is_estimate": True,
    }
    assert client.get(f"{V1}/proposals/999999", headers=viewer_h).status_code == 404
    assert client.get(f"{V1}/proposals?status=nope", headers=viewer_h).status_code == 422


def test_proposal_typed_guardrail_result(
    client: TestClient, viewer_h: H, world: World, seeded: Session
) -> None:
    p = seeded.get(Proposal, world.proposal_waiting2)
    assert p is not None
    p.guardrail_result = {
        "checked_at": "2026-11-11T01:00:00+00:00",
        "mode": "suggest",
        "approved": False,
        "result": "needs_approval",
        "reasons": ["mode_suggest"],
        "new_daily_budget": "1100.00",
        "detail": {"current": "1000.00", "lower_bound": "800.00", "upper_bound": "1200.00"},
    }
    blocked = seeded.get(Proposal, world.proposal_pending)
    assert blocked is not None
    blocked.guardrail_result = {"result": "block", "reason": "cooldown", "detail": {}}
    seeded.flush()
    d = client.get(f"{V1}/proposals/{world.proposal_waiting2}", headers=viewer_h).json()
    g = d["guardrail_result"]
    assert (g["result"], g["reasons"], g["new_daily_budget"], g["reason"]) == (
        "needs_approval",
        ["mode_suggest"],
        "1100.00",
        None,
    )
    assert (d["platform"], d["entity_name"]) == ("google", "google-g1")
    assert (d["change"]["before"], d["change"]["after"]) == ("1000.00", "1100.00")
    b = client.get(f"{V1}/proposals/{world.proposal_pending}", headers=viewer_h).json()
    assert (b["guardrail_result"]["result"], b["guardrail_result"]["reason"]) == (
        "block",
        "cooldown",
    )


def test_approve_and_reject(
    client: TestClient, approver_h: H, world: World, seeded: Session
) -> None:
    r = client.post(
        f"{V1}/proposals/{world.proposal_waiting}/approve",
        headers=approver_h,
        json={"comment": "ok"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal"]["status"] == "approved"
    assert "Phase 3" in body["next_step"]
    appr = seeded.scalars(
        select(Approval).where(Approval.proposal_id == world.proposal_waiting)
    ).one()
    assert (appr.decision, appr.decided_by, appr.comment, appr.channel) == (
        "approved",
        "admin",
        "ok",
        "slack",
    )
    # deciding twice is a conflict
    again = client.post(f"{V1}/proposals/{world.proposal_waiting}/approve", headers=approver_h)
    assert again.status_code == 409
    # reject needs a reason; creates an approvals row if the approval step hadn't yet
    assert (
        client.post(
            f"{V1}/proposals/{world.proposal_waiting2}/reject", headers=approver_h, json={}
        ).status_code
        == 422
    )
    r = client.post(
        f"{V1}/proposals/{world.proposal_waiting2}/reject",
        headers=approver_h,
        json={"reason": "no"},
    )
    assert r.json()["proposal"]["status"] == "rejected"
    assert r.json()["proposal"]["approval"]["channel"] == "cli"
    # expired / not awaiting / missing
    assert (
        client.post(
            f"{V1}/proposals/{world.proposal_expired}/approve", headers=approver_h
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"{V1}/proposals/{world.proposal_pending}/approve", headers=approver_h
        ).status_code
        == 409
    )
    assert client.post(f"{V1}/proposals/999999/approve", headers=approver_h).status_code == 404
    assert len(_audit(seeded, "proposal.approve")) == 1
    assert len(_audit(seeded, "proposal.reject")) == 1
    # nothing was executed: no proposal reached executing/executed
    statuses = set(seeded.scalars(select(Proposal.status)))
    assert not statuses & {"executing", "executed"}


def test_approve_batch(client: TestClient, approver_h: H, world: World) -> None:
    ids = [world.proposal_waiting, world.proposal_expired, world.proposal_waiting2, 999999]
    r = client.post(f"{V1}/proposals/approve-batch", headers=approver_h, json={"ids": ids})
    res = {x["proposal_id"]: x for x in r.json()["results"]}
    assert res[world.proposal_waiting]["ok"] and res[world.proposal_waiting2]["ok"]
    assert (
        not res[world.proposal_expired]["ok"] and "expired" in res[world.proposal_expired]["error"]
    )
    assert not res[999999]["ok"]
    assert (
        client.post(
            f"{V1}/proposals/approve-batch", headers=approver_h, json={"ids": []}
        ).status_code
        == 422
    )


def test_manual_proposal(
    client: TestClient, admin_h: H, approver_h: H, world: World, seeded: Session
) -> None:
    body: dict[str, Any] = {
        "entity_type": "campaign",
        "entity_id": world.meta_campaign,
        "action": "budget_change",
        "params": {"change_pct": -10},
        "reason": "manual trim",
        "risk": "low",
    }
    assert client.post(f"{V1}/proposals", headers=approver_h, json=body).status_code == 403
    r = client.post(f"{V1}/proposals", headers=admin_h, json=body)
    assert r.status_code == 201, r.text
    p = r.json()
    assert (p["status"], p["source"], p["spend_direction"], p["as_of"]) == (
        "pending",
        "human",
        "reducing",
        "2026-11-11",
    )
    assert len(p["dedupe_key"]) == 64
    assert client.post(f"{V1}/proposals", headers=admin_h, json=body).status_code == 409
    bad = body | {"params": {}}
    assert client.post(f"{V1}/proposals", headers=admin_h, json=bad).status_code == 422
    assert (
        client.post(f"{V1}/proposals", headers=admin_h, json=body | {"action": "noop"}).status_code
        == 422
    )
    missing = body | {"entity_id": 999999}
    assert client.post(f"{V1}/proposals", headers=admin_h, json=missing).status_code == 404
    pause = body | {
        "action": "pause",
        "params": {},
        "entity_type": "ad",
        "entity_id": world.meta_ads[0],
    }
    assert (
        client.post(f"{V1}/proposals", headers=admin_h, json=pause).json()["spend_direction"]
        == "reducing"
    )


def test_dry_run_not_enabled(client: TestClient, viewer_h: H, world: World) -> None:
    r = client.get(f"{V1}/proposals/{world.proposal_waiting}/dry-run", headers=viewer_h)
    assert r.status_code == 501
    assert r.json()["detail"] == {
        "code": "not_enabled",
        "phase": "3",
        "message": "dry-run payloads are built by the executor, which ships in Phase 3",
    }
    assert client.get(f"{V1}/proposals/999999/dry-run", headers=viewer_h).status_code == 404


# --------------------------------------------------------------------------- changes


def test_changes_and_evaluations(
    client: TestClient, viewer_h: H, approver_h: H, world: World
) -> None:
    rows = client.get(f"{V1}/changes", headers=viewer_h).json()
    assert [r["id"] for r in rows] == [world.change]
    assert rows[0]["verdict"] == "better" and len(rows[0]["evaluations"]) == 2
    assert rows[0]["entity_name"] == "meta-c1" and rows[0]["campaign_id"] == world.meta_campaign
    assert rows[0]["change"] == {
        "field": "status",
        "before": "ACTIVE",
        "after": "PAUSED",
        "after_is_estimate": True,  # intended (no read-back `after` yet)
    }
    assert client.get(f"{V1}/changes?since=7d", headers=viewer_h).json() == []
    assert len(client.get(f"{V1}/changes?since=2026-11-01", headers=viewer_h).json()) == 1
    assert client.get(f"{V1}/changes?since=lol", headers=viewer_h).status_code == 422
    ev = client.get(f"{V1}/evaluations?change_id={world.change}", headers=viewer_h).json()
    assert sorted(e["horizon_days"] for e in ev) == [3, 7]
    assert client.get(f"{V1}/evaluations?open_only=true", headers=viewer_h).json() == []
    r = client.post(f"{V1}/changes/{world.change}/rollback", headers=approver_h)
    assert r.status_code == 501 and r.json()["detail"]["phase"] == "3"
    assert client.post(f"{V1}/changes/999999/rollback", headers=approver_h).status_code == 404


# --------------------------------------------------------------------------- config


def test_rules_and_limits(client: TestClient, viewer_h: H, admin_h: H, seeded: Session) -> None:
    rules = client.get(f"{V1}/rules", headers=viewer_h).json()
    assert rules["active"]["version"] == 1 and len(rules["history"]) == 1
    limits = client.get(f"{V1}/limits", headers=viewer_h).json()
    body = limits["active"]["body"]

    new = {**body, "caps": {**body["caps"], "meta": {"daily": 11000, "monthly": 300000}}}
    r = client.put(f"{V1}/limits", headers=admin_h, json={"body": new, "reason": "tighten"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["active"]["version"] == 2 and d["active"]["body"]["version"] == 2
    assert [h["active"] for h in d["history"]] == [True, False]
    assert (_audit(seeded, "config.limits.update")[0].payload or {})["previous_version"] == 1

    too_high = {**body, "caps": {**body["caps"], "total": {"daily": 200000, "monthly": 550000}}}
    r = client.put(f"{V1}/limits", headers=admin_h, json={"body": too_high, "reason": "x"})
    assert r.status_code == 422 and "HARD" in r.text
    r = client.put(f"{V1}/limits", headers=admin_h, json={"body": {"nope": 1}, "reason": "x"})
    assert r.status_code == 422
    assert (
        client.put(f"{V1}/limits", headers=admin_h, json={"body": body}).status_code == 422
    )  # reason

    rbody = rules["active"]["body"]
    bad_rule = {**rbody, "rules": [{**rbody["rules"][0], "when": "made_up_var > 1"}]}
    r = client.put(f"{V1}/rules", headers=admin_h, json={"body": bad_rule, "reason": "x"})
    assert r.status_code == 422 and "made_up_var" in r.text
    usd = {**rbody, "targets": {**rbody["targets"], "currency": "USD"}}
    assert (
        client.put(f"{V1}/rules", headers=admin_h, json={"body": usd, "reason": "x"}).status_code
        == 422
    )
    ok = client.put(f"{V1}/rules", headers=admin_h, json={"body": rbody, "reason": "same"})
    assert ok.status_code == 200 and ok.json()["active"]["version"] == 2
    actives = seeded.scalars(select(ConfigVersion).where(ConfigVersion.active.is_(True))).all()
    assert len(actives) == 2  # one per kind


# ------------------------------------------------------------- llm / alerts / emergency


def test_llm_usage(client: TestClient, viewer_h: H, world: World) -> None:
    d = client.get(f"{V1}/llm/usage", headers=viewer_h).json()
    assert d["today"]["calls"] == 1 and d["today"]["input_tokens"] == 1000
    assert Decimal(d["month_to_date"]["cost_usd"]) == Decimal("0.0512")
    assert set(d["by_purpose"]) == {"triage", "analyst"}
    assert [x["date"] for x in d["days"]] == ["2026-11-08", "2026-11-11"]
    assert d["llm_enabled"] is False and d["daily_token_cap"] == 100000
    d1 = client.get(f"{V1}/llm/usage?range=1d", headers=viewer_h).json()
    assert [x["tier"] for x in d1["days"]] == ["small"]
    assert Decimal(d1["month_to_date"]["cost_usd"]) == Decimal("0.0512")  # MTD ignores range


def test_alerts(
    client: TestClient, viewer_h: H, approver_h: H, world: World, seeded: Session
) -> None:
    open_ = client.get(f"{V1}/alerts", headers=viewer_h).json()
    assert [a["id"] for a in open_] == [world.alert]
    assert len(client.get(f"{V1}/alerts?open=false", headers=viewer_h).json()) == 2
    assert (
        client.get(f"{V1}/alerts?open=false&severity=critical", headers=viewer_h).json()[0][
            "message"
        ]
        == "acked"
    )
    r = client.post(f"{V1}/alerts/{world.alert}/ack", headers=approver_h)
    assert r.status_code == 200 and r.json()["acked_by"] == "admin"
    assert (
        client.post(f"{V1}/alerts/{world.alert}/ack", headers=approver_h).status_code == 200
    )  # idempotent
    assert len(_audit(seeded, "alert.ack")) == 1
    assert client.get(f"{V1}/alerts", headers=viewer_h).json() == []
    assert client.post(f"{V1}/alerts/999999/ack", headers=approver_h).status_code == 404


def test_emergency_actions(client: TestClient, viewer_h: H, approver_h: H, world: World) -> None:
    rows = client.get(f"{V1}/emergency-actions?active_only=true", headers=viewer_h).json()
    assert [(r["id"], r["campaign_ids"]) for r in rows] == [
        (world.emergency, [world.meta_campaign])
    ]
    r = client.post(f"{V1}/emergency-actions/{world.emergency}/restore", headers=approver_h)
    assert r.status_code == 501
    assert (
        client.post(f"{V1}/emergency-actions/999999/restore", headers=approver_h).status_code == 404
    )


# ------------------------------------------------------------- control / jobs / system


def test_mode(client: TestClient, admin_h: H, seeded: Session) -> None:
    r = client.post(f"{V1}/control/mode", headers=admin_h, json={"mode": "suggest", "reason": "P2"})
    assert r.json() == {"mode": "suggest", "hard_max_mode": "suggest"}
    assert seeded.get(Client, "client_test").mode == "suggest"  # type: ignore[union-attr]
    r = client.post(f"{V1}/control/mode", headers=admin_h, json={"mode": "auto_small"})
    assert r.status_code == 403 and "HARD_MAX_MODE" in r.text
    assert client.post(f"{V1}/control/mode", headers=admin_h, json={"mode": "x"}).status_code == 422
    assert [(a.payload or {})["after"] for a in _audit(seeded, "control.mode")] == ["suggest"]


def test_kill_switch_env_override(
    seeded: Session, api_settings_factory: Callable[..., Settings], clock: Clock
) -> None:
    app = build_app(seeded, api_settings_factory(kill_switch=True), clock)
    with TestClient(app, base_url=BASE) as c:
        h = bearer(c)
        r = c.post(f"{V1}/control/kill-switch", headers=h, json={"on": False})
        d = r.json()
        assert (d["effective"], d["db"], d["env_override"]) == (True, False, True)
        assert "env KILL_SWITCH" in d["note"]


def test_pause_all_not_enabled(
    client: TestClient, approver_h: H, admin_h: H, seeded: Session
) -> None:
    assert client.post(f"{V1}/control/pause-all", headers=approver_h, json={}).status_code == 422
    r = client.post(
        f"{V1}/control/pause-all", headers=approver_h, json={"confirm": True, "platform": "meta"}
    )
    assert r.status_code == 501 and r.json()["detail"]["phase"] == "3"
    assert seeded.get(Client, "client_test").kill_switch is False  # type: ignore[union-attr]


def test_jobs_run_queues_a_request(
    client: TestClient, approver_h: H, admin_h: H, viewer_h: H, seeded: Session
) -> None:
    assert client.post(f"{V1}/jobs/hourly_pull/run", headers=approver_h).status_code == 403
    assert client.post(f"{V1}/jobs/nope/run", headers=admin_h).status_code == 404
    r = client.post(f"{V1}/jobs/emergency_restore/run", headers=admin_h)
    assert r.status_code == 501 and r.json()["detail"]["phase"] == "3"
    assert client.post(f"{V1}/jobs/retention/run", headers=admin_h).status_code == 422
    r = client.post(f"{V1}/jobs/hourly_pull/run", headers=admin_h)
    assert r.status_code == 202, r.text
    req = r.json()
    assert (req["job"], req["status"], req["requested_by"]) == ("hourly_pull", "queued", "admin")
    dup = client.post(f"{V1}/jobs/hourly_pull/run", headers=admin_h)
    assert dup.status_code == 409 and dup.json()["detail"]["request_id"] == req["id"]
    assert client.post(f"{V1}/jobs/pacing/run", headers=admin_h).status_code == 202
    rows = client.get(f"{V1}/jobs/requests", headers=viewer_h).json()
    assert {x["job"] for x in rows} == {"hourly_pull", "pacing"}
    one = client.get(f"{V1}/jobs/requests/{req['id']}", headers=viewer_h).json()
    assert one["id"] == req["id"]
    assert client.get(f"{V1}/jobs/requests/999999", headers=viewer_h).status_code == 404
    assert len(_audit(seeded, "job.run_requested")) == 2
    assert seeded.scalar(select(JobRequest.status).where(JobRequest.id == req["id"])) == "queued"


def test_system(client: TestClient, viewer_h: H, world: World) -> None:
    d = client.get(f"{V1}/system", headers=viewer_h).json()
    assert [a["platform"] for a in d["ad_accounts"]] == ["meta", "google"]
    assert all(a["stale"] is False for a in d["ad_accounts"])
    [latest] = [j for j in d["latest_job_runs"] if j["job"] == "hourly_pull"]
    assert latest["status"] == "success"
    assert [f["error"] for f in d["recent_failures"]] == ["boom"]
    assert d["heartbeat"]["last_status"] == "unknown" and d["heartbeat"]["healthy"] is False
    ops = {o["platform"]: o for o in d["platform_operations"]}
    assert ops["google"]["limit"] == 2880 and ops["google"]["used"] is None
    assert ops["meta"]["limit"] is None
    assert "daily_pipeline" in d["manual_jobs"] and "retention" not in d["manual_jobs"]


def test_system_heartbeat_and_operations(
    client: TestClient, viewer_h: H, world: World, seeded: Session
) -> None:
    from engine.db.models import JobRun

    seeded.add(
        JobRun(
            client_id=CID,
            job="pacing",
            step=None,
            window_key="2026-11-11T11:45",
            started_at=NOW - dt.timedelta(minutes=10),
            finished_at=NOW - dt.timedelta(minutes=10),
            status="success",
        )
    )
    seeded.add_all(
        [
            SystemStatus(
                client_id=CID,
                key="heartbeat",
                value={
                    "configured": True,
                    "last_status": "ok",
                    "last_ping_at": (NOW - dt.timedelta(minutes=10)).isoformat(),
                },
                updated_at=NOW,
            ),
            SystemStatus(
                client_id=CID,
                key="operations:google",
                value={"day": "2026-11-11", "used": 1440, "limit": 2880},
                updated_at=NOW,
            ),
            SystemStatus(
                client_id=CID,
                key="operations:meta",
                value={"day": "2026-11-10", "used": 99, "limit": None},
                updated_at=NOW,
            ),
        ]
    )
    seeded.flush()
    d = client.get(f"{V1}/system", headers=viewer_h).json()
    hb = d["heartbeat"]
    assert hb["configured"] and hb["last_status"] == "ok" and hb["healthy"] is True
    ops = {o["platform"]: o for o in d["platform_operations"]}
    assert (ops["google"]["used"], ops["google"]["fraction"]) == (1440, 0.5)
    assert (ops["meta"]["used"], ops["meta"]["day"]) == (0, "2026-11-11")  # yesterday's count
    assert d["auth_mode"] == "env"
