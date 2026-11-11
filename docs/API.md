# API (FastAPI, `/api/v1`) — plan §17

Machine-readable contract: [`docs/openapi.json`](openapi.json) (regenerate with
`uv run python -m api.openapi_export`; `tests/unit/test_openapi_snapshot.py` fails if stale).
Interactive docs when running: `/api/v1/docs`.

## Run

```bash
/home/box/pg16/pgctl.sh start                    # local Postgres (box)
make migrate && make bootstrap                   # schema + seed client (needs HARD_* caps)
uv run python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('…'))"
#   -> put the hash in ADMIN_PASSWORD_HASH; set SECRET_KEY (32+ chars)
uv run uvicorn api.main:app --port 8000          # direct: http://127.0.0.1:8000/api/v1
# behind Caddy (§18): https://<host>/ads-autopilot/api/v1  (prefix stripped by the proxy)
uv run ads --api-url http://127.0.0.1:8000/api/v1 login
uv run ads status
```

Tests: `TEST_DATABASE_URL=postgresql+psycopg://ads:ads@127.0.0.1:5432/ads_autopilot_test_api make test`
(API/CLI tests live in `tests/api/`, marked `integration`).

## Auth (AUTH_MODE=env)

| Client | How | CSRF |
|---|---|---|
| Browser | `POST /auth/login {username,password}` → sets `aa_session` (httpOnly, Secure, SameSite=Lax, path `/`) and `aa_csrf` (readable) cookies; body has `csrf_token` | every non-GET needs header `X-CSRF-Token: <aa_csrf>` (must equal the cookie and the HMAC of the session) |
| CLI / scripts | `POST /auth/token {username,password,role?}` → `{access_token}`; send `Authorization: Bearer …` | none |

- Sessions are server-side rows (`sessions`, only `sha256(token)` stored), checked on every
  request; `POST /auth/logout` revokes the current one; admins can list
  (`GET /auth/sessions`) and revoke (`POST /auth/sessions/{id}/revoke`) any session.
- A browser session can't be used as a bearer token and vice versa.
- `role` on `/auth/token` down-scopes a CLI token (e.g. a `viewer` token for a read-only script).
- Login rate limit: 5 failed attempts / 15 min per IP and per username → `429` + `Retry-After`.
- `401` = no/invalid/expired/revoked session · `403` = role too low or CSRF failure ·
  `503` = auth not configured (`ADMIN_PASSWORD_HASH` argon2id / `SECRET_KEY`) or client not
  bootstrapped.
- `AUTH_MODE=main_system` (Phase 6): every request → `501`; `/auth/login|token` → `409`.
  Contract for the implementation is in `api/auth/main_system_auth.py` (INPUT I-11).

Roles: `viewer` (GET only) < `approver` (+ approve/reject/rollback/restore/ack/pause-all) <
`admin` (everything). The env admin is `admin`.

## Conventions

- Money = JSON **string**, major units, client currency (`"1234.50"`); ratios are numbers.
- Metric `date` / `ts_hour` are ad-account-local (`ts_hour` has no offset); other timestamps UTC.
- `range=7d` windows are the last N **complete** account days (ending yesterday).
- Not-enabled-yet routes answer **`501`** with
  `{"detail":{"code":"not_enabled","phase":"3","message":"…"}}` — they never change state.
- Lists take `limit`/`offset` where noted; newest first.

## Endpoints

Status: **live** = implemented over the tables; **501** = route, auth and schema exist, logic
ships in the named phase. DB-only writes (no ad-platform call) are audited in `audit_log`.

| Method | Path | Role | Status | Notes |
|---|---|---|---|---|
| GET | `/health` | none | live | `{"status":"ok"}` |
| POST | `/auth/login`, `/auth/token` | — | live | see Auth |
| POST | `/auth/logout` | viewer | live | revokes current session |
| GET | `/auth/me` | viewer | live | |
| GET | `/auth/sessions` · POST `/auth/sessions/{id}/revoke` | admin | live | extra to §17 |
| GET | `/overview?range=7d` | viewer | live | per platform + total: spend today / daily cap, MTD / monthly cap, range CPA/ROAS/CTR, elapsed-day fraction, alert counts, open approvals, active emergency actions, mode, kill switch. Projected EOD (intraday curve) is the pacing job's, not exposed yet |
| GET | `/campaigns` (`platform`,`managed`,`status`,`range`) · `/campaigns/{id}` · `/ad-groups/{id}` · `/ads/{id}` · `/keywords?ad_group_id=` | viewer | live | each row has `metrics` (range sums) + latest `score`; details nest children |
| PUT | `/campaigns/{id}/managed` `{managed, reason?}` | admin | live | DB flag (INPUT I-3) |
| GET | `/metrics?entity_type=&entity_id=&grain=day\|hour&from=&to=&platform=` | viewer | live | hour only for account/campaign |
| GET | `/rankings?level=ad\|ad_group\|keyword\|campaign&platform=&top=` | viewer | live | latest `scores.as_of`; empty until scoring runs (Phase 2) |
| GET | `/proposals?status=` · `/proposals/{id}` | viewer | live | includes latest approval row |
| POST | `/proposals/{id}/approve` `{comment?}` · `/reject` `{reason}` · `/approve-batch` `{ids,comment?}` | approver | live | records decision → `approved`/`rejected`; 409 if not `awaiting_approval` or expired. **The executor (Phase 3) re-runs the guardrail with fresh state before applying; nothing is sent to a platform now** |
| POST | `/proposals` | admin | live | manual `source=human` proposal inserted `pending`; guardrailed by the pipeline's guardrail step |
| GET | `/proposals/{id}/dry-run` | viewer | **501** (Phase 3) | executor builds payloads |
| GET | `/changes?since=7d\|YYYY-MM-DD&status=` | viewer | live | with evaluations + latest D+7/D+14 `verdict` |
| POST | `/changes/{id}/rollback` | approver | **501** (Phase 3) | |
| GET | `/evaluations?change_id=&open_only=` | viewer | live | |
| GET | `/rules`, `/limits` | viewer | live | active version + history |
| PUT | `/rules`, `/limits` `{body, reason}` | admin | live | new active version; schema + registry names + `HARD_*` ceilings + currency validated (422) |
| GET | `/llm/usage?range=30d` | viewer | live | from `llm_calls` (empty until Phase 4; `llm_enabled=false`) |
| GET | `/alerts?open=true&severity=` · POST `/alerts/{id}/ack` | viewer · approver | live | |
| GET | `/emergency-actions?active_only=` | viewer | live | |
| POST | `/emergency-actions/{id}/restore` | approver | **501** (Phase 3) | |
| POST | `/control/mode` `{mode, reason?}` | admin | live | 403 above `HARD_MAX_MODE` |
| POST | `/control/kill-switch` `{on, reason?}` | admin | live | returns effective/db/env; env override can't be cleared |
| POST | `/control/pause-all` `{platform?, confirm:true}` | approver | **501** (Phase 3) | 422 without `confirm` |
| POST | `/jobs/{name}/run` | admin | **501** | needs an API→engine trigger channel (not built); 404 for unknown job names |
| GET | `/system` | viewer | live | ad accounts + staleness (>2 h), latest run per job/step, 7-day failures; `platform_operations_today` null until connectors record it |
| GET | `/metrics` (root, not `/api/v1`) | bearer `METRICS_TOKEN` | live | Prometheus; 404 if token unset; not routed by the proxy |

## CLI (§19)

`ads login|logout|whoami|status|proposals|approve|reject|dry-run|changes|rollback|pause-all|restore|manage|mode|kill-switch|run|rankings|llm-usage|alerts|ack|version`,
global `--api-url` (`ADS_API_URL`), `--token-file` (`ADS_TOKEN_FILE`, default
`~/.config/ads-autopilot/credentials.json`, mode 0600), `--json`.
Exit codes: 0 ok · 1 error · 2 usage · 3 auth/forbidden · 4 not enabled (501).
