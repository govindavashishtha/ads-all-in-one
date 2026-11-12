"""Fake-platform write-path demo on a *copy* of a scoring DB (never the live score_demo).

Flow: approve → execute → D+3 safety check → D+7 verdict → rollback.

Usage (after copying the DB and migrating 0004 — see docs/EXEC_DEMO.md):

    uv run python scripts/exec_demo.py

Talks only to DATABASE_URL (default: ads_autopilot_exec_demo). No Meta/Google network
calls, no real credentials: FakeWriteClients + fake_platforms_from_db.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from engine.approvals.service import decide
from engine.config import Settings
from engine.db.models import Proposal
from engine.enums import Action, EntityType, Platform, ProposalSource, ProposalStatus
from engine.evaluation.evaluate import evaluate_due
from engine.executor.apply import WriteEnv
from engine.executor.clients import FakeWriteClients
from engine.executor.execute import execute_proposal
from engine.executor.fake_world import fake_platforms_from_db
from engine.executor.rollback import rollback_change
from engine.proposals import spend_direction

CID = "client_001"
TZ = ZoneInfo("Asia/Kolkata")
# Applied at noon IST on 2026-11-11 (the day after score_demo metrics end).
T0 = dt.datetime(2026, 11, 11, 6, 30, tzinfo=dt.UTC)
RULE = "exec_demo_budget"
URL_DEFAULT = "postgresql+psycopg://ads:ads@127.0.0.1:5432/ads_autopilot_exec_demo"
# Post-period numbers in the same ballpark as recent ad-group-1 days, with conversions
# so D+3 is not "worse" and D+7 has enough spend (not invented caps — demo metrics only).
POST_SPEND = Decimal("1000.00")
POST_CONV = Decimal("3")


class Clock:
    def __init__(self, t: dt.datetime) -> None:
        self.t = t

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += dt.timedelta(**kw)
        print(f"  clock → {self.t.astimezone(TZ).isoformat()} Asia/Kolkata")


def settings(url: str) -> Settings:
    return Settings(
        _env_file=None,
        app_env="development",
        client_id=CID,
        client_name="Exec demo (copy of score_demo)",
        timezone="Asia/Kolkata",
        database_url=url,
    )


def report(title: str, rows: object) -> None:
    print(f"\n== {title} ==")
    print(rows)


def seed_post_metrics(conn, entity_id: int, platform: str, start: dt.date, end: dt.date) -> int:
    n = 0
    day = start
    while day <= end:
        r = conn.execute(
            text(
                "INSERT INTO metrics_daily (client_id, platform, entity_type, entity_id, "
                "date, spend, conversions, conv_value) "
                "VALUES (:c, :pl, 'campaign', :i, :d, :s, :v, 0) "
                "ON CONFLICT DO NOTHING"
            ),
            {"c": CID, "pl": platform, "i": entity_id, "d": day, "s": POST_SPEND, "v": POST_CONV},
        )
        n += r.rowcount or 0
        day += dt.timedelta(days=1)
    return n


def main() -> int:  # noqa: PLR0915
    url = os.environ.get("DATABASE_URL", URL_DEFAULT)
    if "score_demo" in url:
        print(
            "refusing to run against score_demo (that DB is in use by the live services)",
            file=sys.stderr,
        )
        return 2
    engine = create_engine(url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    cfg = settings(url)
    clock = Clock(T0)

    with engine.connect() as c:
        ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if ver != "0004":
            print(f"need migration 0004 on this copy (at {ver})", file=sys.stderr)
            return 2
        client = c.execute(
            text("SELECT client_id, mode, kill_switch FROM clients WHERE client_id = :c"),
            {"c": CID},
        ).first()
        if client is None:
            print(f"client {CID} not found", file=sys.stderr)
            return 2

    with sessions() as s, s.begin():
        s.execute(
            text(
                "UPDATE clients SET execution_enabled = true, mode = 'suggest', "
                "kill_switch = false, updated_at = :t WHERE client_id = :c"
            ),
            {"c": CID, "t": clock()},
        )
        camp = s.execute(
            text(
                "SELECT c.id, c.platform, c.external_id, c.daily_budget, c.name "
                "FROM campaigns c "
                "JOIN features f ON f.client_id = c.client_id "
                "AND f.entity_type = 'campaign' AND f.entity_id = c.id "
                "AND f.as_of = (SELECT max(as_of) FROM features WHERE client_id = :c) "
                "WHERE c.client_id = :c AND c.status = 'active' AND c.managed "
                "AND c.daily_budget IS NOT NULL AND f.learning IS FALSE "
                "ORDER BY c.id LIMIT 1"
            ),
            {"c": CID},
        ).first()
        if camp is None:
            print("no active managed campaign with a daily budget out of learning", file=sys.stderr)
            return 2
        unit_id = int(camp.id)
        camp_ext = str(camp.external_id)
        camp_platform = Platform(camp.platform)
        print(
            f"target: campaign {unit_id} {camp.name} ({camp.platform} {camp_ext}) "
            f"daily_budget={camp.daily_budget}"
        )
        existing = s.scalars(
            select(Proposal).where(
                Proposal.client_id == CID,
                Proposal.rule_key == RULE,
                Proposal.status.in_(
                    (
                        ProposalStatus.AWAITING_APPROVAL.value,
                        ProposalStatus.APPROVED.value,
                        ProposalStatus.EXECUTED.value,
                    )
                ),
            )
        ).first()
        if existing is not None:
            pid = int(existing.id)
            unit_id = int(existing.entity_id)
            print(f"reusing existing demo proposal #{pid} (status={existing.status})")
        else:
            params = {"change_pct": -15}
            p = Proposal(
                client_id=CID,
                dedupe_key=f"demo:{RULE}:{unit_id}",
                as_of=(T0 - dt.timedelta(days=1)).date(),
                source=ProposalSource.RULE.value,
                rule_key=RULE,
                entity_type=EntityType.CAMPAIGN.value,
                entity_id=unit_id,
                action=Action.BUDGET_CHANGE.value,
                params=params,
                reason="exec_demo: -15% daily budget (fake platform only)",
                risk="low",
                spend_direction=spend_direction(Action.BUDGET_CHANGE, params).value,
                status=ProposalStatus.AWAITING_APPROVAL.value,
                expires_at=T0 + dt.timedelta(days=7),
            )
            s.add(p)
            s.flush()
            pid = int(p.id)
            print(
                f"inserted awaiting_approval proposal #{pid} "
                f"(budget_change campaign {unit_id} / {camp_ext})"
            )

    with sessions() as s:
        platforms = fake_platforms_from_db(s, CID)
    clients = FakeWriteClients(platforms)
    env = WriteEnv(cfg, sessions, clients, clock)

    # --- approve ---
    with sessions() as s, s.begin():
        p = s.get(Proposal, pid)
        assert p is not None
        if p.status == ProposalStatus.AWAITING_APPROVAL.value:
            decide(
                s,
                client_id=CID,
                proposal_id=pid,
                decision="approved",
                actor="exec_demo",
                role="approver",
                via="cli",
                comment="demo approve (fake platform)",
                now=clock(),
                ip=None,
            )
            print(f"approved proposal #{pid}")
        else:
            print(f"proposal #{pid} already {p.status} — skipping approve")

    # --- execute ---
    result = execute_proposal(env, pid)
    print(f"execute_proposal → {result.status}: {result.detail}")
    with engine.connect() as c:
        changes = list(
            c.execute(
                text(
                    "SELECT id, status, entity_type, entity_id, before, after, "
                    "idempotency_key, origin FROM change_log "
                    "WHERE client_id = :c AND proposal_id = :p ORDER BY id"
                ),
                {"c": CID, "p": pid},
            )
        )
        evals = list(
            c.execute(
                text(
                    "SELECT id, horizon_days, due_at, unit_entity_type, unit_entity_id, "
                    "verdict, action_taken, done_at FROM evaluations "
                    "WHERE client_id = :c AND change_id = ANY(:ids) ORDER BY horizon_days, id"
                ),
                {"c": CID, "ids": [r.id for r in changes] or [0]},
            )
        )
    report("change_log", changes)
    report("evaluations (scheduled)", evals)
    if not changes or changes[0].status != "applied":
        print("expected an applied change — stopping", file=sys.stderr)
        return 1
    change_id = int(changes[0].id)
    fake = platforms[camp_platform].state(EntityType.CAMPAIGN, camp_ext)
    print(f"fake Meta campaign {camp_ext} daily_budget = {fake.daily_budget}")

    # --- D+3 ---
    with engine.begin() as c:
        n = seed_post_metrics(
            c, unit_id, camp_platform.value, dt.date(2026, 11, 12), dt.date(2026, 11, 18)
        )
        print(f"seeded {n} post-period campaign metric days (demo only, not live spend)")
    clock.t = T0 + dt.timedelta(days=4)  # 15 Nov, post D+3 (14 Nov) is complete
    d3 = evaluate_due(env)
    print(f"evaluate_due at D+3 window → {d3}")

    # --- D+7 (due_at = applied + 7 + settle_days=2; post ends 18 Nov, today must be 19+) ---
    clock.t = T0 + dt.timedelta(days=9)  # 20 Nov 12:00 IST
    d7 = evaluate_due(env)
    print(f"evaluate_due at D+7 window → {d7}")
    with engine.connect() as c:
        ev = list(
            c.execute(
                text(
                    "SELECT horizon_days, verdict, action_taken, done_at, p_worse, p_better "
                    "FROM evaluations WHERE client_id = :c AND change_id = :i "
                    "ORDER BY horizon_days"
                ),
                {"c": CID, "i": change_id},
            )
        )
        stats = list(
            c.execute(
                text(
                    "SELECT rule_key, n_changes, n_better, n_neutral, n_worse "
                    "FROM rule_stats WHERE client_id = :c"
                ),
                {"c": CID},
            )
        )
    report("evaluations (after D+7)", ev)
    report("rule_stats", stats)

    # --- rollback ---
    rb = rollback_change(env, change_id, requested_by="exec_demo")
    print(f"rollback_change #{change_id} → {rb.status}: {rb.detail} (new change={rb.change_id})")
    with engine.connect() as c:
        trail = list(
            c.execute(
                text(
                    "SELECT id, status, action, rollback_of, applied_by, origin "
                    "FROM change_log WHERE client_id = :c AND "
                    "(id = :i OR proposal_id = :p OR rollback_of = :i) ORDER BY id"
                ),
                {"c": CID, "i": change_id, "p": pid},
            )
        )
    report("change_log trail", trail)
    fake = platforms[camp_platform].state(EntityType.CAMPAIGN, camp_ext)
    print(f"fake Meta campaign {camp_ext} daily_budget after rollback = {fake.daily_budget}")
    print("\nDEMO OK (fake platform only; no live writes)")
    return 0


if __name__ == "__main__":
    # Satisfy "unused import" if Client is only for typing at runtime wipe — keep Client
    # imported so a missing clients table fails loudly at import-analyse time? actually unused.
    raise SystemExit(main())
