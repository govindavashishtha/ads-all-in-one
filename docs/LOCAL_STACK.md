# Local stack — `ads_autopilot_ui_demo`

Synthetic, development-only stack for UI QA. **No live Meta/Google
credentials.** Writes go through in-memory `FakeWriteClients`. Placeholder
caps live only in the process environment (`/tmp/ui-demo/e2e.env`, mode 0600)
and are the plan §12.2 seed examples, not I-2.

## URLs

| Service | URL |
|---|---|
| Dashboard (via Caddy) | http://localhost:8080/ads-autopilot |
| API (via Caddy) | http://localhost:8080/ads-autopilot/api/v1 |
| API health | http://localhost:8080/ads-autopilot/api/v1/health |
| API docs | http://localhost:8080/ads-autopilot/api/v1/docs |
| API direct | http://127.0.0.1:8000/api/v1 |
| Dashboard direct | http://127.0.0.1:3000/ads-autopilot |
| Postgres | 127.0.0.1:5432, database `ads_autopilot_ui_demo` (user `ads`) |

Use **`http://localhost`** (not `127.0.0.1`) in a browser: session cookies are
`Secure` and will only be stored on http://localhost or HTTPS.

Credentials: `/tmp/ui-demo/e2e.env` and `/tmp/ui-demo/e2e.pw` (owner-only).
The env admin is `admin`. A down-scoped viewer *browser* session can be
re-issued with `uv run python /tmp/ui-demo/issue_viewer_session.py` after
sourcing the env file (writes `/tmp/ui-demo/viewer.session`).

## PIDs (as of 12 Nov 2026, 10:55 Asia/Kolkata)

Recorded in `/tmp/ui-demo/pids` as well.

| Process | PID | Notes |
|---|---:|---|
| Postgres 16 | 80671 | `/home/box/pg16/pgctl.sh start` |
| Caddy | 104058 | `:8080`, `api`/`dashboard` in `/etc/hosts` → 127.0.0.1 |
| API wrapper (`uv run uvicorn`) | 339868 | |
| uvicorn | 339901 | `:8000 --root-path /ads-autopilot --proxy-headers --forwarded-allow-ips 127.0.0.1` |
| Dev engine wrapper | 339874 | |
| Dev engine | 339902 | `scripts/run_dev_engine.py` (fixtures + FakeWriteClients) |
| Dashboard (`next start`) | 346122 | `:3000`, real build (`NEXT_PUBLIC_MOCK_API` unset) |

Logs: `/tmp/ui-demo/api.log`, `/tmp/ui-demo/engine.log`, `/tmp/ui-demo/dash.log`.

## Start

Postgres must already be up (`/home/box/pg16/pgctl.sh start`). `/etc/hosts`
must contain `127.0.0.1 dashboard api` so the existing Caddyfile can proxy
to the local API and dashboard.

```bash
# 1. Create / migrate / seed (once). HARD_* stay in this subshell.
bash -lc '
  set -euo pipefail
  PGPASSWORD=ads psql -h 127.0.0.1 -U ads -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE datname = '"'"'ads_autopilot_ui_demo'"'"' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS ads_autopilot_ui_demo;
CREATE DATABASE ads_autopilot_ui_demo OWNER ads;
SQL
  set -a; . /tmp/ui-demo/e2e.env; set +a
  cd /workspace/ads-autopilot
  uv run alembic -c db/alembic.ini upgrade head
  uv run python -m engine.bootstrap
  uv run python scripts/seed_ui_demo.py
'

# 2. Long-lived processes (env sourced only inside the wrappers)
/tmp/ui-demo/start-api.sh    > /tmp/ui-demo/api.log    2>&1 &
/tmp/ui-demo/start-engine.sh > /tmp/ui-demo/engine.log 2>&1 &
/tmp/ui-demo/start-dash.sh   > /tmp/ui-demo/dash.log   2>&1 &

# Caddy, if it is not already on :8080:
#   cd /workspace/ads-autopilot
#   SITE_ADDRESS=:8080 /workspace/tools/caddy run --config proxy/Caddyfile --adapter caddyfile
```

The seed pins the job clock to **2026-11-11 22:00 Asia/Kolkata** (same
instant as `ads_autopilot_score_demo` / `llm_demo`) so rules, search-term
negatives, and the fake LLM triage produce a useful Approvals + AI usage
view. The API and dev engine then run on the wall clock.

`scripts/run_dev_engine.py` refuses to start unless `APP_ENV=development`.
It never loads platform credentials.

## Stop

```bash
# API + engine + dashboard only (leave Postgres and Caddy if other work needs them)
kill 339868 339901 339874 339902 346122
# or, if PIDs have changed:
#   kill $(awk -F= '/^(api|engine|dash)/{print $2}' /tmp/ui-demo/pids)
```

Do **not** point this stack at `ads_autopilot_score_demo`. Do not export
`HARD_DAILY_CAP` / `HARD_MONTHLY_CAP` into the test shell; integration
tests use their own `TEST_DATABASE_URL` (for the QA gate:
`ads_autopilot_test_qa`).
