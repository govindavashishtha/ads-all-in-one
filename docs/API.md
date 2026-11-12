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
# behind Caddy (§18): https://<host>/ads-autopilot/api/v1  (prefix stripped by the proxy);
# then also pass --root-path /ads-autopilot (so /api/v1/docs loads openapi.json through the
# prefix) and --proxy-headers --forwarded-allow-ips=<proxy IP or CIDR> (docker-compose.yml pins
# the proxy to ${PROXY_IP:-172.30.57.10} on network ${ADS_SUBNET:-172.30.57.0/24})
uv run ads --api-url http://127.0.0.1:8000/api/v1 login
uv run ads status
```

Tests: `TEST_DATABASE_URL=postgresql+psycopg://ads:ads@127.0.0.1:5432/ads_autopilot_test_api make test`
(API/CLI tests live in `tests/api/`, marked `integration`).

Cookies are always `Secure`: browsers only store them over HTTPS or on `http://localhost`.
Any other host needs TLS at the proxy (Caddy's automatic HTTPS with a real `SITE_ADDRESS`).

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
| GET | `/overview?range=7d` | viewer | live | per platform + total: spend today / daily cap, MTD / monthly cap, range CPA/ROAS/CTR, elapsed-day fraction, alert counts, open approvals, active emergency actions, mode, kill switch; `timezone`, `targets`, daily `trend[]` (`trend_days` 1–90, default 28), per-block `intraday[]` with the expected cumulative curve (pacing job's learned/default curve), `projected_eod` from the same-day pacing snapshot (`system_status`), else `null`; open warn/critical `alerts[]` |
| GET | `/campaigns` (`platform`,`managed`,`status`,`range`) · `/campaigns/{id}` · `/ad-groups/{id}` · `/ads/{id}` · `/keywords?ad_group_id=` | viewer | live | each row has `metrics` (range sums) + latest `score`; details nest children |
| PUT | `/campaigns/{id}/managed` `{managed, reason?}` | admin | live | DB flag (INPUT I-3) |
| GET | `/metrics?entity_type=&entity_id=&grain=day\|hour&from=&to=&platform=` | viewer | live | hour only for account/campaign |
| GET | `/rankings?level=ad\|ad_group\|keyword\|campaign&platform=&top=` | viewer | live | latest `scores.as_of`; empty until scoring runs (Phase 2); rows carry campaign/parent names, 14 d + 7 d spend/conversions, `fatigue_flag`, `learning`; response carries `targets` |
| GET | `/proposals?status=` · `/proposals/{id}` | viewer | live | includes latest approval row, `entity_name`, `platform`, `campaign_id/name`, `change {field,before,after,after_is_estimate}`, a typed `guardrail_result`, and for escalations (status `escalated`) the LLM `triage {outcome, action, confidence, tier, reason, cause, llm_call_id, proposal_id, at}` from `params.triage` |
| POST | `/proposals/{id}/approve` `{comment?}` · `/reject` `{reason}` · `/approve-batch` `{ids,comment?}` | approver | live | records decision → `approved`/`rejected`; 409 if not `awaiting_approval` or expired. The executor later re-runs the guardrail with fresh state (`approved=true`) before applying; nothing is sent at approve time |
| POST | `/proposals` | admin | live | manual `source=human` proposal inserted `pending`; guardrailed by the pipeline's guardrail step |
| GET | `/proposals/{id}/dry-run` | viewer | live | guardrail re-run (`approved=true`) on current DB state, then the planned ops with `before` as last synced; the platform is not called. Includes the write-gate state (D-28). A `plan_error` is returned rather than 4xx when the proposal cannot be turned into ops |
| GET | `/changes?since=7d\|YYYY-MM-DD&status=` | viewer | live | with evaluations + latest D+7/D+14 `verdict`; same `entity_name`/`change` fields as proposals |
| POST | `/changes/{id}/rollback` `{reason?}` | approver | live | 202 + `write_request` (D-29); 409 if not `applied` or already queued; 404 unknown. The engine re-reads the platform and either applies the rollback payload (D-33) or opens a `rollback_with_state_drift` proposal. Audited `write.rollback_requested` |
| GET | `/evaluations?change_id=&open_only=` | viewer | live | |
| GET | `/rules`, `/limits` | viewer | live | active version + history |
| PUT | `/rules`, `/limits` `{body, reason}` | admin | live | new active version; schema + registry names + `HARD_*` ceilings + currency validated (422) |
| GET | `/llm/usage?range=30d` | viewer | live | from `llm_calls` (days in the client timezone, by call time); `llm_enabled`, `provider`, `disabled_reason`, models from `engine.llm.status.llm_status` (same decision the engine makes) |
| GET | `/alerts?open=true&severity=` · POST `/alerts/{id}/ack` | viewer · approver | live | |
| GET | `/emergency-actions?active_only=` | viewer | live | |
| POST | `/emergency-actions/{id}/restore` `{reason?}` | approver | live | 202 + `write_request`; 409 if already restored or already queued. Engine re-enables only campaigns still PAUSED with last `entity_history.source = emergency`, behind the write gate. Audited |
| POST | `/control/mode` `{mode, reason?}` | admin | live | 403 above `HARD_MAX_MODE` |
| POST | `/control/kill-switch` `{on, reason?}` | admin | live | returns effective/db/env; env override can't be cleared |
| POST | `/control/pause-all` `{platform?, confirm:true, reason?}` | approver | live | 202 + `write_request` (one or both platforms); 422 without `confirm`; 409 if the same scope is already queued. Spend-reducing (passes the kill switch) but still needs `execution_enabled` and mode ≠ `read_only`. Audited |
| POST | `/jobs/{name}/run` | admin | live | inserts a `job_requests` row (202 + request); the engine's `job_requests` poller (every minute) runs whitelisted jobs (`manual_jobs` in `/system`, including `execute_approved`, `evaluate`, `reconcile`, `emergency_restore`, `token_reset`) with the normal advisory lock + `job_runs` idempotency. 409 `already_queued`, 404 unknown, 422 CLI-only (`retention`, `backfill`). Jobs that can write do so only through the executor (write gate D-28 + guardrail). Audited `job.run_requested` |
| GET | `/jobs/requests?limit=` · `/jobs/requests/{id}` | admin | live | queue status: `queued → running → success/failed`; `rejected` for names not whitelisted by the engine; running > 6 h → failed |
| GET | `/system` | viewer | live | ad accounts + staleness (>2 h), latest run per job/step, 7-day failures, `heartbeat` (healthy = pacing ok within 30 min and last ping not failed), `platform_operations[]` (today's count published by the engine, limit + access level), `manual_jobs`, `recent_job_requests`, `execution` (write gate D-28), `recent_write_requests` |
| GET / POST | `/control/execution` `{enabled, reason?}` | viewer / admin | live | master write switch (`clients.execution_enabled`, default false; D-28). POST is audited; turning it on does not raise the mode or `HARD_MAX_MODE` |
| GET | `/write-requests?limit=` · `/write-requests/{id}` | viewer | live | queue status for rollback / restore / pause-all (`queued → running → success/failed`) |
| GET | `/metrics` (root, not `/api/v1`) | bearer `METRICS_TOKEN` | live | Prometheus; 404 if token unset; not routed by the proxy |

## Write path (Phase 3 / 5 code, D-27..D-34)

The API never holds a write credential. `POST /changes/{id}/rollback`,
`/emergency-actions/{id}/restore` and `/control/pause-all` return **202** with a
`write_request` body; the engine poller (every minute) performs the write behind the write
gate (`execution_enabled`, mode, `HARD_MAX_MODE`, kill switch — D-28). Dry-run is a GET
computed from DB state; the executor itself still re-reads the platform and sends a
validate-only request before applying. **No live writes** until Govinda signs off the Phase 2
exit criteria, issues write tokens, raises `HARD_MAX_MODE` if needed, and turns
`execution_enabled` on.

## CLI (§19)

`ads login|logout|whoami|status|proposals|approve|reject|dry-run|changes|rollback|pause-all|restore|write-request|manage|mode|kill-switch|execution|run|rankings|llm-usage|alerts|ack|version`,
global `--api-url` (`ADS_API_URL`), `--token-file` (`ADS_TOKEN_FILE`, default
`~/.config/ads-autopilot/credentials.json`, mode 0600), `--json`.
Exit codes: 0 ok · 1 error · 2 usage · 3 auth/forbidden · 4 not enabled (501).
