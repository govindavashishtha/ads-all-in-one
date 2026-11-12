# ads-autopilot

[![ci](https://github.com/govindavashishtha/ads-all-in-one/actions/workflows/ci.yml/badge.svg)](https://github.com/govindavashishtha/ads-all-in-one/actions/workflows/ci.yml)

Self-hosted, open-source automation for **one** Meta Ads account and **one** Google Ads account: ingest on a schedule, rank with confidence-aware scores, enforce caps in code, and propose or apply guarded changes.

Published at [govindavashishtha/ads-all-in-one](https://github.com/govindavashishtha/ads-all-in-one). The plan of record is [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) (v1.1).

Most of the work is deterministic code and SQL (ingestion, features, ranking, YAML rules, guardrails, execution, evaluation). An LLM is called only for triage, analysis, and creative drafts, under a token and cost budget; if it is off or over budget, every pipeline still completes.

## Status

The code for Phases 0–5 is in this repo and is tested against **fake platforms and mocked SDKs**. Nothing is written to Meta or Google until the Phase 2 exit criteria are signed off and the write gate is opened.

Until then:

- Use **read-only** platform credentials. Leave `META_WRITE_TOKEN` and `GOOGLE_WRITE_REFRESH_TOKEN` empty.
- Keep `INITIAL_MODE=read_only` (or `suggest`) and `HARD_MAX_MODE=suggest`.
- `clients.execution_enabled` defaults to **off**. The API cannot raise the mode above `HARD_MAX_MODE`.
- Official API field checks in [docs/VERIFIED.md](docs/VERIFIED.md) (`VERIFY-P0`) are still open. Do not treat live numbers as signed off until those, and a read-only comparison against Ads Manager / Google Ads, are done.

Phase 6 (auth and credentials from a parent system, route-based deploy) is not implemented. There is no license file yet.

## Screenshots

Synthetic demo data only (the sidebar says **SYNTHETIC demo**). These are not live ad accounts.

![Overview: spend versus caps, pacing, and efficiency](docs/images/overview.png)

![Confidence-aware ad rankings within peer groups](docs/images/rankings.png)

![Approvals queue in suggest mode](docs/images/approvals.png)

## What it does

- Pulls entities, history, and metrics from Meta and Google Ads on a schedule, through the official `facebook-business` and `google-ads` (API v26) SDKs. GA4 is an optional conversions cross-check.
- Scores ads, ad sets/ad groups, keywords, and campaigns with Bayesian shrinkage, so a lucky small sample does not outrank a proven one.
- Applies a YAML ruleset to registry variables and emits `noop`, a proposal, or an LLM escalation.
- Runs every proposal through a pure guardrail (`Allow` / `NeedsApproval` / `Block`) before anything can be applied.
- Routes approvals to the dashboard or CLI. Approval links are GET-only; a decision is a CSRF-protected POST (or `ads approve`). Requests expire after 24 hours and are never executed once expired.
- Applies allowed or approved changes through a write-ahead executor: read current state, log intent, validate-only, apply, read back. Idempotency keys and a startup/hourly reconciler cover crashes mid-write.
- Evaluates applied changes at **D+3** (safety), **D+7** (verdict), and **D+14** when D+7 has too little data. Losers can be rolled back; a drift from the logged result needs a new approval.
- Pauses managed campaigns on a cap breach (emergency stop), with pause-all and restore. Unmanaged campaigns are never touched.
- Exposes a single-client Next.js dashboard at `/ads-autopilot`, a FastAPI API, and an `ads` CLI.

It does **not** (v1): support multiple clients in the UI, other ad platforms, autonomous campaign creation, automatic bid or bid-strategy changes, or budget edits on Google shared budgets. Performance Max and Meta Advantage+ are campaign-level pause/budget only. New ads and campaigns are always proposed, and created **PAUSED**.

## Architecture

```mermaid
flowchart TD
    P["Caddy: /ads-autopilot"]
    P --> D["Next.js dashboard"]
    P --> API["FastAPI /api/v1"]
    CLI["CLI: ads"] --> API
    API --> DB[("Postgres 16")]

    S["Engine: APScheduler 3.x + advisory locks"]
    S --> C["Read connectors<br/>facebook-business, google-ads"]
    C --> DB
    CRED["credentials.get_credentials()"] --> C

    DB --> R["Features → Bayesian ranking → YAML rules"]
    R -->|"escalate"| LLM["LLM adapters<br/>token and cost caps, 24h cache, ledger"]
    LLM --> G
    R -->|"proposal"| G{"Guardrail"}
    G -->|"Block"| AL["Alert"]
    G -->|"NeedsApproval"| AP["Approvals, 24h expiry"]
    AP -->|"approved, guardrail re-run"| X
    G -->|"Allow"| X["Write-ahead executor"]
    X --> LOG[("change_log + rollback payload")]
    LOG --> EV["Evaluator D+3 / D+7 / D+14"]
    EV -->|"worse"| RB["Rollback"] --> LOG
    S --> EM["Emergency stop / pause-all"] --> LOG
    API --> DB
```

Runtime config after first start lives in the database (`clients` for mode and kill switch, `config_versions` for rules and limits). `.env` and the YAML seeds are used once by `engine.bootstrap`. `HARD_*` ceilings and `KILL_SWITCH=true` stay env-only and cannot be relaxed from the API.

The API never holds a write credential. Rollback, restore, and pause-all enqueue a `write_request`; the engine poller performs the write behind the write gate.

## Safety model

| Mode | Credentials | What gets applied |
|---|---|---|
| `read_only` | read | Proposals are created, then blocked `mode_read_only`. Emergency stop alerts only. |
| `suggest` | read + write | Everything needs approval. After sign-off and `execution_enabled`, approved changes (and spend-reducing emergency writes) can be applied. |
| `auto_small` | read + write | Guardrail `Allow` with `risk = low` may auto-apply. |
| `auto` | read + write | Guardrail `Allow` with `risk` in `{low, medium}` may auto-apply. |

`risk = high` and anything in `approval_required_for` (new campaign, new ad, enable, add negative, rollback with state drift) needs a human in every mode.

Additional gates:

- **Hard caps** — `HARD_DAILY_CAP` and `HARD_MONTHLY_CAP` are env-only totals. Seed/API limits cannot exceed them. The hard backstop is still the platforms' own account limits, set in Ads Manager / Google Ads.
- **Kill switch** — DB flag or `KILL_SWITCH=true` (the env override cannot be cleared from the API). Only privileged, spend-reducing emergency / pause-all actions may pass.
- **Write gate (D-28)** — closed when `execution_enabled` is false (the default), mode is `read_only`, mode is above `HARD_MAX_MODE`, or the kill switch is on (except those reducing emergency writes).
- **Write-ahead + idempotency** — intent is committed before the platform call; a reconciler finishes rows left `applying`.
- **Approvals** expire at 24 hours (reminder at 12 hours).
- **Evaluation** can roll a change back; if the platform state has drifted, a `rollback_with_state_drift` proposal is opened instead.

## Quickstart

You need Docker and Compose for the full stack. Python 3.12 and [uv](https://docs.astral.sh/uv/) are used for hashes, the CLI, and local tests.

### 1. Configure

```bash
cp .env.example .env
```

Set at least `HARD_DAILY_CAP`, `HARD_MONTHLY_CAP`, `SECRET_KEY` (32+ random bytes), `ADMIN_PASSWORD_HASH` (argon2id), `CLIENT_NAME`, and `TIMEZONE`. Keep `INITIAL_MODE=read_only` and leave write tokens empty. For a first live **read**, also fill the Meta and Google read credentials listed in [Configuration](#configuration). Do not commit `.env`.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# after `make setup`, or any environment with argon2-cffi:
uv run python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('choose-a-password'))"
```

The YAML seeds in `engine/rules/rules.yaml` and `engine/guardrails/limits.yaml` ship with example caps and `inputs_confirmed: false`. Bootstrap will refuse those placeholders unless `APP_ENV=development`. Replace them with real targets and caps, then set `inputs_confirmed: true`, before any non-development deploy.

### 2. Run with Docker Compose

```bash
make up          # builds and starts postgres, engine, api, dashboard, proxy
make logs        # follow logs
make down
```

The engine container runs `alembic upgrade head` and then bootstraps on first start. Postgres is not published to the host; everything goes through Caddy.

Open [http://localhost/ads-autopilot](http://localhost/ads-autopilot) (use `localhost`, not `127.0.0.1` — session cookies are `Secure` and are only stored on `http://localhost` or HTTPS). API health is [http://localhost/ads-autopilot/api/v1/health](http://localhost/ads-autopilot/api/v1/health); interactive docs are under `/ads-autopilot/api/v1/docs`.

On a real domain, set `SITE_ADDRESS` so Caddy can terminate TLS, and change the Compose Postgres password.

### 3. Local development (no Compose)

Postgres 16 must already be listening on `127.0.0.1:5432` with databases `ads_autopilot` and `ads_autopilot_test` (user `ads` is the Makefile default). Point `DATABASE_URL` in `.env` at the host, not the Compose hostname `postgres`.

```bash
make setup
make migrate && make bootstrap
make lint && make test

uv run uvicorn api.main:app --host 127.0.0.1 --port 8000 --root-path /ads-autopilot
uv run python -m engine.main

# dashboard (separate terminal); see dashboard/README.md
cd dashboard && npm ci && npm run dev
# or, UI only with labelled example data:
#   NEXT_PUBLIC_MOCK_API=true npm run dev
```

The dashboard's `NEXT_PUBLIC_API_URL` defaults to `/ads-autopilot/api/v1` (same-origin, behind the proxy). For a production-like single origin, use `make up` or the Caddyfile in `proxy/`.

## Configuration

Full list and comments: [`.env.example`](.env.example). Dashboard build-time values: [`dashboard/.env.example`](dashboard/.env.example). After first start, change mode, rules, and limits through the API; change `HARD_*` only by redeploying.

| Group | Variables | Read-only live run | Later / optional |
|---|---|---|---|
| App | `CLIENT_ID`, `CLIENT_NAME`, `TIMEZONE`, `INITIAL_MODE`, `APP_ENV` | Required. Keep `INITIAL_MODE=read_only`. | |
| Hard ceilings | `HARD_DAILY_CAP`, `HARD_MONTHLY_CAP`, `HARD_MAX_MODE` | Caps are required (bootstrap stops without them). Keep `HARD_MAX_MODE=suggest`. | Raise `HARD_MAX_MODE` only at the Phase 3 gate. |
| Auth | `ADMIN_USER`, `ADMIN_PASSWORD_HASH`, `SECRET_KEY`, `SESSION_TTL_MINUTES` | Required to log in (`AUTH_MODE=env`). | `AUTH_MODE=main_system` is Phase 6 (`501` today). |
| Meta | `META_APP_ID`, `META_APP_SECRET`, `META_READ_TOKEN`, `META_AD_ACCOUNT_ID`, `META_API_VERSION` | Required for live Meta reads (`ads_read` system-user token). | `META_WRITE_TOKEN` stays empty until Phase 3. |
| Google Ads | `GOOGLE_ADS_CUSTOMER_ID`, `GOOGLE_ADS_LOGIN_CUSTOMER_ID`, `GOOGLE_CLOUD_PROJECT_ID`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_READ_REFRESH_TOKEN` | Required for live Google reads. Set the login customer id only if access is through a manager account. | `GOOGLE_WRITE_REFRESH_TOKEN` stays empty until Phase 5. |
| LLM | `LLM_PROVIDER`, `LLM_API_KEY`, `LLM_SMALL_MODEL`, `LLM_LARGE_MODEL`, `LLM_*_PRICE_PER_MTOK`, daily/monthly caps | Optional. If unset or incomplete, the layer stays off and escalations become `flag_human` alerts. Default caps are 100k tokens/day, $1/day, $20/month. | Providers: `anthropic`, `openai` (or an OpenAI-compatible base URL), or `fake` in development. |
| Database | `DATABASE_URL` | Required. Compose default is `postgresql+psycopg://ads:ads@postgres:5432/ads_autopilot`. | |
| Notifications | `SLACK_WEBHOOK_URL`, `SMTP_*`, `ALERT_EMAIL_TO`, `WHATSAPP_*` | Optional. | |
| Observability | `HEARTBEAT_URL`, `METRICS_TOKEN`, `SENTRY_DSN` | Optional. Prometheus `/metrics` is on the API container only, not through the proxy. | |
| Frontend (build-time) | `NEXT_PUBLIC_BASE_PATH`, `NEXT_PUBLIC_API_URL` | Defaults (`/ads-autopilot`, `/ads-autopilot/api/v1`) match the Caddyfile. Changing them requires a dashboard rebuild. | |
| Phase 6 | `MAIN_SYSTEM_JWKS_URL`, `MAIN_BACKEND_URL`, `MAIN_BACKEND_SERVICE_TOKEN`, `CREDENTIALS_PROVIDER=remote` | Leave empty. | |
| GA4 | `GA4_PROPERTY_ID`, `GA4_SERVICE_ACCOUNT_JSON_PATH` | Optional. | |

Platform credentials are read only through `engine.credentials.get_credentials`. Never log them or paste them into issues.

## Tests

```bash
make setup
make lint                # ruff + mypy --strict
make test-unit           # no database
make test                # needs TEST_DATABASE_URL (default: local ads_autopilot_test)

cd dashboard
npm ci
npm run lint && npm run typecheck && npm test    # vitest
```

`make test` uses a disposable Postgres database (downgrade to base, then upgrade) and includes unit, property, contract, API, and integration tests.

An optional Playwright smoke against a **running** real-API stack is `npm run smoke:real` in `dashboard/` (see [`dashboard/README.md`](dashboard/README.md)). It is not part of CI.

CI (`.github/workflows/ci.yml`) runs `make setup`, `make lint`, Alembic upgrade / downgrade / upgrade / `check`, `make test`, `docker compose config`, and Caddyfile validation. It does not run the dashboard tests.

A rules-and-guardrail backtest over ingested history:

```bash
uv run python -m engine.backtest --out report.md
```

A fake-platform walk through approve → execute → evaluate → rollback is documented in [docs/EXEC_DEMO.md](docs/EXEC_DEMO.md).

## CLI and API

The `ads` CLI talks only to the API, never to the database.

```bash
uv run ads --api-url http://127.0.0.1:8000/api/v1 login
uv run ads status
uv run ads --help
```

Commands include `proposals`, `approve`, `reject`, `dry-run`, `changes`, `rollback`, `pause-all`, `restore`, `mode`, `kill-switch`, `execution`, `run`, `rankings`, `llm-usage`, and `alerts`. Token file default: `~/.config/ads-autopilot/credentials.json` (mode 0600). Exit `4` means the route is not enabled in this phase (`HTTP 501`).

API contract: [docs/API.md](docs/API.md) and [docs/openapi.json](docs/openapi.json). Roles are `viewer` < `approver` < `admin`.

## Repository layout

```
ads-autopilot/
├── engine/          # scheduler, connectors, ingest, features, ranking, rules,
│                    # guardrails, executor, evaluation, LLM, jobs
├── api/             # FastAPI app and auth adapter
├── cli/             # Typer app `ads`
├── dashboard/       # Next.js 16 App Router UI
├── proxy/Caddyfile  # /ads-autopilot/api/* → API; /ads-autopilot/* → dashboard
├── db/              # Alembic migrations
├── tests/           # unit, property, contract, api, integration
├── docs/            # plan, architecture, API, decisions, verification
├── docker-compose.yml
└── Makefile
```

Pinned SDK and runtime versions: [docs/VERSIONS.md](docs/VERSIONS.md).

## Roadmap

Build durations in the plan assume one developer. A phase's **gate**, not the build, is what turns live writes on.

| Phase | In the plan | In this repo |
|---|---|---|
| 0 Setup | Compose, migrations, credentials, native platform backstops, `VERIFY-P0` | Code and CI are here. Official doc checks, live read verification, and several inputs from the operator are still open. |
| 1 Read-only ingestion + dashboard | Entities, metrics, backfill, Overview / Campaigns / System, CLI `status` | Implemented; live 7-day accuracy soak has not been run. |
| 2 Scoring + suggest | Features, ranking, rules, guardrail, approvals, digest, backtest | Implemented against synthetic data. Sign-off is required before any write token is installed. |
| 3 Guarded Meta writes | Executor, reconciler, rollback, evaluator, emergency stop, `auto_small` | Code complete against fakes. Live writes stay off. |
| 4 LLM layer | Triage, analyst, token controller, creative drafts, AI usage | Adapters, budget controller, ledger, triage, and creative drafts are implemented. Daily digest narrative and search-term relevance batches are not wired yet. |
| 5 Google writes | Write connector, negatives, RSA drafts | Code complete against fakes. Live writes stay off. |
| 6 Main-system integration | Swap auth, credentials provider, and the frontend route | Not started. |

## Docs

| Doc | What it is |
|---|---|
| [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) | v1.1 plan (source of truth) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the plan maps onto packages |
| [docs/API.md](docs/API.md) | HTTP API and CLI |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decisions D-1… and D-27…D-34 (write gate) |
| [docs/VERIFIED.md](docs/VERIFIED.md) | `VERIFY-P0` checklist (still open) |
| [docs/VERSIONS.md](docs/VERSIONS.md) | Pinned SDKs and API versions |
| [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) | Rules for contributors and coding agents |
| [docs/EXEC_DEMO.md](docs/EXEC_DEMO.md) | Fake-platform executor walk-through |
| [docs/LOCAL_STACK.md](docs/LOCAL_STACK.md) | Synthetic UI-demo stack used in QA |
| [docs/UI_QA.md](docs/UI_QA.md) | Dashboard QA log |
| [engine/llm/README.md](engine/llm/README.md) | LLM adapters and budget controller |
| [dashboard/README.md](dashboard/README.md) | Dashboard dev, mock mode, smoke tests |
| [cli/README.md](cli/README.md) | CLI notes |

## Contributing

Read [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) and plan §0 first. Do not invent targets, caps, account ids, conversion events, or model names. Do not send live writes to Meta or Google.

`make lint && make test` must stay green. Schema changes go through a new Alembic revision, never an edit of `0001`.

## License

No license file is included yet. All use is at your own risk until one is published.
