# Executor write-path demo (fake platforms only)

A one-shot walk through approve → execute → D+3 → D+7 → rollback on a **copy** of
`ads_autopilot_score_demo`. Nothing is sent to Meta or Google.

The live scoring services (uvicorn :8000, dashboard :3000, Caddy :8080) keep using
`ads_autopilot_score_demo`. Do not point them at this copy, and do not run the script
against `score_demo`.

## 1. Copy the DB and apply migration 0004

`score_demo` is still on 0003 (no `execution_enabled` / `write_requests`). A
`CREATE DATABASE … TEMPLATE` would require disconnecting its users, so dump/restore:

```bash
# as a user who can create databases (local ads role is enough on this box)
/home/box/pg16/pgctl.sh start   # if needed
psql -h 127.0.0.1 -U ads -d postgres -c "DROP DATABASE IF EXISTS ads_autopilot_exec_demo"
psql -h 127.0.0.1 -U ads -d postgres -c "CREATE DATABASE ads_autopilot_exec_demo OWNER ads"
pg_dump -h 127.0.0.1 -U ads --no-owner --no-acl ads_autopilot_score_demo \
  | psql -h 127.0.0.1 -U ads -d ads_autopilot_exec_demo -v ON_ERROR_STOP=1
DATABASE_URL=postgresql+psycopg://ads:ads@127.0.0.1:5432/ads_autopilot_exec_demo \
  uv run alembic -c db/alembic.ini upgrade head
```

## 2. Run the demo

```bash
DATABASE_URL=postgresql+psycopg://ads:ads@127.0.0.1:5432/ads_autopilot_exec_demo \
  uv run python scripts/exec_demo.py
```

What it does (clock starts 2026-11-11 12:00 Asia/Kolkata, the day after score_demo metrics
end):

1. Turns `execution_enabled` on and keeps `mode=suggest` **on the copy only**.
2. Inserts an `awaiting_approval` -15% budget change on the first active managed campaign that is out of learning and has a campaign-level daily budget (`rule_key=exec_demo_budget`) and
   approves it through `engine.approvals.service.decide`.
3. Executes it through `FakeWriteClients` built from `fake_platforms_from_db` (write-ahead
   `change_log`, validate-only then apply, D+3/D+7 evaluation rows).
4. Seeds post-period *demo* metrics on that campaign (not live spend) so D+3 is not
   `worse` and D+7 has enough volume, advances the clock, and runs `evaluate_due`.
5. Rolls the change back through the executor (guarded; the fake campaign budget returns to its previous value).

Re-running on the same copy reuses the existing demo proposal (execute is idempotent; a
second rollback is a no-op / skip). For a clean repeat, drop and recreate the copy (step 1).

## 3. What this does *not* do

No write tokens, no network calls to Meta/Google, no change to `HARD_MAX_MODE`, no flip of
`execution_enabled` on `score_demo` or any live database. See D-27..D-34 and the sign-off
list in the executor hand-off.
