# ads-autopilot — Build Plan

Version 1.1 · 11 Nov 2026 · Owner: Govinda Vashishtha
Supersedes v1.0. Section 27 lists every change from v1.0.
Scope: one client, one Meta Ads account + one Google Ads account, standalone repo, later merged into the main system.

---

## 0. Rules for the developer agent

These rules apply to every phase. If any rule conflicts with a later section, this section wins.

1. **Do not guess.** If a value in §25 (Inputs required) is missing when a phase needs it, stop and ask. Never invent a target CPA, cap, account id, conversion event or model name.
2. **`VERIFY-P0` markers.** Any statement tagged `VERIFY-P0` names an external API field, parameter or behaviour that must be confirmed against the official documentation during Phase 0. Record the result (doc URL, date checked, exact field name, SDK version) in `docs/VERIFIED.md`. If the documentation disagrees with this plan, the documentation wins: update the connector only, record the difference in `docs/VERIFIED.md`, and do not change rules, ranking or guardrail code.
3. **Only the executor writes to ad platforms.** No other module may import a `WriteConnector`. Enforce with an import-lint test (`tests/unit/test_import_boundaries.py`).
4. **Only the connectors convert money and ids.** Everything outside `engine/connectors` uses `Decimal` in major currency units (e.g. INR, not paise or micros) and internal integer ids.
5. **No live writes before the Phase 3 gate.** Until Govinda signs off the Phase 2 exit criteria, the process must run with read-only credentials and `mode=read_only` or `suggest`.
6. **Database is the runtime source of truth** for mode, kill switch, rules and limits. `.env` and YAML files only seed the database on first start (§7).
7. **Every job step is idempotent.** Re-running any job for the same window must produce the same rows (upserts and unique keys, never duplicates).
8. **All probabilities that use sampling use a fixed seed** (given per formula) so tests are reproducible.
9. **Time.** Store timestamps as UTC `TIMESTAMPTZ`. A metric `date` or `ts_hour` is always in the **ad account's** timezone (`ad_accounts.timezone`), never the server's or the client's.
10. **Definition of done for any task:** code + unit tests + the acceptance checks listed for that phase in §23 pass in CI.

---

## 1. Summary

ads-autopilot is a self-hosted service that pulls Meta Ads and Google Ads data on a schedule, ranks every ad / ad set / keyword with confidence-aware scores, enforces budget limits in code, applies small changes automatically, routes big changes to a human, and evaluates every change 3 and 7 days later (rolling back the losers).

Core design rule: **deterministic code and SQL do ~90% of the work** (ingestion, features, ranking, rules, guardrails, execution, evaluation). The LLM is called only for (a) ambiguous/anomalous cases, (b) rare root-cause analysis, (c) ad copy generation, and (d) plain-language explanations. Token spend scales with the number of problems, not the number of ads.

---

## 2. Goals and non-goals

### Goals
1. **Budget protection.** Detect a daily or monthly cap breach within one pacing cycle (15 minutes plus the platform's reporting lag) and pause managed campaigns. Polling alone cannot guarantee a hard stop, so the **hard backstop is the platforms' own limits** (Meta account spending limit, Google account budget), set manually at Phase 0 (§12.5).
2. Rank all ads, ad sets/ad groups and keywords daily with a score that respects sample size.
3. Shift budget toward winners and pause clear losers automatically, within hard guardrails.
4. Every optimisation change is dry-run first, logged with a rollback snapshot, and evaluated against a baseline. Exception: emergency stop and `pause-all` skip the dry-run (they are still validated locally and logged).
5. Single-client dashboard with login, showing spend vs budget, ROAS/CPA trends, rankings, change feed and approvals.
6. LLM cost is capped per account per day and per month, and visible on the dashboard.
7. Built so that merging into the main system later only swaps: auth source, credential source, and the frontend route.

### Non-goals (v1)
- Multi-client / multi-tenant UI, billing, or signup (schema is ready via `client_id`, UI is not).
- TikTok, LinkedIn, or other platforms.
- Autonomous campaign creation. New campaigns and new ads are always proposed, never auto-launched.
- Bid-strategy modelling. We steer budgets, statuses, negative keywords and creatives; platforms keep doing auction bidding. We never change bids or bid strategies automatically.
- Budget changes on Google **shared** budgets, and any ad-set/asset-level action inside Performance Max or Meta Advantage+ campaigns (campaign-level pause and budget only, §8.2).
- Real-time (sub-15-minute) reaction.
- Custom attribution modelling. v1 trusts platform-reported conversions as defined in §8.4.

---

## 3. Architecture

```mermaid
flowchart TD
    subgraph SCHED["Scheduler (APScheduler 3.x, no AI)"]
        J15["every 15 min: pacing"]
        J60["hourly :05: metrics pull"]
        JD["daily 06:00: daily pipeline"]
        JW["weekly Mon 07:00: strategy + creative"]
    end

    subgraph CONN["Connectors (engine/connectors)"]
        MM["Meta: facebook-business SDK"]
        GM["Google: google-ads SDK"]
    end

    CRED["Credentials provider: get_credentials()"]
    CRED --> CONN

    SCHED --> CONN --> DB[("Postgres")]
    DB --> FEAT["Feature builder (SQL views + Python)"]
    FEAT --> RANK["Ranking engine (Bayesian shrinkage)"]
    RANK --> RULES{"Rules engine (YAML)"}
    RULES -->|"clear-cut"| PROP["Proposal"]
    RULES -->|"within bands"| NOOP["log only, 0 tokens"]
    RULES -->|"escalate"| TRI["LLM triage, small model"]
    TRI -->|"routine"| PROP
    TRI -->|"needs reasoning"| ANA["LLM analyst, large model"]
    ANA --> PROP
    TOK["Token budget controller"] -.-> TRI
    TOK -.-> ANA
    PROP --> GUARD{"Guardrail (pure code)"}
    GUARD -->|"Block"| BLOCK["blocked + alert"]
    GUARD -->|"Allow"| EXEC["Executor: validate-only, then apply"]
    GUARD -->|"NeedsApproval"| APPR["Human approval"]
    APPR -->|"approved, guardrail re-run"| EXEC
    EXEC --> LOG[("change_log + rollback payload")]
    LOG --> EVAL["Evaluator (D+3 safety, D+7 final)"]
    EVAL -->|"worse"| RB["rollback"] --> LOG
    EVAL -->|"stats"| LEARN["rule win/loss stats -> threshold proposals"]
    J15 --> EMERG["Emergency stop (pause managed campaigns)"] --> LOG
    DB --> API["FastAPI + auth adapter"] --> DASH["Dashboard (Next.js)"]
    API --> CLI["CLI: ads ..."]
```

### Component responsibilities

| Component | Responsibility | AI? |
|---|---|---|
| Scheduler | Fires jobs at fixed cadences. One run at a time per job (Postgres advisory lock + `max_instances=1`). | No |
| Credentials provider | Single `get_credentials(client_id, platform, access)` function, `access ∈ {read, write}`. Reads `.env` now; calls main backend later. Nothing else reads secrets. | No |
| Connectors | Typed wrappers around the official SDKs. Normalise Meta + Google into one entity model (§8.2). `ReadConnector` and `WriteConnector` are separate classes. Convert money and ids. | No |
| Warehouse (Postgres) | Source of truth: entities, entity history, metrics, features, scores, proposals, approvals, change log, evaluations, config versions, LLM ledger. | No |
| Feature builder | Computes the variables in the registry (§11.1). | No |
| Ranking engine | Posterior estimates, intervals, `p_beats_target`, rank within peer group (§10). | No |
| Rules engine | Applies YAML rules to registry variables. Outputs `noop`, `proposal`, or `escalate`. | No |
| LLM layer | Triage (small model) and analyst/creative (large model). Receives compact JSON only. Output is only ever a proposal. | Yes |
| Token budget controller | Daily token cap, daily and monthly cost caps, caching, batching, hard stop. | No |
| Guardrail | Final gate on every proposal (§12). Pure functions. | No |
| Executor | Read state → write-ahead log → validate-only → apply → read-back verify (§15). | No |
| Approval service | Creates approval requests, notifies, accepts decisions from dashboard/CLI, expires stale ones. | No |
| Evaluator | D+3 safety check and D+7 final verdict; rollback or confirm; updates rule stats. | No |
| API | REST for dashboard + CLI, behind the auth adapter. | No |
| Dashboard | Single-client UI under a configurable base path. | No |
| CLI | Operator control. Calls the API, never the DB. | No |

---

## 4. External dependencies

### 4.1 Runtime connectors (decision changed from v1.0)

The engine uses the **official SDKs directly**. MCP servers are not used at runtime: they are designed for LLM tool calls, add a subprocess hop, return loosely typed output, and put third-party code in possession of write tokens.

| Platform | Library | Used for |
|---|---|---|
| Meta | `facebook-business` (official Python SDK for the Marketing API) | reads (entities, insights), writes (status, budget, ad creation as PAUSED) |
| Google Ads | `google-ads` (official Python client) | reads via GAQL, writes via mutate operations |
| GA4 (optional) | `google-analytics-data` | daily conversions cross-check only |

Pin exact versions and the Meta `META_API_VERSION` / Google Ads API version at Phase 0 in `docs/VERSIONS.md`.

### 4.2 Reference-only repositories (not runtime dependencies)

Read at Phase 0 for patterns; re-check licence and README before copying any text.

| Repo | Used as |
|---|---|
| mikusnuz/meta-ads-mcp | Reference for Meta endpoint coverage. |
| kLOsk/adloop | Reference for the draft → preview → confirm write pattern on Google. |
| googleads/google-ads-mcp | Reference GAQL queries. |
| logly/mureo | Source text for LLM playbook prompts (budget rebalance, creative refresh, search-term clean-up). |
| attainmentlabs/meta-ads-mcp | Reference safety model (dry-run, caps, audit log, create-paused). |

### 4.3 Platform access (Phase 0 critical path — start on day 1)

**Google Ads API**
- Developer tokens were retired on 9 Sept 2026. Access levels (Test, Explorer, Basic, Standard) now belong to the **Google Cloud project**. There is no `GOOGLE_ADS_DEVELOPER_TOKEN`.
- A new project has Test access (test accounts only). Explorer access reaches production accounts but is limited to 2,880 operations/day. Basic allows 15,000/day.
- Action: enable the Google Ads API on the project and **apply for Basic access on day 1**. Develop against a test account until approved. `VERIFY-P0`: current limits and application steps at https://developers.google.com/google-ads/api/docs/api-policy/access-levels.
- Operation budget (must stay under 2,000/day so Explorer is enough as a fallback): pacing 96 calls, hourly pull ≤ 24 × 6 = 144 calls, daily pipeline ≤ 60 calls, search terms ≤ 10 calls, writes ≤ 50. Connector counts operations per day in a Prometheus counter and alerts at 80% of the access level's limit.

**Meta Marketing API**
- Use a **system user token** created in the Business Manager that owns the ad account, with `ads_read` (read credential) and `ads_read` + `ads_management` (write credential), scoped to the one ad account.
- If the app and the ad account belong to different businesses, the app needs advanced access (app review). `VERIFY-P0`: confirm which case applies and record it. If app review is needed, start it on day 1.
- Do not use a 60-day long-lived user token.

---

## 5. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Engine | Python 3.12 | |
| Scheduling | APScheduler 3.x (in-process, `BackgroundScheduler`) + Postgres advisory locks | Job defaults: `coalesce=True`, `max_instances=1`. `misfire_grace_time`: 300 s for pacing and approval_expiry, 3600 s for all others. |
| Data | Postgres 16 | JSONB for raw payloads and rollback snapshots. |
| Migrations | Alembic | |
| ORM / queries | SQLAlchemy 2 + raw SQL views for features | |
| Stats | numpy, scipy | Beta and Gamma posteriors. |
| Rule expressions | `simpleeval` | Never `eval`. Only registry names (§11.1) and the functions `min`, `max`, `abs` are allowed. |
| API | FastAPI + Pydantic v2 | |
| Dashboard | Next.js (App Router) + TypeScript + Recharts + Tailwind | `basePath` and API URL are **build-time** values (§18). |
| CLI | Typer | Shares Pydantic models with the API. |
| LLM client | Provider-agnostic wrapper (`engine/llm/client.py`) | Model names from env. |
| Notifications | Slack webhook / email (SMTP) / WhatsApp (provider TBD) | Behind one `notify()` interface. |
| Reverse proxy | Caddy | In docker-compose; terminates TLS and routes paths (§18). |
| Packaging | Docker + docker-compose | Services: `postgres`, `engine`, `api`, `dashboard`, `proxy`. |
| Observability | structlog JSON logs, Prometheus metrics, optional Sentry, external heartbeat | |
| Tests | pytest, hypothesis, Playwright | |

---

## 6. Repo structure

```
ads-autopilot/
├── engine/
│   ├── main.py                  # starts scheduler, runs startup reconciler (§15)
│   ├── config.py                # pydantic-settings, reads env
│   ├── bootstrap.py             # seeds DB from env + YAML on first start (§7)
│   ├── credentials/
│   │   ├── base.py              # CredentialsProvider interface
│   │   ├── env_provider.py      # now
│   │   └── remote_provider.py   # Phase 6
│   ├── connectors/
│   │   ├── base.py              # ReadConnector / WriteConnector + normalised models
│   │   ├── money.py             # the only place that converts minor units / micros
│   │   ├── meta_read.py
│   │   ├── meta_write.py
│   │   ├── google_read.py
│   │   ├── google_write.py
│   │   └── ga4.py               # optional
│   ├── ingest/                  # pull + upsert entities, history, metrics
│   ├── features/                # SQL views + python builders, registry.py
│   ├── ranking/                 # posteriors.py, rank.py, allocation.py
│   ├── rules/
│   │   ├── engine.py
│   │   ├── registry.py          # variable registry (§11.1)
│   │   └── rules.yaml           # seed for config_versions(kind='rules')
│   ├── guardrails/
│   │   ├── check.py             # pure functions
│   │   └── limits.yaml          # seed for config_versions(kind='limits')
│   ├── pacing/                  # curve.py, emergency.py
│   ├── executor/                # apply.py, reconcile.py, rollback.py
│   ├── approvals/
│   ├── evaluation/
│   ├── llm/
│   │   ├── client.py
│   │   ├── budget.py
│   │   ├── summariser.py
│   │   └── prompts/             # triage.md, analyst.md, creative.md
│   ├── notify/
│   └── jobs/                    # pacing.py, hourly.py, daily.py, weekly.py, ...
├── api/
│   ├── main.py
│   ├── auth/
│   │   ├── adapter.py           # get_current_user() — single entry point, returns user + role
│   │   ├── env_auth.py          # env admin -> server-side session
│   │   └── main_system_auth.py  # Phase 6
│   └── routes/
├── cli/
│   └── ads.py
├── dashboard/
├── proxy/
│   └── Caddyfile
├── db/
│   ├── alembic.ini
│   └── migrations/
├── tests/
│   ├── unit/  property/  contract/  integration/  fixtures/
├── docs/
│   ├── BUILD_PLAN.md
│   ├── VERIFIED.md              # VERIFY-P0 results
│   └── VERSIONS.md              # pinned SDK and API versions
├── .env.example
├── docker-compose.yml
└── Makefile
```

---

## 7. Configuration

### 7.1 Source-of-truth rules

| Setting | Seed (first start only) | Runtime source | Changed by |
|---|---|---|---|
| mode | `INITIAL_MODE` | `clients.mode` | API `/control/mode` |
| kill switch | — | `clients.kill_switch` **OR** env `KILL_SWITCH=true` (env is a hard override that the API cannot clear) | API `/control/kill-switch`, or ops editing env |
| rules | `engine/rules/rules.yaml` | active row in `config_versions(kind='rules')` | API `PUT /rules` |
| limits | `engine/guardrails/limits.yaml` | active row in `config_versions(kind='limits')` | API `PUT /limits` |
| hard ceilings | env only | env only (`HARD_*`) | redeploy only |

`engine/bootstrap.py` runs at start: if `clients` has no row for `CLIENT_ID`, insert it and insert version 1 of rules and limits from the YAML files. If rows exist, do nothing.

`PUT /limits` is rejected if any cap exceeds a `HARD_*` ceiling. `/control/mode` is rejected if the requested mode is above `HARD_MAX_MODE` (order: `read_only < suggest < auto_small < auto`).

### 7.2 `.env.example`

```dotenv
# --- App ---
APP_ENV=development                 # development | staging | production
CLIENT_ID=client_001
CLIENT_NAME="Client Name"
TIMEZONE=Asia/Kolkata               # client timezone, used for job schedules and the digest
INITIAL_MODE=read_only              # seed only: read_only | suggest | auto_small | auto
KILL_SWITCH=false                   # true = hard override, API cannot clear it

# --- Hard ceilings (cannot be exceeded from the API) ---
HARD_DAILY_CAP=                     # major currency units, total across platforms
HARD_MONTHLY_CAP=
HARD_MAX_MODE=suggest               # raise to auto_small at the Phase 3 gate

# --- Auth (standalone) ---
AUTH_MODE=env                       # env | main_system
ADMIN_USER=admin
ADMIN_PASSWORD_HASH=                # argon2id hash, never plain text
SECRET_KEY=                         # 32+ random bytes; signs CSRF and approval-link tokens
SESSION_TTL_MINUTES=720
MAIN_SYSTEM_JWKS_URL=               # Phase 6

# --- Credentials source ---
CREDENTIALS_PROVIDER=env            # env | remote
MAIN_BACKEND_URL=                   # Phase 6
MAIN_BACKEND_SERVICE_TOKEN=         # Phase 6

# --- Meta ---
META_APP_ID=
META_APP_SECRET=
META_READ_TOKEN=                    # system user, ads_read
META_WRITE_TOKEN=                   # system user, ads_read + ads_management (empty until Phase 3)
META_AD_ACCOUNT_ID=act_XXXXXXXXXX
META_API_VERSION=                   # pin explicitly

# --- Google Ads ---
GOOGLE_ADS_CUSTOMER_ID=XXXXXXXXXX
GOOGLE_ADS_LOGIN_CUSTOMER_ID=       # manager account id, only if access is via a manager
GOOGLE_CLOUD_PROJECT_ID=            # the project that holds the Ads API access level
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
GOOGLE_READ_REFRESH_TOKEN=          # user with read-only access to the account
GOOGLE_WRITE_REFRESH_TOKEN=         # user with standard access (empty until Phase 5)

# --- GA4 (optional) ---
GA4_PROPERTY_ID=
GA4_SERVICE_ACCOUNT_JSON_PATH=

# --- LLM ---
LLM_PROVIDER=
LLM_API_KEY=
LLM_SMALL_MODEL=
LLM_LARGE_MODEL=
LLM_SMALL_IN_PRICE_PER_MTOK=
LLM_SMALL_OUT_PRICE_PER_MTOK=
LLM_LARGE_IN_PRICE_PER_MTOK=
LLM_LARGE_OUT_PRICE_PER_MTOK=
LLM_DAILY_TOKEN_CAP=100000
LLM_DAILY_COST_CAP_USD=1.00
LLM_MONTHLY_COST_CAP_USD=20.00

# --- Database ---
DATABASE_URL=postgresql+psycopg://ads:ads@postgres:5432/ads_autopilot

# --- Notifications ---
SLACK_WEBHOOK_URL=
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
ALERT_EMAIL_TO=
WHATSAPP_PROVIDER=
WHATSAPP_API_KEY=
WHATSAPP_TO=

# --- Observability ---
HEARTBEAT_URL=                      # external monitor pinged by the pacing job
METRICS_TOKEN=                      # bearer token for /metrics
SENTRY_DSN=

# --- Frontend (build-time: changing these requires rebuilding the dashboard image) ---
NEXT_PUBLIC_BASE_PATH=/ads-autopilot
NEXT_PUBLIC_API_URL=/ads-autopilot/api/v1
```

---

## 8. Data model

### 8.1 Conventions

- Every table has `client_id TEXT NOT NULL` and `created_at TIMESTAMPTZ DEFAULT now()`.
- `id` is `BIGINT GENERATED ALWAYS AS IDENTITY`. Platform ids are `external_id TEXT`. `platform` is an enum (`meta`, `google`).
- Every entity table has `UNIQUE (client_id, platform, external_id)` (for `keywords`: `UNIQUE (client_id, ad_group_id, external_id)`).
- Money is `NUMERIC(14,2)` in the **ad account's currency, major units**. Conversion happens only in `engine/connectors/money.py`:
  - Meta budget fields are integers in the currency's minor unit; Meta insights `spend` is a decimal string in major units. `VERIFY-P0` for the account currency.
  - Google amounts are micros (÷ 1,000,000). `VERIFY-P0`.
  - A fixture test asserts a known budget and a known day's spend round-trip correctly for each platform.
- Both ad accounts must use the same currency in v1. Bootstrap fails with a clear error if they differ.
- `entity_type` enum: `account`, `campaign`, `ad_group`, `ad`, `keyword`, `search_term`. In metrics/features/scores, `entity_id` is the internal `id` of the row in the table named by `entity_type`.

### 8.2 Entity model and budget ownership

| Normalised | Meta | Google |
|---|---|---|
| `campaign` | campaign | campaign |
| `ad_group` (`kind=ad_set`) | ad set | — |
| `ad_group` (`kind=ad_group`) | — | ad group |
| `ad` | ad | ad (RSA etc.) |
| `keyword` | — | ad group criterion (keyword) |

**Budget unit** = the entity whose budget the engine may change:

| `campaigns.budget_level` | Meaning | Budget unit |
|---|---|---|
| `campaign` | Meta campaign-level budget (Advantage campaign budget / CBO), or a Google campaign with a non-shared budget | the campaign |
| `ad_set` | Meta campaign whose ad sets hold budgets | each ad set |
| `shared` | Google campaign on a shared budget | none — budget changes are blocked in v1 |

`campaigns.kind` ∈ `standard`, `pmax`, `advantage_plus`, `other`. For `pmax`, `advantage_plus` and `other`, `campaigns.automation_scope = campaign_only`: the engine ranks and may pause or re-budget the **campaign** only, and never touches its children. For `standard`, `automation_scope = full`.

`learning` (boolean, per ad_group and per campaign):
- Meta ad set: `learning = true` when the ad set's learning stage status is `LEARNING`. "Learning limited" (`FAIL`) and `SUCCESS` both count as `learning = false`. `VERIFY-P0`: field `learning_stage_info.status` and its values.
- Google campaign: `learning = true` while the bidding strategy system status is a learning value. `VERIFY-P0`: field `campaign.bidding_strategy_system_status` and which enum values mean learning.
- If the field cannot be read, set `learning = NULL`; the guardrail treats `NULL` as `true` (protected).

### 8.3 Tables

| Table | Key columns |
|---|---|
| `clients` | `client_id PK`, `name`, `timezone`, `currency`, `mode`, `kill_switch BOOL` |
| `ad_accounts` | `id PK`, `platform`, `external_id`, `name`, `status`, `currency`, `timezone` (IANA, read from the platform), `last_synced_at` |
| `campaigns` | `id PK`, `ad_account_id FK`, `platform`, `external_id`, `name`, `objective`, `kind`, `automation_scope`, `status`, `budget_level`, `daily_budget NULL`, `lifetime_budget NULL`, `shared_budget_external_id NULL`, `bid_strategy`, `learning BOOL NULL`, `managed BOOL DEFAULT false`, `pool TEXT NULL` (allocation pool label, §10.6), `raw JSONB` |
| `ad_groups` | `id PK`, `campaign_id FK`, `platform`, `external_id`, `kind` (`ad_set`/`ad_group`), `name`, `status`, `daily_budget NULL`, `optimization_goal`, `learning BOOL NULL`, `learning_status_at`, `raw` |
| `ads` | `id PK`, `ad_group_id FK`, `platform`, `external_id`, `name`, `status`, `format`, `creative_id`, `headline`, `body`, `cta`, `first_live_at`, `raw` |
| `keywords` | `id PK`, `ad_group_id FK`, `platform` (always `google`), `external_id`, `text`, `match_type`, `status`, `is_negative BOOL` |
| `search_terms` | `id PK`, `ad_group_id FK`, `term`, `date`, `impressions`, `clicks`, `cost`, `conversions`, `conv_value`, `UNIQUE (client_id, ad_group_id, term, date)` |
| `entity_history` | `id PK`, `entity_type`, `entity_id`, `observed_at`, `status`, `daily_budget NULL`, `learning NULL`, `source` (`hourly_pull`/`executor`/`emergency`). One row is appended **only when a tracked field changed** since the previous row. Used for start-of-day budget, manual-change detection, baselines and backtests. |
| `metrics_hourly` | `PK (client_id, platform, entity_type, entity_id, ts_hour)`, `impressions`, `clicks`, `spend`, `conversions NUMERIC(14,4)`, `conv_value`, `pulled_at`. Only `account` and `campaign` level. No reach/frequency (not additive). |
| `metrics_daily` | `PK (client_id, platform, entity_type, entity_id, date)`, `impressions`, `clicks`, `spend`, `conversions NUMERIC(14,4)`, `conv_value`, `video_views`, `pulled_at`, `is_final BOOL` (true once `date` is more than 30 days old) |
| `metrics_window` | `PK (client_id, platform, entity_type, entity_id, window_days, as_of)`, `reach`, `frequency`. Meta only, `window_days = 7`, pulled as one windowed query per day (§9). |
| `features` | `PK (client_id, entity_type, entity_id, as_of)` + one column per feature variable in §11.1 |
| `scores` | `PK (client_id, entity_type, entity_id, as_of)`, `ctr_post_mean`, `ctr_p10`, `ctr_p90`, `theta_a`, `theta_b`, `cpa_shrunk`, `cpa_p10`, `cpa_p90`, `aov_shrunk`, `roas_shrunk`, `roas_p10`, `roas_p90`, `p_beats_target`, `rank_metric`, `rank_in_peer`, `peer_size`, `peer_group`, `composite`, `data_sufficient BOOL` |
| `config_versions` | `id PK`, `kind` (`rules`/`limits`), `version INT`, `body JSONB`, `active BOOL`, `changed_by`, `reason`, `UNIQUE (client_id, kind, version)`; a partial unique index allows one active row per `(client_id, kind)` |
| `proposals` | `id PK`, `dedupe_key TEXT UNIQUE`, `as_of DATE`, `source` (`rule`/`llm_triage`/`llm_analyst`/`human`/`allocation`/`evaluator`/`emergency`), `rule_key`, `entity_type`, `entity_id`, `action`, `params JSONB`, `expected_impact JSONB`, `reason TEXT`, `risk` (`low`/`medium`/`high`), `spend_direction` (`reducing`/`increasing`/`neutral`), `status` (`pending`,`blocked`,`awaiting_approval`,`approved`,`rejected`,`executing`,`executed`,`expired`,`failed`), `guardrail_result JSONB`, `expires_at` |
| `approvals` | `id PK`, `proposal_id FK`, `channel`, `requested_at`, `reminded_at`, `decided_at`, `decision`, `decided_by`, `comment` |
| `change_log` | `id PK`, `proposal_id FK`, `platform`, `entity_type`, `entity_id`, `action`, `status` (`applying`,`applied`,`verify_failed`,`failed`,`rolled_back`), `before JSONB`, `intended JSONB`, `after JSONB NULL`, `rollback_payload JSONB`, `validate_result JSONB`, `started_at`, `applied_at NULL`, `applied_by`, `rollback_of FK NULL` |
| `emergency_actions` | `id PK`, `platform`, `trigger` (`daily_cap`/`monthly_cap`/`pause_all`), `campaign_ids BIGINT[]`, `triggered_at`, `restore_policy` (`auto_next_day`/`manual`), `restored_at NULL`, `restored_by NULL` |
| `evaluations` | `id PK`, `change_id FK`, `horizon_days` (3/7/14), `due_at`, `done_at NULL`, `unit_entity_type`, `unit_entity_id`, `baseline JSONB`, `observed JSONB`, `p_worse`, `p_better`, `verdict` (`better`,`neutral`,`worse`,`insufficient_data`) `NULL`, `action_taken` |
| `rule_stats` | `PK (client_id, rule_key)`, `n_changes`, `n_better`, `n_neutral`, `n_worse`, `updated_at` |
| `llm_calls` | `id PK`, `job`, `tier`, `model`, `purpose`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `cost_usd NUMERIC(10,5)`, `latency_ms`, `prompt_hash`, `result_ref`, `status` |
| `alerts` | `id PK`, `dedupe_key`, `severity`, `kind`, `entity_type`, `entity_id`, `message`, `sent_channels TEXT[]`, `last_sent_at`, `acked_at`, `acked_by`; partial unique index on `(client_id, dedupe_key) WHERE acked_at IS NULL` |
| `sessions` | `id PK`, `token_hash UNIQUE`, `kind` (`browser`/`cli`), `user`, `role`, `expires_at`, `revoked_at NULL` |
| `audit_log` | `id PK`, `actor`, `action`, `target`, `payload JSONB`, `ip`, `at`. Append-only: the app DB role has no UPDATE/DELETE on this table. |
| `job_runs` | `id PK`, `job`, `step NULL`, `window_key`, `started_at`, `finished_at`, `status`, `rows_ingested`, `error`, `UNIQUE (client_id, job, step, window_key)` |

`proposals.dedupe_key = sha256(client_id | entity_type | entity_id | action | rule_key-or-source | as_of)`. Insert with `ON CONFLICT (dedupe_key) DO NOTHING`.

Indexes: `(client_id, entity_type, entity_id, date DESC)` is covered by the metrics PKs; add `(client_id, status)` on proposals, `(client_id, applied_at)` on change_log, `(due_at) WHERE done_at IS NULL` on evaluations, `(entity_type, entity_id, observed_at DESC)` on entity_history.

Retention: delete `metrics_hourly` rows older than 90 days (daily data already exists; nothing is "rolled up").

### 8.4 Conversion and click definitions (single definition, used everywhere)

Set in `rules.yaml → targets` (inputs I-1, I-4):

| Metric | Meta | Google |
|---|---|---|
| clicks | `inline_link_clicks` | `metrics.clicks` |
| conversions | the count for exactly one configured action type (`targets.meta_conversion_action`), read from `actions` | `metrics.conversions` |
| conv_value | value for the same action type from `action_values` | `metrics.conversions_value` |
| attribution | request with the ad sets' own attribution setting so numbers match Ads Manager. `VERIFY-P0`: parameter `use_unified_attribution_setting=true`. | account defaults; dates are click dates (`segments.date`) |

Never sum multiple Meta action types (they overlap and double-count).

---

## 9. Jobs

Schedules use the client `TIMEZONE`. "Account day" always means the day in `ad_accounts.timezone`. Each job takes a Postgres advisory lock and writes `job_runs` rows keyed by `window_key`.

| Job | Cadence | Does | LLM |
|---|---|---|---|
| `pacing` | every 15 min | §12.4. Pulls today's spend at account + campaign level only. Pings `HEARTBEAT_URL` on success. | 0 |
| `hourly_pull` | hourly :05 | Upsert `metrics_hourly` for the current and previous 3 hours (account + campaign). Refresh entity status/budget/learning; append `entity_history` on change. Detect manual changes (§9.2). | 0 |
| `daily_pipeline` | 06:00 | One chained pipeline, steps in §9.1. | small, few calls |
| `search_terms` | daily 07:00 (Google) | Pull search terms for the last 30 days (upsert). Apply the `negative_keyword_candidate` rule. | optional small batch |
| `weekly_strategy` | Mon 07:00 | Re-pull daily metrics for the last 30 days. Budget allocation (§10.6). Fatigue → LLM creative drafts (created PAUSED, need approval). LLM analyst on top movers. Threshold-change **proposals** from `rule_stats` (§16.4). Weekly report. | large, 1–3 calls |
| `approval_expiry` | every 15 min | Remind once at 12 h; expire at 24 h (`proposals.status = expired`). | 0 |
| `emergency_restore` | hourly :10 | §12.4 step 6. | 0 |
| `token_reset` | 00:00 | No-op on data (usage is computed from `llm_calls` by date); kept only to emit the previous day's LLM usage metric. | 0 |
| `retention` | daily 03:00 | Delete `metrics_hourly` older than 90 days; set `metrics_daily.is_final`. | 0 |
| `backfill` | manual, once in Phase 1 | Pull 90 days of `metrics_daily` for all entities and 28 days of `metrics_hourly` if the platform still serves it. | 0 |

### 9.1 `daily_pipeline` steps (strict order; each step is its own `job_runs` row and is skipped if already `success` for that `window_key = as_of date`)

`as_of` = yesterday in the account timezone. If any ad account's local time is earlier than 03:00 when the job fires, skip that account for today's run and raise a `warn` alert (data not ready).

1. `repull` — upsert `metrics_daily` for the last 7 account days, all entity levels.
2. `windows` — pull Meta 7-day reach/frequency per ad set and ad into `metrics_window`.
3. `features` — build `features` for `as_of`.
4. `evaluate` — run due evaluations (§16). Rollbacks happen here, **before** new proposals are made.
5. `score` — build `scores` for `as_of`.
6. `rules` — run rules → `proposals` (deduped).
7. `triage` — LLM triage for `escalate` results, within token budget.
8. `guardrail` — check every `pending` proposal.
9. `execute` — execute `Allow` results according to mode.
10. `approvals` — create and send approval requests.
11. `digest` — send the daily digest (yesterday's spend vs cap, CPA/ROAS vs target, top 3 winners/losers, changes made, rollbacks, pending approvals, LLM cost).

If a step fails, later steps do not run; the job is retried once after 10 minutes, then alerts `critical`.

### 9.2 Manual-change detection

A manual change is a change to a **tracked field** (`status`, `daily_budget`) of a managed entity that was not made by the engine.

- Preferred: read the platform's change history. Google: `change_event` resource (gives the actor and client type). Meta: the ad account `activities` edge. `VERIFY-P0` both.
- Fallback (and cross-check): in `hourly_pull`, if a tracked field differs from the last `entity_history` row and there is no `change_log` row for that entity with `applied_at` in the last 2 hours whose `after` equals the new value, treat it as manual.
- Effect: append `entity_history`, raise an `info` alert, and start the `after_manual_change_hours` cooldown for that entity.

### 9.3 Rate limits

Connectors use a per-platform token bucket and exponential backoff with jitter (max 5 retries) on rate-limit and transient errors. Insights are always requested for many entities in one call using level + filtering, never one call per entity.

---

## 10. Ranking method

Goal: rank entities so a tiny lucky sample never beats a large proven one.

All parameters live in `rules.yaml → stats`:

```yaml
stats:
  score_window_days: 14      # scoring uses this many account days
  settle_days: 2             # ...ending this many days before today (conversions still arriving are excluded)
  prior_window_days: 28
  ctr_prior_impressions: 2000
  theta_prior_spend_x_cpa: 1.0   # prior weight = this many target CPAs of spend
  aov_prior_conversions: 5
  min_parent_spend_x_cpa: 3.0    # below this, fall back to the account-level prior
  mc_draws: 10000
```

"Scoring window" = the `score_window_days` account days ending `settle_days` before today. Notation for one entity over the scoring window: impressions `n`, clicks `c`, spend `s`, conversions `v`, conversion value `w`.

### 10.1 Peer groups and priors

| Entity | Peer group | Prior source ("parent") |
|---|---|---|
| Meta ad | ads in the same ad set | the campaign |
| Meta ad set | ad sets in the same campaign | the campaign |
| Google ad | ads in the same ad group | the campaign |
| Google keyword | keywords in the same ad group with the same match type | the campaign |
| Campaign | campaigns on the same platform with the same objective | the ad account |

Parent totals are computed over `prior_window_days` ending at the same settled date, **excluding the entity itself** (leave-one-out). If the remaining parent spend is below `min_parent_spend_x_cpa × target_cpa`, use the ad account totals (also leave-one-out). If those are also too small, use the target itself (`θ_parent = 1 / target_cpa`, `CTR_parent = 0.01`).

### 10.2 CTR — Beta-Binomial

- `m` = parent clicks / parent impressions; `k = ctr_prior_impressions`.
- Clamp the data first: `c ← min(c, n)`.
- Posterior `Beta(m·k + c, (1 − m)·k + n − c)`.
- `ctr_post_mean = α / (α + β)`; `ctr_p10`, `ctr_p90` from `scipy.stats.beta.ppf`.

### 10.3 Conversions per unit spend — Gamma-Poisson

Conversions can exceed clicks on Meta (view-through, multiple conversions), so a Beta model on conversions/clicks is not used. Model `v ~ Poisson(θ · s)` where `θ` = conversions per currency unit.

- `θ_parent` = parent conversions / parent spend.
- Prior weight `b0 = theta_prior_spend_x_cpa × target_cpa`; `a0 = θ_parent × b0`. If `a0 < 0.1`, set `a0 = 0.1`.
- Posterior `θ ~ Gamma(shape a = a0 + v, rate b = b0 + s)`. Store `theta_a = a`, `theta_b = b`.
- In scipy: `gamma(a, scale=1/b)`.
- `cpa_shrunk = b / a`.
- `cpa_p10 = 1 / gamma.ppf(0.9, a, scale=1/b)`, `cpa_p90 = 1 / gamma.ppf(0.1, a, scale=1/b)` (so `cpa_p90` is the pessimistic CPA).

### 10.4 ROAS (only when `targets.objective = value`)

- `AOV_parent` = parent conv_value / parent conversions.
- `aov_shrunk = (w + aov_prior_conversions × AOV_parent) / (v + aov_prior_conversions)`.
- `roas_shrunk = (a / b) × aov_shrunk`.
- `roas_p10 = gamma.ppf(0.1, a, scale=1/b) × aov_shrunk`; `roas_p90` likewise with 0.9.
- Known limitation: order-value variance is ignored, so ROAS intervals are slightly too narrow. Acceptable for v1.

### 10.5 `p_beats_target`, rank and composite

| `targets.objective` | `p_beats_target` | `rank_metric` (best first) |
|---|---|---|
| `conversions` | `P(CPA < target_cpa) = 1 − gamma.cdf(1/target_cpa, a, scale=1/b)` | `cpa_p90` ascending |
| `value` | `P(ROAS > target_roas) = 1 − gamma.cdf(target_roas / aov_shrunk, a, scale=1/b)` | `roas_p10` descending |
| `traffic` | `P(CTR > CTR_parent) = 1 − beta.cdf(CTR_parent, α, β)` | `ctr_p10` descending |

- `rank_in_peer` = position by `rank_metric` within the peer group (1 = best). Ties broken by higher spend, then lower internal id.
- Ranking uses the pessimistic bound, so it never saturates. `composite = round(100 × p_beats_target)`, minus 10 if `fatigue_flag` (floor 0), is **display only** and is never used in a rule.

### 10.6 Minimum data (`data_sufficient`)

Measured over the scoring window:

| Entity | `data_sufficient = true` when |
|---|---|
| Meta ad | impressions ≥ 1,000 AND spend ≥ 1 × target_cpa AND `hours_live ≥ 72` |
| Meta ad set | `learning = false` AND spend ≥ 3 × target_cpa |
| Google keyword | clicks ≥ 100 OR spend ≥ 2 × target_cpa |
| Google ad | ad-group impressions ≥ 1,000 AND the ad's impressions ≥ 300 |
| Campaign | spend ≥ 5 × target_cpa AND `learning = false` |

Entities with `data_sufficient = false` are ranked with a "low data" tag and can never be the subject of a `pause` or `budget_change` proposal from a rule.

### 10.7 Budget allocation (weekly only)

Runs in `weekly_strategy`. Daily budget moves come only from the scale/trim rules (§11).

1. **Pools.** A pool is either (a) the ad sets of one managed Meta campaign with `budget_level = ad_set`, or (b) the managed campaigns that share the same non-null `campaigns.pool` label. Members must be budget units, `data_sufficient`, `learning = false`, not in cooldown. Pools with fewer than 2 eligible members are skipped.
2. **Win share.** Draw `mc_draws` samples of `θ` (or `θ × aov_shrunk` for value) per member with `numpy.random.default_rng(seed = as_of as YYYYMMDD integer)`. `win_share_i` = fraction of draws where member `i` is the maximum.
3. **Target share.** Clamp `win_share_i` to `[share_floor_factor / n, share_ceiling]`, then renormalise to sum to 1.
4. **Deltas.** `target_budget_i = target_share_i × pool_total`. `delta_i = target_budget_i − current_budget_i`, clamped to ± `auto_max_budget_change_pct` of the current budget.
5. **Increase eligibility.** Set `delta_i = 0` for any increase where `spend_pace_7d < allocation.min_spend_pace` (it is not using its current budget) or, on Meta, `frequency_7d > allocation.max_frequency_7d` (saturated).
6. **Zero-sum.** Scale down the larger side so total increases equal total decreases. The pool total never changes.
7. Drop deltas under `min_useful_change_pct`. Emit the rest as `budget_change` proposals with `source = allocation`, `risk = low`; they pass through the guardrail like any other proposal.

---

## 11. Rules engine

### 11.1 Variable registry

`engine/rules/registry.py` is the single list of names a rule may use. Loading a rules version fails (and `PUT /rules` returns 422) if any expression references a name not in the registry. A CI test asserts that every registry name has a builder and every `features`/`scores` column is in the registry.

Windows: `_1d` = yesterday (account day); `_7d` / `_28d` / `_30d` = that many complete account days ending yesterday; `_s14` = the scoring window (settled).

| Name | Type | Definition |
|---|---|---|
| `platform`, `entity_type`, `campaign_kind` | str | from the entity |
| `objective`, `target_cpa`, `target_roas` | str/num | from `rules.yaml → targets` |
| `is_budget_unit` | bool | §8.2 |
| `learning` | bool | §8.2 (`NULL` → `true`) |
| `data_sufficient` | bool | §10.6 |
| `hours_live`, `days_live` | num | since `first_live_at` (ads) or first `entity_history` row with active status |
| `impressions_1d`, `clicks_1d`, `spend_1d`, `conversions_1d` | num | sums |
| `spend_7d`, `conversions_7d`, `spend_s14`, `conversions_s14`, `impressions_s14`, `clicks_s14` | num | sums |
| `ctr_1d`, `ctr_7d`, `ctr_28d` | num / None | clicks ÷ impressions; None if impressions = 0 |
| `cpa_1d`, `cpa_7d`, `cpa_28d` | num / None | spend ÷ conversions; None if conversions = 0 |
| `roas_7d`, `roas_28d` | num / None | conv_value ÷ spend; None if spend = 0 |
| `frequency_7d` | num / None | from `metrics_window` (Meta only) |
| `spend_pace_7d` | num / None | `spend_7d ÷ (7 × current daily budget)`; only for budget units, else None |
| `fatigue_flag` | bool | `frequency_7d > 3.5 and ctr_7d < 0.7 × ctr_28d and days_live ≥ 14` |
| `cpa_shrunk`, `cpa_p90`, `roas_shrunk`, `roas_p10`, `p_beats_target` | num | §10 |
| `rank_in_peer`, `peer_size` | int | §10.5 |
| `active_ads_in_group` | int | active ads in the same ad set / ad group |
| `cost_30d`, `clicks_30d`, `conversions_30d` | num | search terms only |

Evaluation semantics:
- If an expression raises (e.g. comparing with `None`), the rule evaluates to **false** for that entity and the event is logged at debug level. It is not an error.
- Rules are sorted by `priority` ascending. For each entity, the **first** rule whose `applies_to` matches and whose `when` is true wins; later rules are not evaluated for that entity.
- Priority bands: 100–199 protection, 200–299 anomaly, 300–399 kill, 400–499 optimisation, 900 noop.

### 11.2 `rules.yaml` (seed; values marked `# INPUT` come from §25)

```yaml
version: 1
targets:
  currency: INR                    # INPUT I-2
  objective: conversions           # INPUT I-1: conversions | value | traffic
  target_cpa: 500                  # INPUT I-1
  target_roas: 3.0                 # INPUT I-1 (used only when objective = value)
  meta_conversion_action: ""       # INPUT I-1, exact Meta action_type string

stats: { ... }                     # see section 10

rules:
  - key: hold_low_data
    priority: 100
    applies_to: [ad, ad_group, keyword, campaign]
    when: "not data_sufficient"
    action: noop

  - key: hold_learning
    priority: 110
    applies_to: [ad_group, campaign]
    when: "learning"
    action: noop

  - key: cpa_spike_anomaly
    priority: 200
    applies_to: [ad_group, campaign]
    when: "(conversions_1d == 0 and spend_1d >= 3 * target_cpa) or (conversions_1d > 0 and spend_1d >= target_cpa and cpa_1d > 2.0 * cpa_28d)"
    action: escalate
    reason: anomaly

  - key: ctr_collapse_anomaly
    priority: 210
    applies_to: [ad, ad_group]
    when: "impressions_1d >= 2000 and ctr_1d < 0.5 * ctr_28d"
    action: escalate
    reason: anomaly

  - key: pause_clear_loser_ad
    priority: 300
    applies_to: [ad]
    when: "spend_s14 >= 3 * target_cpa and conversions_s14 == 0 and active_ads_in_group > 2"
    action: pause
    risk: low

  - key: pause_high_cpa_ad
    priority: 310
    applies_to: [ad]
    when: "p_beats_target < 0.10 and cpa_shrunk > 2.0 * target_cpa and active_ads_in_group > 2"
    action: pause
    risk: low

  - key: creative_fatigue
    priority: 320
    applies_to: [ad]
    when: "fatigue_flag"
    action: escalate
    reason: fatigue                # weekly job turns this into a creative-refresh proposal (approval required)

  - key: scale_winner
    priority: 400
    applies_to: [ad_group, campaign]
    when: "is_budget_unit and p_beats_target > 0.80 and spend_pace_7d >= 0.90"
    action: budget_change
    params: { change_pct: 15 }
    risk: low

  - key: trim_underperformer
    priority: 410
    applies_to: [ad_group, campaign]
    when: "is_budget_unit and p_beats_target < 0.20"
    action: budget_change
    params: { change_pct: -15 }
    risk: low

  - key: negative_keyword_candidate
    priority: 420
    applies_to: [search_term]
    when: "cost_30d >= 3 * target_cpa and conversions_30d == 0 and clicks_30d >= 20"
    action: add_negative
    params: { match_type: exact, level: ad_group }
    risk: medium

  - key: within_band
    priority: 900
    applies_to: [ad, ad_group, keyword, campaign, search_term]
    when: "True"
    action: noop
```

Why 3 × target CPA for the zero-conversion rules: for an entity that is truly at target, the chance of zero conversions after spending 3 target CPAs is about 5% (`e^-3`); at 2× it is about 13.5% and at 1× about 37%, which would pause or block too many good entities.

`scale_winner` requires `spend_pace_7d >= 0.90`: only raise a budget that is actually being spent.

---

## 12. Guardrails, modes and pacing

### 12.1 Modes

| Mode | Platform credentials used | Proposals | Auto-execution | Emergency stop |
|---|---|---|---|---|
| `read_only` | read | created, then all `blocked (mode_read_only)` | none | alert only (no write credential) |
| `suggest` | read + write | all `awaiting_approval` | only after approval | pauses campaigns |
| `auto_small` | read + write | per guardrail | `Allow` results with `risk = low` | pauses campaigns |
| `auto` | read + write | per guardrail | `Allow` results with `risk ∈ {low, medium}` | pauses campaigns |

`risk = high` and anything in `approval_required_for` needs approval in every mode.

### 12.2 `limits.yaml` (seed; example numbers, real values are input I-2)

```yaml
version: 1
caps:                                   # major currency units; none may exceed HARD_* ceilings
  total:  { daily: 20000, monthly: 550000 }
  meta:   { daily: 12000, monthly: 330000 }
  google: { daily: 8000,  monthly: 220000 }
campaign:
  max_daily_budget: 8000
  min_daily_budget: 300
change:
  max_budget_change_pct_per_day: 20     # per budget unit, vs its start-of-day budget, either direction
  min_useful_change_pct: 5
  auto_max_budget_change_pct: 15
  auto_max_abs_change: 2000
  max_auto_pauses_per_day: 5
  keep_min_active_ads_per_group: 2
cooldowns:
  same_entity_days: 7                   # equals the evaluation horizon, so evaluations are never contaminated
  after_manual_change_hours: 48
learning_phase:
  block_budget_changes: true
  block_pauses: true
pacing:
  min_elapsed_hours: 3
  warn_spent_fraction: 0.85
  warn_projected_ratio: 1.10
  emergency_spent_fraction: 0.97
  platform_flex: { meta: 1.75, google: 2.0 }
  emergency_stop_enabled: true
allocation:
  share_floor_factor: 0.5
  share_ceiling: 0.60
  min_spend_pace: 0.90
  max_frequency_7d: 3.5
approval_required_for:
  - new_campaign
  - new_ad
  - enable
  - add_negative
  - rollback_with_state_drift
```

### 12.3 `guardrails.check(proposal, state) -> Allow | NeedsApproval | Block(reason)`

Pure function. `state` is a snapshot (mode, kill switch, limits, entity rows, entity_history, today's change_log, MTD spend). Checks run in this exact order; the first Block returns immediately.

Definitions:
- `spend_direction`: `reducing` = `pause`, negative `budget_change`, `add_negative`; `increasing` = `enable`, positive `budget_change`, `new_ad`, `new_campaign`; set when the proposal is created.
- `privileged` = `proposal.source ∈ {emergency, evaluator}` or the action came from `/control/pause-all`.
- `start_of_day_budget` = the budget in the latest `entity_history` row at or before the start of the current account day.

| # | Check | Result |
|---|---|---|
| 1 | mode is `read_only` | Block `mode_read_only` |
| 2 | kill switch on (DB or env) and not (`privileged` and `spend_direction = reducing`) | Block `kill_switch` |
| 3 | entity's campaign has `managed = false` | Block `not_managed` |
| 4 | campaign `automation_scope = campaign_only` and `entity_type ≠ campaign`; or `budget_change` on an entity that is not a budget unit; or `budget_level = shared` | Block `unsupported_scope` |
| 5 | entity in cooldown (a `change_log` row `applied` within `same_entity_days`, or a manual change within `after_manual_change_hours`) and not `privileged` | Block `cooldown` |
| 6 | `learning` is true/NULL and action is `budget_change` or `pause` on an ad group/campaign, and not `privileged` | Block `learning_phase` |
| 7 | action `pause` on an ad and active ads in its group after the pause < `keep_min_active_ads_per_group` | Block `min_active_ads` |
| 8 | `budget_change`: compute `new = current × (1 + change_pct/100)`. Clamp `new` to `start_of_day_budget × (1 ± max_budget_change_pct_per_day/100)`. Clamp to `[min_daily_budget, max_daily_budget]`. Round to 2 decimals. If `abs(new − current) / current < min_useful_change_pct/100` | Block `change_too_small`; otherwise continue with the clamped `new` written into `guardrail_result` |
| 9 | `spend_direction = increasing`: let `B` = sum of daily budgets of **all** active campaigns/budget units on the platform (managed and unmanaged) after the change. If `B > caps.<platform>.daily`, or total across platforms `> caps.total.daily`, or `MTD_spend + B × remaining_days_in_month > caps.<platform>.monthly` (same for total) | Block `cap_exceeded` + `warn` alert |
| 10 | mode is `suggest` | NeedsApproval |
| 11 | action in `approval_required_for`; or `risk = high`; or `abs(change_pct) > auto_max_budget_change_pct`; or `abs(new − current) > auto_max_abs_change`; or action `pause` and auto pauses already applied today ≥ `max_auto_pauses_per_day`; or (`risk = medium` and mode is `auto_small`) | NeedsApproval |
| 12 | otherwise | Allow |

Notes:
- The budget-sum check (step 9) bounds the **average** daily spend. Platforms may exceed a daily budget on a single day (Meta up to 1.75×, Google up to 2×, both evening out over the week/month), which is why pacing (§12.4) and native limits (§12.5) exist as separate layers.
- On approval, `check` is re-run with fresh state; mode `suggest` then returns Allow at step 10 only for that approved proposal (pass `approved=True`, which skips steps 10–11).

### 12.4 Pacing and emergency stop (job `pacing`, every 15 min)

For each ad account, using the account's local day:

1. Read `spend_today` (account level) and per-campaign spend from the platform. Read `mtd_spend` (from `metrics_daily` + `spend_today`).
2. **Intraday curve.** `curve(h)` = average, over the last 28 account days, of (cumulative spend at local hour `h` ÷ that day's total), from account-level `metrics_hourly`. If fewer than 14 days exist, use linear `curve(h) = h / 24`.
3. **Projection** (only when local elapsed hours ≥ `min_elapsed_hours`): `projected_eod = spend_today / max(curve(now), 0.05)`.
4. **Warn** (`warn` alert, deduped per account per day): `spend_today ≥ warn_spent_fraction × daily_cap`, or `projected_eod > warn_projected_ratio × daily_cap`, or any campaign's `spend_today > platform_flex × its daily budget`.
5. **Emergency stop** (requires `emergency_stop_enabled` and mode ≠ `read_only`; in `read_only`, raise the `critical` alert only):
   - Daily: `spend_today ≥ emergency_spent_fraction × caps.<platform>.daily` → pause all **managed, active** campaigns on that platform. If the breach is on `caps.total.daily` (sum of both platforms), pause on both.
   - Monthly: `mtd_spend ≥ emergency_spent_fraction × caps.<platform>.monthly` (or total) → same pause.
   - Implementation: status → PAUSED via the executor's emergency path (no dry-run, write-ahead `change_log`, read-back verify). Insert one `emergency_actions` row listing exactly the campaigns the engine paused; `restore_policy = auto_next_day` for a daily trigger, `manual` for a monthly trigger or `pause-all`.
   - Always raises a `critical` alert. If unmanaged campaigns are still active, the alert says so and lists them (the engine never touches unmanaged campaigns).
   - Emergency stop pauses; it never edits budgets (budget edits disturb learning and need a second edit to undo).
6. **Restore** (job `emergency_restore`): for each `emergency_actions` row with `restore_policy = auto_next_day`, `restored_at IS NULL`, whose account-local date is now later than the trigger date: if kill switch is off, mode ≠ `read_only`, and the monthly cap is not breached, re-enable each listed campaign **only if** its current status is still PAUSED and its last `entity_history` row has `source = emergency`. Skip campaigns someone touched since. Set `restored_at`. `manual` rows are restored only via `POST /emergency-actions/{id}/restore` (approver role).

### 12.5 Native platform backstops (Phase 0 checklist, done by Govinda in the platform UIs)

- Meta: set the **account spending limit**.
- Google: set an **account budget** where the billing setup allows it; otherwise note in `docs/VERIFIED.md` that Google has no native backstop for this account and lower `caps.google.daily` accordingly.

These are the only hard stops. The engine's caps are defence in depth on top of them.

### 12.6 Property-test invariants (hypothesis)

For any random sequence of proposals and state:
1. After applying only `Allow` results, the sum of active daily budgets per platform ≤ `caps.<platform>.daily`, and total ≤ `caps.total.daily`.
2. No budget unit's budget differs from its `start_of_day_budget` by more than `max_budget_change_pct_per_day`.
3. With kill switch on, the only `Allow` results are `privileged` and `spend_direction = reducing`.
4. No `Allow` for an entity with `learning` true/NULL unless `privileged`.
5. No `Allow` for an unmanaged campaign's entities, ever.
6. `check` is deterministic: same inputs → same output.

---

## 13. LLM layer and token economics

### 13.1 When the LLM is called

| Trigger | Tier | Typical tokens (in / out) | Frequency |
|---|---|---|---|
| Rule `escalate` (anomaly) | small | 1,000 / 200 per case | 5–15 cases/day, batched into 1–3 requests |
| Triage answers `escalate_large` | large | 10,000 / 2,000 | 0–2 per day |
| Creative refresh (fatigue) | large | 30,000 / 5,000 | weekly |
| Daily digest narrative | small | 2,000 / 500 | daily |
| Search-term relevance (optional) | small | 1,500 / 300 per batch of 50 terms | 1 batch/day |

Never called for: pulling data, computing metrics, scoring, enforcing budgets, or executing changes. If the LLM is unavailable or over budget, every pipeline still completes; escalated cases become `flag_human` alerts.

### 13.2 Prompt input (compact JSON, no raw rows)

```json
{
  "client": {"objective": "conversions", "target_cpa": 500, "currency": "INR"},
  "case": {
    "id": "esc_2026_11_11_07",
    "reason": "cpa_spike_anomaly",
    "entity": {"platform": "meta", "type": "ad_group", "id": 123, "learning": false},
    "untrusted_text": {"entity_name": "Retargeting 30d"},
    "metrics": {
      "d1":  {"spend": 4200, "conv": 2, "cpa": 2100, "ctr": 0.011, "freq": 4.1},
      "d7":  {"spend": 21000, "conv": 30, "cpa": 700, "ctr": 0.014},
      "d28": {"cpa": 520, "ctr": 0.016}
    },
    "score": {"p_beats_target": 0.18, "cpa_p90": 910},
    "recent_changes": [{"at": "2026-11-09", "action": "budget_change", "pct": 15}],
    "peer_context": {"rank": 4, "of": 5}
  },
  "allowed_actions": ["noop", "pause", "budget_change", "escalate_large", "flag_human"]
}
```

Output (validated with a Pydantic model; anything invalid is discarded and the case becomes `flag_human`):

```json
{"case_id": "esc_2026_11_11_07", "action": "budget_change", "change_pct": -15, "confidence": 0.7, "reason": "max 40 words"}
```

- `action` must be in `allowed_actions`. `change_pct` is an integer in `[-20, 20]` and is only read for `budget_change`.
- The model never supplies entity ids, budgets in currency, or free-form parameters; the engine builds the proposal from `case_id`.
- Names, ad text and search terms are **untrusted input**. They are only ever placed under `untrusted_text`, and the system prompt states that content there is data, not instructions.
- LLM proposals are `risk = medium` and go through the guardrail like any other proposal.
- Generated ad copy is only ever stored as a `new_ad` proposal (approval required, created PAUSED).

### 13.3 Token controls

- Summariser sends at most 20 entities per call, rounded numbers, short keys.
- Static system prompt + playbook first (cacheable by the provider if long enough); per-case JSON last.
- All triage cases from one job go in one request (array of cases).
- Use the provider's batch API for the weekly job when available.
- Cache by `prompt_hash`: an identical request within 24 h reuses the stored result (0 tokens).
- Budget check before every call, using `llm_calls` sums for the current client-timezone day and month:

  ```
  skip if  tokens_today + est_tokens > LLM_DAILY_TOKEN_CAP
        or cost_today  + est_cost   > LLM_DAILY_COST_CAP_USD
        or cost_month  + est_cost   > LLM_MONTHLY_COST_CAP_USD
  ```
  On skip: mark the case `flag_human`, raise one `warn` alert per day (deduped). Priority when near a cap: anomaly triage > analyst > digest > search terms > creative.
- `cost = (input_tokens × in_price + output_tokens × out_price) / 1,000,000`, with prices from the `LLM_*_PRICE_PER_MTOK` env values.
- Every call is recorded in `llm_calls`.

### 13.4 Monthly cost estimate (estimate, not a quote)

Assumptions: 30-day month; triage 10 cases/day; analyst 1/day; creative 4.3 runs/month; digest daily; search terms 1 batch/day; no caching or batch discount. Example prices are Anthropic list prices checked 11 Nov 2026 at https://platform.claude.com/docs/en/about-claude/pricing: Claude Haiku 4.5 $1 / $5 per MTok (in / out), Claude Sonnet 4.6 $3 / $15. The same page lists newer, cheaper models (Claude Haiku 5.5 at $0.10 / $0.50 for prompts up to 100K tokens; Claude Sonnet 5.5 at $2 / $10, with a tokenizer that produces about 30% more tokens for the same text). Choose models at Phase 4 (input I-8) and re-run this table.

| Workload | Tokens / month (in / out) | Est. cost (USD) |
|---|---|---|
| Triage (small) | 300K / 60K | 0.60 |
| Digest (small) | 60K / 15K | 0.14 |
| Search terms (small) | 45K / 9K | 0.09 |
| Analyst (large) | 300K / 60K | 1.80 |
| Weekly creative (large) | 129K / 21.5K | 0.71 |
| **Total** | **834K / 165.5K** | **3.34** |

Busiest day (Monday): about 63K tokens and about $0.30, inside the 100K-token and $1.00 daily caps. Hard ceiling: 3M tokens/month all on the large tier at 85% input / 15% output is about $14.40, so `LLM_MONTHLY_COST_CAP_USD=20` is safe.

---

## 14. Approval flow

1. Guardrail returns NeedsApproval → `proposals.status = awaiting_approval`, `expires_at = now + 24 h`, create an `approvals` row.
2. Notify on configured channels: summary line, before → after, reason, expected impact, and one link to the dashboard page `…/approvals/{id}`.
3. **Links never perform an action.** Opening the link (a GET, which chat apps pre-fetch for previews) only shows the page. Approve/reject is a POST from a logged-in session with a CSRF token, or `ads approve <id>` from the CLI.
4. Decision requires role `approver` or `admin`. Write `audit_log`.
5. On approve: re-run the guardrail with `approved=True` and fresh state. If it now Blocks, set `status = blocked` and notify the approver with the reason. Otherwise → executor.
6. Expiry: reminded once at 12 h, expired at 24 h. Expired proposals are never executed.
7. Batch approval: the dashboard can approve a selected group in one request; each proposal is still guardrailed individually.

---

## 15. Executor

The only code that calls a `WriteConnector`.

Per proposal:
1. Set `proposals.status = executing`. Read current state of the tracked fields from the **platform** → `before`.
2. Build `intended` (target values) and `rollback_payload` (the `before` values of exactly the fields being changed, or "remove negative keyword X").
3. **Write-ahead:** insert `change_log` with `status = applying`, `before`, `intended`, `rollback_payload`, and commit.
4. **Validate-only call** (skipped on the emergency path):
   - Google: the same mutate request with `validate_only = true`. `VERIFY-P0`.
   - Meta: the same request with `execution_options = ["validate_only"]`. `VERIFY-P0`: supported objects/fields. Where unsupported, validate locally (types, ranges, minimum budget).
   - On failure: `change_log.status = failed`, `proposals.status = failed`, `warn` alert. Stop.
5. Apply via the write connector. New ads and campaigns are always created PAUSED.
6. Read back the tracked fields → `after`. If `after` matches `intended`: `status = applied`, `applied_at = now`, append `entity_history` (`source = executor`), `proposals.status = executed`. If not: `status = verify_failed`, `critical` alert, no automatic retry.
7. Insert `evaluations` rows: `horizon_days = 3` with `due_at = applied_at + 3 d`, and `horizon_days = 7` with `due_at = applied_at + (7 + settle_days) d`. Not for emergency actions or rollbacks.

**Startup reconciler** (`engine/executor/reconcile.py`, runs at engine start and hourly): for each `change_log` row with `status = applying` older than 5 minutes, read the platform state. Equal to `intended` → `applied` (and do steps 6–7). Equal to `before` → `failed`. Anything else → `verify_failed` + `critical` alert.

**Rollback** (`engine/executor/rollback.py`): read current platform state. If the tracked fields equal the original change's `after`, apply `rollback_payload` as a new `change_log` row with `rollback_of` set, and mark the original `rolled_back`. If the state has drifted (someone changed it since), do not apply: create a proposal with action `rollback_with_state_drift` (approval required). A rollback that is `spend_direction = increasing` (re-enable, restore a higher budget) still passes guardrail steps 1–4 and 9.

---

## 16. Evaluation and rollback

### 16.1 Evaluation unit

Effects are measured where they can be observed:

| Change | Evaluation unit |
|---|---|
| pause / new ad | the parent ad set or ad group |
| budget_change on a Meta ad set | the parent campaign (siblings share the audience and auction, so they are not independent controls) |
| budget_change on a campaign | the campaign |
| add_negative | the ad group |

### 16.2 D+3 safety check (no statistics; conversions are still arriving)

Post period = the 3 account days after the change. On the evaluation unit:
- If `spend_post ≥ 3 × target_cpa` and `conversions_post = 0` → verdict `worse` → rollback.
- If the change was a budget increase and average daily spend after is below 50% of the average daily spend in the 7 days before → `warn` alert "delivery collapsed", no rollback.
- Otherwise no verdict; `action_taken = none`.

### 16.3 D+7 final verdict (runs at D+7+`settle_days`)

- Pre period: the 7 account days before the change day. Post period: the 7 account days after it (same weekdays). The change day itself is excluded.
- Unit totals: `(v_pre, s_pre)`, `(v_post, s_post)`. Control = all other campaigns on the same platform that had no engine change in either period: `(vc_pre, sc_pre)`, `(vc_post, sc_post)`.
- If `s_post < 2 × target_cpa`: verdict `insufficient_data`; create one `horizon_days = 14` evaluation (same method, 14-day post period). If that is also insufficient, final verdict `neutral`.
- Trend factor: `g = (vc_post / sc_post) ÷ (vc_pre / sc_pre)`. If either control period has spend below `3 × target_cpa` or zero conversions, `g = 1`.
- Draw `mc_draws` samples with `numpy.random.default_rng(seed = change_log.id)`:
  `θ_pre ~ Gamma(0.5 + v_pre, rate = s_pre)`, `θ_post ~ Gamma(0.5 + v_post, rate = s_post)`.
  For `objective = value`, multiply each `θ` by that period's `conv_value / conversions` (skip if conversions = 0).
- `r = θ_post / (θ_pre × g)` (ratio > 1 means efficiency improved).
- `p_worse = P(r < 1/1.15)` (CPA more than 15% worse). `p_better = P(r > 1.05)`.
- Verdict: `worse` if `p_worse > 0.8`; else `better` if `p_better > 0.7`; else `neutral`.
- Action: `worse` → rollback via §15. `better`/`neutral` → none.
- Update `rule_stats` for the proposal's `rule_key` (only D+7/D+14 verdicts count).

### 16.4 Learning (proposals only in v1)

In `weekly_strategy`, for each rule in `rule_stats`:
- `n_worse / n_changes > 0.40` with `n_changes ≥ 20` → raise a `warn` alert and create a **human-review note** suggesting a tighter threshold.
- `n_better / n_changes > 0.70` with `n_changes ≥ 20` → note suggesting loosening.

Nothing changes `rules` automatically. Winners picked on a good recent run tend to regress toward average, which inflates the "worse" count; a human decides. The weekly LLM analyst may read `rule_stats` to word the suggestion.

---

## 17. API (FastAPI, prefix `/api/v1`)

Auth: every route except `/health` requires `get_current_user()`. Roles: `viewer` (GET only), `approver` (viewer + approve/reject/rollback/restore), `admin` (everything). The env admin is `admin`.

- Browser: `POST /auth/login` creates a `sessions` row and sets an opaque session id in an `httpOnly; Secure; SameSite=Lax` cookie. State-changing requests need the `X-CSRF-Token` header (double-submit).
- CLI: `POST /auth/token` returns an opaque bearer token (`sessions.kind = cli`). Bearer requests skip CSRF.
- Logout / `ads logout` sets `revoked_at`. Sessions are checked on every request.
- Login is rate-limited (5 attempts per 15 minutes per IP and per user).

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/auth/login`, `/auth/token`, `/auth/logout` | — | sessions |
| GET | `/auth/me` | viewer | |
| GET | `/overview?range=7d` | viewer | spend vs caps, CPA/ROAS, pace, alert count, mode, kill switch |
| GET | `/campaigns`, `/campaigns/{id}`, `/ad-groups/{id}`, `/ads/{id}`, `/keywords?ad_group_id=` | viewer | entities with latest metrics + score |
| PUT | `/campaigns/{id}/managed` | admin | opt a campaign in/out of automation |
| GET | `/metrics?entity_type=&entity_id=&grain=day\|hour&from=&to=` | viewer | time series |
| GET | `/rankings?level=ad\|ad_group\|keyword\|campaign&platform=` | viewer | ranked table with intervals |
| GET | `/proposals?status=` | viewer | |
| POST | `/proposals/{id}/approve`, `/proposals/{id}/reject`, `/proposals/approve-batch` | approver | |
| POST | `/proposals` | admin | manual proposal (guardrailed like any other) |
| GET | `/proposals/{id}/dry-run` | viewer | payload that would be sent |
| GET | `/changes` | viewer | change feed with verdicts |
| POST | `/changes/{id}/rollback` | approver | manual rollback (§15) |
| GET | `/evaluations?change_id=` | viewer | |
| GET | `/rules`, `/limits` | viewer | active version + history |
| PUT | `/rules`, `/limits` | admin | new version; validated (schema, registry names, `HARD_*` ceilings), audited |
| GET | `/llm/usage?range=30d` | viewer | tokens and cost |
| GET | `/alerts` · POST `/alerts/{id}/ack` | viewer · approver | |
| GET | `/emergency-actions` · POST `/emergency-actions/{id}/restore` | viewer · approver | |
| POST | `/control/mode` | admin | bounded by `HARD_MAX_MODE` |
| POST | `/control/kill-switch` | admin | on/off (cannot clear the env override) |
| POST | `/control/pause-all` | approver | emergency pause of managed campaigns; `restore_policy = manual` |
| POST | `/jobs/{name}/run` | admin | manual trigger |
| GET | `/system` | viewer | job runs, last sync times, connector health |
| GET | `/health` | none | liveness only: `{"status":"ok"}`, no data |
| GET | `/metrics` | bearer `METRICS_TOKEN` | Prometheus; not routed by the public proxy |

---

## 18. Dashboard and routing

Next.js with `basePath = NEXT_PUBLIC_BASE_PATH`; all fetches go to `NEXT_PUBLIC_API_URL`. Both are **build-time** values passed as Docker build args; changing them needs an image rebuild.

Caddy routing (order matters):

| Request path | Goes to | Rewrite |
|---|---|---|
| `/ads-autopilot/api/*` | `api:8000` | strip the `/ads-autopilot` prefix (API receives `/api/v1/...`) |
| `/ads-autopilot/*` | `dashboard:3000` | none (Next.js handles its own basePath) |

`/metrics` is not exposed by the proxy.

| Page | Content |
|---|---|
| Login | user + password |
| Overview | spend today vs daily cap (gauge) per platform and total, month-to-date vs monthly cap, intraday pace line vs expected curve, CPA and ROAS 28-day lines with target line, Meta vs Google split, open approvals, critical alerts, mode + kill switch, active emergency actions |
| Campaigns | table: status, managed toggle, kind, budget level, budget, spend 7d, CPA, ROAS, score, learning flag; drill-down to ad sets/ad groups and ads |
| Rankings | per level: ranked table with interval bars, low-data tag, filters |
| Creatives | ad text/thumbnails, CTR trend, frequency, fatigue flag, draft variants awaiting approval |
| Keywords & search terms | keyword table, negative keyword candidates, wasted spend total |
| Approvals | pending queue with before → after, reason, guardrail result, approve/reject, batch approve |
| Change log | feed with status and verdicts, rollback button |
| Budgets & rules | caps, limits and rules viewer/editor with validation errors and version diff |
| AI usage | tokens/day by tier, cost today and MTD vs caps, calls by purpose |
| Alerts | list, ack |
| System | job runs by step, last sync times, connector health, API operations used today |

---

## 19. CLI

`ads` (Typer), calls the API with the bearer token from `ads login` (stored in a `0600` file).

| Command | Does |
|---|---|
| `ads login` / `ads logout` | create / revoke the CLI session |
| `ads status` | mode, kill switch, spend vs caps, pace, open approvals, last job runs |
| `ads proposals [--status awaiting_approval]` | list |
| `ads approve <id> [...]` / `ads reject <id> --reason` | |
| `ads dry-run <proposal_id>` | payload that would be sent |
| `ads changes [--since 7d]` | change feed with verdicts |
| `ads rollback <change_id>` | |
| `ads pause-all [--platform meta\|google] --confirm` | emergency pause of managed campaigns |
| `ads restore <emergency_action_id>` | restore campaigns paused by an emergency action |
| `ads manage <campaign_id> on\|off` | opt a campaign in/out |
| `ads mode <read_only\|suggest\|auto_small\|auto>` | |
| `ads kill-switch <on\|off>` | |
| `ads run <job>` | trigger a job now |
| `ads rankings --level ad --top 20` | |
| `ads llm-usage` | tokens/cost today and MTD |

---

## 20. Security

- Secrets only in `.env` (gitignored) or the host's secret store; `.env.example` has no values. Pre-commit `gitleaks` scan.
- Only the credentials provider reads secrets. Logs redact anything matching token patterns.
- **Separate read and write credentials** per platform. The write credential is loaded only by the executor and the pacing emergency path, and is left empty until its phase gate (Meta: Phase 3, Google: Phase 5).
- Least privilege: Meta system-user tokens scoped to the one ad account; Google OAuth users with access to the one customer id only.
- Admin password stored as an argon2id hash. Server-side sessions (revocable), CSRF on cookie-authenticated writes, login rate limit.
- Write paths gated by mode + kill switch + guardrail. The only paths that skip parts of the guardrail are spend-reducing: emergency stop and `pause-all`.
- `HARD_*` env ceilings cannot be raised from the API, so a stolen admin session cannot lift caps or enable `auto` beyond what the deployment allows.
- `audit_log` is append-only at the DB-role level.
- HTTPS via the proxy; Postgres not exposed publicly; daily encrypted backups with a tested restore.
- LLM: no PII, tokens or account ids in prompts; only aggregated metrics and ad/search text, the latter marked untrusted (§13.2).

---

## 21. Observability

- Structured JSON logs with `job`, `step`, `client_id`, `proposal_id`, `change_id`.
- Prometheus metrics: job duration/success by step, platform API calls and errors, operations used today per platform, rows ingested, proposals by status, guardrail blocks by reason, changes by status, rollbacks, emergency actions, LLM tokens and cost, spend vs cap.
- **Dead-man's switch:** the pacing job pings `HEARTBEAT_URL` on every successful run. The external monitor alerts if two consecutive pings are missed. This covers the case where the whole box is down.
- Alerts are deduplicated by `dedupe_key` (one open alert per key; re-send at most every 6 h for `critical` until acked).
- Alert list: job failed after retry, data stale > 2 h, credential invalid or expiring within 7 days, pacing warn, emergency stop, verify failure, reconciler found drift, rollback executed, LLM cap hit, API operations > 80% of limit, manual change detected (`info`).

---

## 22. Testing strategy

| Level | What |
|---|---|
| Unit | feature formulas; posterior math against hand-computed values (at least 3 fixed cases per formula in §10 and §16); rule expression semantics incl. `None` handling and first-match priority; money conversion; pacing curve; summariser size |
| Property (hypothesis) | the six invariants in §12.6 |
| Boundary | import-lint: only `engine/executor` and `engine/pacing/emergency.py` import `WriteConnector`; only `engine/connectors` imports the platform SDKs |
| Registry | every rule name is in the registry; every registry name has a builder |
| Contract | recorded SDK responses (fixtures) for each connector method, to catch API changes on version bumps |
| Crash safety | kill the executor between write-ahead and apply, and between apply and read-back; the reconciler must reach the right final status in both cases |
| Idempotency | run `daily_pipeline` twice for the same `as_of`: no new proposals, no second execution |
| Replay / backtest | run rules + guardrail over the 90-day backfill in `read_only`; report proposals per rule per week for human review before `suggest` goes live |
| Integration | Google: test account (writes + validate-only). Meta: one dedicated campaign kept PAUSED with the minimum budget, plus validate-only paths |
| Dashboard | Playwright smoke: login, overview loads under the base path, approve flow, logout revokes the session |
| LLM | golden cases with fixed JSON inputs and an expected action class, temperature 0; run nightly (not per commit); fail on invalid JSON or disallowed action |

---

## 23. Phased milestones

Build durations assume one developer. Observation ("soak") periods run **in parallel with the next phase's build**; a phase's gate is passed when its soak ends, and the gate, not the build, controls when writes are switched on.

| Phase | Build | Deliverables | Gate / acceptance checks |
|---|---|---|---|
| 0. Setup | 1 week | repo, docker-compose with proxy, Postgres + Alembic, `.env.example`, bootstrap, credentials provider, read credentials for both platforms, Google Basic-access application submitted, Meta access case confirmed, native backstops set (§12.5), CI, `docs/VERIFIED.md` with every `VERIFY-P0` resolved, `docs/VERSIONS.md` | both platforms readable from a script; money fixture tests pass; all inputs I-1…I-4, I-9, I-10 received |
| 1. Read-only ingestion + dashboard | 2 weeks | entities + history, hourly/daily ingestion, 90-day backfill, pacing (alerts only), heartbeat, sessions + login, Overview/Campaigns/System pages, CLI `status` | 7 consecutive days with no failed job; for dates at least 3 days old, daily spend within 0.5% and conversions within 2% of Ads Manager / Google Ads UI using the §8.4 definitions |
| 2. Scoring + suggest mode | 2 weeks | features, registry, ranking, rules, guardrail + property tests, proposals, approvals UI/CLI, digest, backtest report | backtest reviewed; then 2 weeks in `suggest`; ≥ 80% of proposals judged sensible by Govinda; **Govinda signs off before any write credential is installed** |
| 3. Guarded auto-actions (Meta) | 2 weeks | executor + reconciler, rollback, evaluator, emergency stop + restore, `auto_small`, change-log page | crash-safety and idempotency tests pass; then `HARD_MAX_MODE=auto_small` on 1–2 opted-in Meta campaigns for 2 weeks with no invariant breach and at least one successful rollback drill |
| 4. LLM layer | 1 week | summariser, triage + analyst, token controller, creative drafts, AI usage page | cost within caps for 1 week; golden set passes; pipelines complete with the LLM disabled |
| 5. Google writes | 1.5 weeks | Google write connector, search-term rule + negatives flow, RSA draft refresh | same gate as Phase 3, on 1–2 opted-in Google campaigns for 2 weeks |
| 6. Integration into main system | 1 week | `main_system_auth`, `remote_provider`, route-based deployment, DB migration plan | runs under the main domain at `/ads-autopilot` with main-system login |

Total: about 10.5 weeks of build effort; about **13–15 calendar weeks** including soak periods and platform access approvals. Phases 4 and 5 can run in parallel with two people.

---

## 24. Integration into the main system

Three seams change; everything else is untouched.

1. **Auth adapter** — `AUTH_MODE=main_system`: `get_current_user()` verifies the main system's JWT (JWKS URL) or session and maps its roles to `viewer` / `approver` / `admin`. Env login and the `sessions` table are bypassed.
2. **Credentials provider** — `CREDENTIALS_PROVIDER=remote`: `get_credentials(client_id, platform, access)` calls the main backend with a service token; results cached in memory for a short TTL, never written to disk. Token refresh stays the main backend's job.
3. **Frontend route** — the main system's router proxies `/ads-autopilot/*` and `/ads-autopilot/api/*` exactly as the Caddy rules in §18. If the prefix differs, rebuild the dashboard image with new build args.

Data: keep a separate Postgres schema `ads_autopilot` (or separate DB) at first. Multi-client later needs: per-client scheduler fan-out, a client selector in the UI, and `client_id` added to the unique keys' leading column where not already present.

Main-system team must provide: JWT format/JWKS, role names, credentials endpoint contract, route prefix, and where notifications should go.

---

## 25. Inputs required from Govinda

The agent must stop and ask if an input is missing when its phase starts.

| # | Input | Config key | Needed by |
|---|---|---|---|
| I-1 | Objective; target CPA and/or ROAS; the exact conversion event on each platform (e.g. purchase or lead) | `rules.yaml → targets` | Phase 0 |
| I-2 | Daily and monthly caps (total and per platform), currency, hard ceilings | `limits.yaml → caps`, `HARD_*` | Phase 0 |
| I-3 | Which campaigns are opted in first | `campaigns.managed` | Phase 2 |
| I-4 | Is conversion value tracked on both platforms? Use GA4 cross-check? | `targets.objective`, `GA4_*` | Phase 0 |
| I-5 | Approval channel: Slack, WhatsApp (which provider), or email | notification env | Phase 2 |
| I-6 | Who besides Govinda can approve | users/roles | Phase 2 |
| I-7 | Hosting: own server, main system's cloud, or a small VPS; domain for TLS | deployment | Phase 0 |
| I-8 | LLM provider, small and large model, monthly cap | `LLM_*` | Phase 4 |
| I-9 | Meta: can a system user be created in the Business Manager that owns the ad account? | `META_*_TOKEN` | Phase 0 |
| I-10 | Google: direct access to the account or via a manager account? Which Google Cloud project? | `GOOGLE_*` | Phase 0 |
| I-11 | Main system: JWT/session format and how platform credentials are stored there | Phase 6 env | Phase 6 |
| I-12 | Allocation pools for Google campaigns, if any | `campaigns.pool` | Phase 5 |

---

## 26. Risks

| Risk | Mitigation |
|---|---|
| Platforms overspend a daily budget on a single day by design | native account limits (§12.5), budget-sum cap, pacing projection, emergency pause |
| Whole service down, so no pacing protection | external heartbeat; native limits still apply |
| Attribution delay makes recent CPA look bad | settled scoring window, 7-day daily re-pull, 30-day weekly re-pull, D+7 verdict judged after `settle_days` |
| Resetting Meta learning with frequent edits | ≤ 20%/day, 7-day cooldown, learning protection, emergency path pauses instead of editing budgets |
| SDK / API version changes | pinned versions, contract fixtures, `docs/VERSIONS.md`, connector isolation |
| Google access level too low or delayed | apply for Basic on day 1; operation budget fits inside Explorer |
| Credential expiry stops the system silently | credential check in `hourly_pull`, 7-day-ahead alert, staleness alert |
| Bad thresholds | backtest, suggest-mode soak, rule stats reviewed by a human |
| LLM hallucinated or injected actions | untrusted-text isolation, strict output schema, action whitelist, guardrail after the LLM, human approval for all generated copy |
| Manual edits conflict with the engine | change-history detection, 48 h cooldown, rollback refuses on state drift |
| Crash mid-write | write-ahead `change_log` + reconciler |
| Small account, little data | leave-one-out priors with account fallback, 14-day scoring window, low-data holds |

---

## 27. Changes from v1.0

| Area | v1.0 | v1.1 |
|---|---|---|
| Budget goal | "stop overruns within 15 minutes" | detect within one cycle; native platform limits are the hard stop |
| Pacing | linear `pace` formula, thresholds 1.15 / 1.30, budget capping | historical intraday curve, cap-based triggers, pause-only emergency, automatic next-day restore |
| Guardrail order | `suggest` returned NeedsApproval before block checks | all block checks first, then mode |
| Kill switch | blocked all writes | still allows privileged spend-reducing writes |
| Connectors | MCP servers at runtime | official SDKs; MCP repos are reference only |
| Google access | optional developer token | no developer token; Cloud-project access level, Basic applied for on day 1 |
| Meta credential | long-lived token | system-user tokens, separate read and write |
| Conversion model | Beta-Binomial on conversions/clicks | Gamma-Poisson on conversions per spend |
| Ranking | by composite `P(beats target)` | by pessimistic bound; composite is display only |
| Allocation | daily Thompson share | weekly, zero-sum, floors/ceilings, only into budgets that are being spent |
| `scale_winner` | `spend_pace < 1.0` | `spend_pace_7d >= 0.90` |
| Kill thresholds | 2× CPA (ads), 1× CPA (search terms) | 3× CPA on settled data; negatives always need approval |
| Rule variables | several undefined | registry with load-time validation |
| Rule matching | undefined | priority order, first match wins, anomaly before kill |
| Cooldown | 72 h | 7 days (= evaluation horizon) |
| Evaluation | D+3 and D+7 statistical verdicts on the entity | D+3 safety check only; D+7 on the evaluation unit after settling; exact Monte Carlo procedure |
| Rollback | apply stored inverse | only if state has not drifted, otherwise approval |
| Learning | auto-tighten thresholds | suggestions only |
| Budget ownership | implicit | `budget_level`, budget units, shared budgets and PMax/Advantage+ scoped out |
| Schema | — | added `entity_history`, `metrics_window`, `config_versions`, `emergency_actions`, `rule_stats`, `sessions`; proposal `dedupe_key`; write-ahead `change_log.status`; account timezone; removed `rules` and `budgets` tables |
| Frequency | summed from daily rows | pulled as a 7-day window |
| Job order | review 06:00, evaluate 06:30, digest 06:15 | one chained pipeline: evaluate before rules, digest last |
| Config | env and DB both authoritative | env/YAML seed only; DB at runtime; `HARD_*` ceilings in env |
| Auth | JWT cookie | revocable server-side sessions, bearer tokens for CLI, roles from day 1 |
| Approval links | could act via signed link | links only open a page; action is an authenticated POST |
| Routing | no proxy, API URL without `/v1` | Caddy in compose, explicit path rules, build-time note |
| LLM controls | daily cost cap unused; search terms not costed | all three caps enforced; estimate updated to $3.34/month |
| Observability | — | heartbeat, alert dedupe, API operation counter |
| Timeline | 9–11 weeks | 10.5 weeks build, 13–15 calendar weeks with soak periods and a 90-day backfill |
