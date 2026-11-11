# ads-autopilot — Build Plan

Version 1.0 · 11 Nov 2026 · Owner: Govinda Vashishtha
Scope: one client, one Meta Ads account + one Google Ads account, standalone repo, later merged into the main system.

---

## 1. Summary

ads-autopilot is a self-hosted service that pulls Meta Ads and Google Ads data on a schedule, ranks every ad / ad set / keyword with confidence-aware scores, enforces budget limits in code, applies small changes automatically, routes big changes to a human, and checks every change 3–7 days later (rolling back the losers).

Core design rule: **deterministic code and SQL do ~90% of the work** (ingestion, features, ranking, rules, guardrails, execution, evaluation). The LLM is called only for (a) ambiguous/anomalous cases, (b) rare root-cause analysis, (c) ad copy generation, and (d) plain-language explanations. Token spend therefore scales with the number of problems, not the number of ads.

---

## 2. Goals and non-goals

### Goals
1. Stop budget overruns within 15 minutes (pacing check + emergency stop).
2. Rank all ads, ad sets/ad groups and keywords daily with a score that respects sample size.
3. Shift budget toward winners and pause clear losers automatically, within hard guardrails.
4. Every change is dry-run first, logged with a rollback snapshot, and evaluated against a baseline.
5. Single-client dashboard with login, showing spend vs budget, ROAS/CPA trends, rankings, change feed and approvals.
6. LLM cost is capped per account per day and visible on the dashboard.
7. Built so that merging into the main system later only swaps: auth source, credential source, and the frontend route.

### Non-goals (for v1)
- Multi-client / multi-tenant UI, billing, or signup (schema is ready for it via `client_id`, UI is not).
- TikTok, LinkedIn, or other platforms.
- Fully autonomous campaign creation. New campaigns are always proposed, never auto-launched.
- Bid-strategy modelling beyond what the platforms expose (we steer budgets, status, keywords, creatives; platforms keep doing auction bidding).
- Real-time (sub-15-minute) reaction.
- Custom attribution modelling. v1 trusts platform-reported conversions, optionally cross-checked with GA4.

---

## 3. Architecture

```mermaid
flowchart TD
    subgraph SCHED["Scheduler (APScheduler, no AI)"]
        J15["*/15 min: pacing"]
        J60["hourly: metrics pull"]
        JD["daily 06:00 IST: full review"]
        JW["weekly Mon 07:00 IST: strategy + creative"]
    end

    subgraph CONN["Connectors (engine/connectors)"]
        MM["Meta: mikusnuz/meta-ads-mcp"]
        GM["Google: kLOsk/adloop (writes) + googleads/google-ads-mcp (GAQL reads)"]
        GA4["GA4 (optional)"]
    end

    CRED["Credentials provider: get_credentials() (.env now, main backend later)"]
    CRED --> CONN

    SCHED --> CONN --> DB[("Postgres: entities, metrics, features, scores, proposals, change_log, llm_calls")]
    DB --> FEAT["Feature builder (SQL views + Python)"]
    FEAT --> RANK["Ranking engine (empirical Bayes)"]
    RANK --> RULES{"Rules engine (YAML)"}
    RULES -->|"clear-cut"| PROP["Proposal"]
    RULES -->|"within bands"| NOOP["log only, 0 tokens"]
    RULES -->|"ambiguous / anomaly"| TRI["LLM triage, small model, ~1K tokens"]
    TRI -->|"routine"| PROP
    TRI -->|"needs reasoning"| ANA["LLM analyst, large model, ~10K tokens"]
    ANA --> PROP
    TOK["Token budget controller"] -.-> TRI
    TOK -.-> ANA
    PROP --> GUARD{"Budget guardrail (code)"}
    GUARD -->|"violates hard limit"| BLOCK["block + alert"]
    GUARD -->|"small change"| EXEC["Executor: dry-run then apply"]
    GUARD -->|"big change / new campaign"| APPR["Human approval (dashboard, CLI, Slack/WhatsApp/email)"]
    APPR -->|"approved"| EXEC
    EXEC --> LOG[("change_log + rollback payload")]
    LOG --> EVAL["Evaluator (D+3 / D+7)"]
    EVAL -->|"worse"| RB["auto rollback"] --> LOG
    EVAL -->|"better"| LEARN["update thresholds / playbook"] --> RULES
    DB --> API["FastAPI + auth adapter"] --> DASH["Dashboard (Next.js, basePath /ads-autopilot)"]
    API --> CLI["CLI: ads ..."]
```

### Component responsibilities

| Component | Responsibility | AI? |
|---|---|---|
| Scheduler | Fires jobs at fixed cadences, guarantees one run at a time per job (Postgres advisory lock). | No |
| Credentials provider | Single `get_credentials(client_id, platform)` function. Reads `.env` now; calls main backend later. Nothing else reads secrets. | No |
| Connectors | Thin wrappers around the MCP servers / APIs. Normalise Meta + Google into one entity model (account → campaign → ad set/ad group → ad / keyword). Read and write methods are separate classes. | No |
| Warehouse (Postgres) | Source of truth: entity snapshots, hourly + daily metrics, features, scores, proposals, approvals, change log, evaluations, LLM call ledger. | No |
| Feature builder | Computes CPA, ROAS, CTR, CVR, CPM, frequency, spend pace, 7d vs 28d trends, fatigue signals. | No |
| Ranking engine | Empirical-Bayes shrunk CTR/CVR/ROAS, credible intervals, composite score, rank within peer group. | No |
| Rules engine | Applies YAML rules to scores/features. Outputs `noop`, `proposal`, or `escalate_to_llm`. | No |
| LLM layer | Triage (small model) and analyst/creative (large model). Receives compact JSON only. | Yes |
| Token budget controller | Daily token/cost cap per account, caching, batching, model tiering, hard stop. | No |
| Budget guardrail | Final gate on every proposal: caps, max change %, learning-phase protection, cooldowns. | No |
| Executor | Dry-run → snapshot → apply → verify read-back → write change_log. | No |
| Approval service | Creates approval requests, notifies, accepts approve/reject from dashboard/CLI/notification links, expires stale ones. | No |
| Evaluator | Compares post-change performance to baseline at D+3 and D+7, rolls back or confirms, feeds learning. | No |
| API | REST for dashboard + CLI, all behind the auth adapter. | No |
| Dashboard | Single-client UI under a configurable base path. | No |
| CLI | Operator control: status, approve, reject, pause-all, rollback, dry-run. | No |

---

## 4. Open-source building blocks

Only what was verified earlier; check each README again at Phase 0 before pinning versions.

| Repo | Licence | Role here |
|---|---|---|
| mikusnuz/meta-ads-mcp | MIT | Meta reads + writes (insights, budgets, statuses, creatives, rules; 134 tools). Primary Meta connector. |
| kLOsk/adloop | MIT | Google Ads writes using draft → preview → confirm (budgets, bids, pause, keywords, negatives, RSAs, PMax assets). README states a Google Cloud project + Ads API + OAuth client, no developer token. |
| googleads/google-ads-mcp | Apache-2.0 | Official Google server, read-mostly (GAQL). Used for reporting pulls; cannot change budgets/bids/keywords. |
| logly/mureo | Apache-2.0 | Reference skills for budget-rebalance, creative-refresh, search-term-cleanup, rollback. Used as prompt/playbook source for the LLM layer, not as the runtime core. |
| attainmentlabs/meta-ads-mcp | MIT | Reference safety model: dry-run, budget caps, audit log, create-paused. We copy the pattern into our guardrail/executor. |

Connector strategy: the engine talks to connectors through a Python interface (`ReadConnector`, `WriteConnector`). Phase 1 can call the MCP servers as subprocesses (stdio MCP client) or call the underlying APIs directly if MCP overhead is a problem. Swapping one for the other must not touch rules/ranking code.

---

## 5. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Engine | Python 3.12 | Best ad-API + data tooling; MCP client libraries exist; same language for stats. |
| Scheduling | APScheduler (in-process) + Postgres advisory locks | One process, no extra infra. Swap to Celery/RQ only if jobs outgrow one box. |
| Data | Postgres 16 | Relational + JSONB for raw payloads and rollback snapshots; easy to merge into main system's DB later. |
| Migrations | Alembic | Standard with SQLAlchemy. |
| ORM / queries | SQLAlchemy 2 + raw SQL views for features | Features are easier to audit as SQL. |
| Stats | numpy, scipy | Beta/Gamma posteriors, quantiles. |
| API | FastAPI + Pydantic v2 | Typed, fast, auto OpenAPI for the dashboard and later the main system. |
| Dashboard | Next.js (App Router) + TypeScript + Recharts + Tailwind | `basePath` config gives route-based deployment for free. |
| CLI | Typer | Shares Pydantic models with the API; calls the API, not the DB. |
| LLM client | Provider-agnostic wrapper (`engine/llm/client.py`) | Model names come from env; prompt caching and batch used where the provider supports it. |
| Notifications | Slack webhook / email (SMTP) / WhatsApp (provider TBD) | Behind one `notify()` interface. |
| Packaging | Docker + docker-compose | One command: postgres, engine, api, dashboard. |
| Observability | structlog JSON logs, Prometheus metrics endpoint, optional Sentry | Cheap, standard. |
| Tests | pytest, hypothesis (guardrail properties), Playwright (dashboard smoke) | Guardrails need property tests. |

---

## 6. Repo structure

```
ads-autopilot/
├── engine/
│   ├── main.py                  # starts scheduler
│   ├── config.py                # pydantic-settings, reads env
│   ├── credentials/
│   │   ├── base.py              # CredentialsProvider interface
│   │   ├── env_provider.py      # now
│   │   └── remote_provider.py   # later: main backend
│   ├── connectors/
│   │   ├── base.py              # ReadConnector / WriteConnector + normalised models
│   │   ├── meta.py              # mikusnuz/meta-ads-mcp
│   │   ├── google_read.py       # googleads/google-ads-mcp (GAQL)
│   │   ├── google_write.py      # kLOsk/adloop
│   │   └── ga4.py               # optional
│   ├── ingest/                  # pull + upsert entities and metrics
│   ├── features/                # SQL views + python feature builders
│   ├── ranking/                 # empirical bayes, composite score
│   ├── rules/
│   │   ├── engine.py
│   │   └── rules.yaml           # thresholds (versioned)
│   ├── guardrails/
│   │   ├── budget.py
│   │   └── limits.yaml
│   ├── executor/                # dry-run, snapshot, apply, verify
│   ├── approvals/
│   ├── evaluation/
│   ├── llm/
│   │   ├── client.py
│   │   ├── budget.py            # token budget controller
│   │   ├── summariser.py        # rows -> compact JSON
│   │   └── prompts/             # triage.md, analyst.md, creative.md (seeded from mureo skills)
│   ├── notify/
│   └── jobs/                    # pacing.py, hourly.py, daily.py, weekly.py, evaluate.py
├── api/
│   ├── main.py
│   ├── auth/
│   │   ├── adapter.py           # get_current_user() — single entry point
│   │   ├── env_auth.py          # ADMIN_USER / ADMIN_PASSWORD_HASH -> JWT cookie
│   │   └── main_system_auth.py  # later: verify main-system JWT/session
│   └── routes/
├── cli/
│   └── ads.py
├── dashboard/                   # Next.js, basePath = NEXT_PUBLIC_BASE_PATH
├── db/
│   ├── alembic.ini
│   └── migrations/
├── tests/
│   ├── unit/ ├── property/ ├── integration/ └── fixtures/
├── docs/
│   └── BUILD_PLAN.md
├── .env.example
├── docker-compose.yml
└── Makefile
```

---

## 7. `.env.example`

```dotenv
# --- App ---
APP_ENV=development                 # development | staging | production
CLIENT_ID=client_001
CLIENT_NAME="Client Name"
TIMEZONE=Asia/Kolkata
ACCOUNT_CURRENCY=INR
MODE=suggest                        # read_only | suggest | auto_small | auto
KILL_SWITCH=false                   # true = executor refuses all writes

# --- Auth (standalone) ---
ADMIN_USER=admin
ADMIN_PASSWORD_HASH=                # argon2/bcrypt hash, never plain text
JWT_SECRET=                         # 32+ random bytes
SESSION_TTL_MINUTES=720
AUTH_MODE=env                       # env | main_system
MAIN_SYSTEM_JWKS_URL=               # later

# --- Credentials source ---
CREDENTIALS_PROVIDER=env            # env | remote
MAIN_BACKEND_URL=                   # later
MAIN_BACKEND_SERVICE_TOKEN=         # later

# --- Meta ---
META_APP_ID=
META_APP_SECRET=
META_ACCESS_TOKEN=                  # long-lived, ads_read + ads_management
META_AD_ACCOUNT_ID=act_XXXXXXXXXX
META_API_VERSION=                   # pin explicitly

# --- Google Ads ---
GOOGLE_ADS_CUSTOMER_ID=XXXXXXXXXX
GOOGLE_ADS_LOGIN_CUSTOMER_ID=       # MCC id if applicable
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
GOOGLE_OAUTH_REFRESH_TOKEN=
GOOGLE_CLOUD_PROJECT_ID=
GOOGLE_ADS_DEVELOPER_TOKEN=         # only if the chosen connector needs it

# --- GA4 (optional) ---
GA4_PROPERTY_ID=
GA4_SERVICE_ACCOUNT_JSON_PATH=

# --- LLM ---
LLM_PROVIDER=
LLM_API_KEY=
LLM_SMALL_MODEL=
LLM_LARGE_MODEL=
LLM_DAILY_TOKEN_CAP=100000          # per account
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

# --- Frontend ---
NEXT_PUBLIC_BASE_PATH=/ads-autopilot
NEXT_PUBLIC_API_URL=/ads-autopilot/api
```

---

## 8. Postgres schema

Conventions: every table has `client_id TEXT NOT NULL`, `created_at TIMESTAMPTZ DEFAULT now()`. Platform ids are stored as text with a `platform` enum (`meta`, `google`). Money is `NUMERIC(14,2)` in account currency. Raw API payloads go in `raw JSONB`.

| Table | Key columns |
|---|---|
| `clients` | `client_id PK`, `name`, `timezone`, `currency`, `mode`, `kill_switch BOOL` |
| `ad_accounts` | `id PK`, `client_id`, `platform`, `external_id`, `name`, `status`, `currency`, `last_synced_at` |
| `campaigns` | `id PK`, `client_id`, `ad_account_id FK`, `platform`, `external_id UNIQUE(platform,external_id)`, `name`, `objective`, `status`, `daily_budget`, `lifetime_budget`, `bid_strategy`, `is_cbo BOOL`, `managed BOOL` (opt-in to automation), `raw` |
| `ad_groups` | `id PK`, `client_id`, `campaign_id FK`, `platform`, `external_id`, `kind` (`ad_set`/`ad_group`), `name`, `status`, `daily_budget`, `optimization_goal`, `learning_status`, `learning_status_at`, `raw` |
| `ads` | `id PK`, `client_id`, `ad_group_id FK`, `platform`, `external_id`, `name`, `status`, `format`, `creative_id`, `headline`, `body`, `cta`, `first_live_at`, `raw` |
| `keywords` | `id PK`, `client_id`, `ad_group_id FK`, `external_id`, `text`, `match_type`, `status`, `is_negative BOOL`, `cpc_bid` |
| `search_terms` | `id PK`, `client_id`, `ad_group_id FK`, `term`, `date`, `impressions`, `clicks`, `cost`, `conversions`, `conv_value` |
| `metrics_hourly` | `PK(client_id, platform, entity_type, entity_id, ts_hour)`, `impressions`, `clicks`, `spend`, `conversions`, `conv_value`, `reach`, `frequency`, `pulled_at` |
| `metrics_daily` | `PK(client_id, platform, entity_type, entity_id, date)`, same metric columns + `video_views`, `is_final BOOL` (attribution window closed) |
| `features` | `PK(client_id, entity_type, entity_id, as_of)`, `ctr_7d`, `cvr_7d`, `cpa_7d`, `roas_7d`, `cpa_28d`, `roas_28d`, `trend_cpa`, `trend_ctr`, `frequency_7d`, `spend_pace`, `days_live`, `fatigue_flag` |
| `scores` | `PK(client_id, entity_type, entity_id, as_of)`, `ctr_post_mean`, `cvr_post_mean`, `cvr_p10`, `cvr_p90`, `roas_shrunk`, `roas_p10`, `p_beats_target`, `composite`, `rank_in_peer`, `peer_group`, `data_sufficient BOOL` |
| `rules` | `id PK`, `client_id`, `rule_key`, `version`, `yaml JSONB`, `active BOOL`, `changed_by`, `reason` |
| `budgets` | `id PK`, `client_id`, `scope` (`account`/`campaign`/`platform`), `scope_id`, `daily_cap`, `monthly_cap`, `max_daily_change_pct`, `effective_from`, `effective_to` |
| `proposals` | `id PK`, `client_id`, `source` (`rule`/`llm_triage`/`llm_analyst`/`human`), `rule_key`, `entity_type`, `entity_id`, `action` (`pause`,`enable`,`budget_change`,`add_negative`,`new_ad`,`new_campaign`,...), `params JSONB`, `expected_impact JSONB`, `reason TEXT`, `risk` (`low`/`medium`/`high`), `status` (`pending`,`blocked`,`awaiting_approval`,`approved`,`rejected`,`executed`,`expired`,`failed`), `guardrail_result JSONB`, `expires_at` |
| `approvals` | `id PK`, `client_id`, `proposal_id FK`, `channel`, `requested_at`, `decided_at`, `decision`, `decided_by`, `comment`, `token_hash` (one-time link) |
| `change_log` | `id PK`, `client_id`, `proposal_id FK`, `platform`, `entity_type`, `entity_id`, `action`, `before JSONB`, `after JSONB`, `rollback_payload JSONB`, `dry_run_result JSONB`, `applied_at`, `applied_by` (`engine`/user), `verify_status`, `rolled_back_at`, `rollback_of FK NULL` |
| `evaluations` | `id PK`, `client_id`, `change_id FK`, `horizon_days` (3/7), `baseline JSONB`, `observed JSONB`, `metric`, `delta_pct`, `p_better`, `verdict` (`better`,`neutral`,`worse`,`insufficient_data`), `action_taken` |
| `llm_calls` | `id PK`, `client_id`, `job`, `tier` (`small`/`large`), `model`, `purpose`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `cost_usd NUMERIC(10,5)`, `latency_ms`, `prompt_hash`, `result_ref`, `status` |
| `alerts` | `id PK`, `client_id`, `severity` (`info`/`warn`/`critical`), `kind`, `entity_type`, `entity_id`, `message`, `sent_channels TEXT[]`, `acked_at`, `acked_by` |
| `audit_log` | `id PK`, `client_id`, `actor`, `action`, `target`, `payload JSONB`, `ip`, `at` (append-only; covers logins, rule edits, approvals, kill switch) |
| `job_runs` | `id PK`, `client_id`, `job`, `started_at`, `finished_at`, `status`, `rows_ingested`, `error` |

Indexes: `(client_id, entity_id, date DESC)` on metrics and features; `(client_id, status)` on proposals; `(client_id, applied_at)` on change_log. Partition `metrics_hourly` by month if it grows; keep 90 days hourly, aggregate older into daily.

---

## 9. Jobs and schedulers

All times IST. Each job takes a Postgres advisory lock, writes a `job_runs` row, and is idempotent (re-running the same window upserts, never duplicates).

| Job | Cadence | Does | LLM |
|---|---|---|---|
| `pacing` | every 15 min | Pull today's spend per campaign (cheap insights call). Compute `pace = spend_today / (daily_budget × elapsed_fraction_of_day)`. If pace > 1.30 on a managed campaign or account daily cap reached → emergency action (see §12). Alert on pace > 1.15. | 0 |
| `hourly_pull` | hourly :05 | Upsert hourly metrics for all active entities, refresh entity statuses/budgets, detect external edits (changes not in change_log → flag "manual change", start cooldown). | 0 |
| `daily_review` | 06:00 | 1) Re-pull last 7 days daily metrics (attribution settles). 2) Build features. 3) Score + rank. 4) Run rules → proposals. 5) Escalate ambiguous to LLM triage within budget. 6) Guardrail all proposals. 7) Auto-execute allowed small ones (by mode). 8) Send approvals for big ones. 9) Send daily digest. | small, few calls |
| `evaluate` | daily 06:30 | For each change at D+3 and D+7: compute verdict vs baseline; auto-rollback "worse"; mark "better" and feed learning stats. | 0 |
| `search_terms` | daily 07:00 (Google) | Pull search terms report; flag terms with spend ≥ 1× target CPA and 0 conversions, or irrelevant n-grams → negative keyword proposals. | optional small batch for relevance |
| `weekly_strategy` | Mon 07:00 | Weekly report; detect fatigue; LLM analyst on top movers; creative refresh (new headlines/primary text for fatigued ads, as paused drafts needing approval); recalibrate priors and thresholds from evaluation results. | large, 1–3 calls |
| `approval_expiry` | every 15 min | Expire approvals older than 24 h (configurable); re-notify once at 12 h. | 0 |
| `token_reset` | 00:00 | Reset daily token counters, roll monthly totals. | 0 |
| `retention` | daily 03:00 | Roll hourly older than 90 days into daily, vacuum. | 0 |

API rate limits: connectors use per-platform token buckets and exponential backoff; the 15-minute job pulls only account + campaign level spend.

---

## 10. Ranking method

Goal: rank entities so a tiny lucky sample never beats a large proven one.

### 10.1 Peer groups
Compare like with like: Meta ads within the same ad set (and ad sets within campaign); Google keywords within ad group and match type; Google ads within ad group. Prior is built per peer group's parent level (campaign) over the last 28 days.

### 10.2 CTR and CVR — Beta-Binomial with empirical-Bayes prior

For an entity with impressions `n`, clicks `c`, conversions `v`:

- Prior mean `m` = parent-level rate over 28 days. Prior strength `k` (pseudo-trials): CTR `k = 2000` impressions, CVR `k = 50` clicks (start values; fit by method of moments per campaign once there is enough data).
- `α0 = m·k`, `β0 = (1 − m)·k`
- CTR posterior: `Beta(α0 + c, β0 + n − c)`
- CVR posterior (trials = clicks): `Beta(α0' + v, β0' + c − v)`
- Posterior mean = `α / (α + β)`; credible interval via `scipy.stats.beta.ppf([0.1, 0.9])`.
- `p_beats_target = P(CVR > target_CVR)` where `target_CVR = CPC_7d / target_CPA`.

### 10.3 CPA and ROAS with shrinkage

- Shrunk conversions: `v̂ = c · E[CVR]`; `CPA_shrunk = spend / max(v̂, ε)`.
- ROAS: `ROAS_shrunk = (conv_value + k_r · ROAS_parent) / (spend + k_r)` with `k_r` = 1 × target CPA in spend-equivalent (i.e. the parent's ROAS counts as one target-CPA worth of spend).
- ROAS interval: Gamma-Poisson on conversions (`v ~ Poisson(λ·spend)`, `λ ~ Gamma(a0 + v, b0 + spend)`) × mean order value (shrunk to parent AOV) → `roas_p10`, `roas_p90`.

### 10.4 Composite score (0–100)
Per objective set in `rules.yaml`:
- Conversion/ROAS campaigns: `composite = 100 × P(ROAS > target_ROAS)` (or `P(CPA < target_CPA)` if no value tracking), with a −10 point penalty when `fatigue_flag` is set.
- Traffic campaigns: `composite = 100 × P(CTR > parent_CTR)`.

### 10.5 Minimum data before judging (`data_sufficient`)
| Entity | Minimum |
|---|---|
| Meta ad | ≥ 1,000 impressions AND spend ≥ 1× target CPA AND live ≥ 72 h |
| Meta ad set | out of learning phase (as reported by Meta) OR spend ≥ 3× target CPA |
| Google keyword | ≥ 100 clicks OR spend ≥ 2× target CPA |
| Google ad (RSA) | ≥ 1,000 impressions in the ad group comparison window |
Entities below minimum are ranked with a "low data" tag and never auto-paused.

### 10.6 Budget allocation (weekly + daily)
Thompson sampling across ad sets/campaigns in the same pool: draw 1,000 samples of ROAS (or 1/CPA) from posteriors, share of wins = target budget share. Move toward target share by at most the guardrail limit per day (§12).

---

## 11. Rules engine

Rules are YAML, versioned in the `rules` table and in git. Each rule returns `noop`, `proposal(action, params, risk)`, or `escalate(reason)`. Evaluation order: kill rules → protection rules → optimisation rules → anomaly rules.

```yaml
version: 1
targets:
  currency: INR
  target_cpa: 500          # set per client
  target_roas: 3.0
  objective: conversions   # conversions | value | traffic

data_minimums:
  meta_ad:        { impressions: 1000, spend_x_cpa: 1.0, hours_live: 72 }
  meta_ad_set:    { spend_x_cpa: 3.0, require_out_of_learning: true }
  google_keyword: { clicks: 100, spend_x_cpa: 2.0 }

rules:
  - key: pause_clear_loser_ad
    when: "data_sufficient and spend_7d >= 2 * target_cpa and conversions_7d == 0"
    action: pause
    risk: low

  - key: pause_high_cpa_ad
    when: "data_sufficient and p_cpa_below_target < 0.10 and cpa_shrunk > 2.0 * target_cpa"
    action: pause
    risk: low
    constraints: { keep_min_active_ads_per_group: 2 }

  - key: scale_winner_adset
    when: "data_sufficient and p_beats_target > 0.80 and spend_pace < 1.0 and not learning"
    action: budget_change
    params: { change_pct: +15 }
    risk: low

  - key: trim_underperformer_adset
    when: "data_sufficient and p_beats_target < 0.20 and not learning"
    action: budget_change
    params: { change_pct: -15 }
    risk: low

  - key: creative_fatigue
    when: "frequency_7d > 3.5 and ctr_7d < 0.7 * ctr_28d and days_live >= 14"
    action: escalate
    reason: fatigue            # -> LLM creative refresh proposal (needs approval)

  - key: negative_keyword_candidate
    when: "platform == 'google' and entity == 'search_term' and cost_30d >= 1.0 * target_cpa and conversions_30d == 0"
    action: add_negative
    params: { match_type: exact }
    risk: medium

  - key: cpa_spike_anomaly
    when: "cpa_1d > 2.0 * cpa_28d and spend_1d >= 1.0 * target_cpa"
    action: escalate
    reason: anomaly

  - key: ctr_collapse_anomaly
    when: "ctr_1d < 0.5 * ctr_28d and impressions_1d >= 2000"
    action: escalate
    reason: anomaly

  - key: within_band
    when: "true"
    action: noop

cooldowns:
  same_entity_hours: 72        # no second change to an entity within 72 h
  after_manual_change_hours: 48
```

Expressions are evaluated with a safe expression library (e.g. `simpleeval`), never `eval`.

---

## 12. Budget guardrail logic

Every proposal (rule, LLM, or human via API) passes through `guardrails.check(proposal) -> Allow | NeedsApproval | Block(reason)`.

```yaml
# guardrails/limits.yaml
account:
  daily_cap: 20000            # account currency
  monthly_cap: 550000
platform_split_max_shift_pct_per_week: 20
campaign:
  max_daily_budget: 8000
  min_daily_budget: 300
change:
  max_budget_change_pct_per_day: 20     # cumulative per entity per day, either direction
  auto_max_budget_change_pct: 15        # above this -> approval
  auto_max_abs_change: 2000             # currency; above -> approval
  max_pauses_per_day_auto: 5
  keep_min_active_ads_per_group: 2
learning_phase:
  block_budget_changes: true
  block_pauses_of_adsets: true
pacing:
  alert_pace: 1.15
  emergency_pace: 1.30
  emergency_action: cap_to_remaining   # set budgets so projected spend <= cap; pause if already at cap
approval_required_for:
  - new_campaign
  - new_ad
  - enable_paused_campaign
  - bid_strategy_change
  - any_change_over_auto_limits
  - add_negative                       # until 2 weeks of clean history, then auto
```

Check order:
1. `KILL_SWITCH` or `MODE=read_only` → Block. `MODE=suggest` → everything becomes NeedsApproval.
2. Entity not `managed=true` → Block.
3. Cooldown active → Block.
4. Learning-phase protection → Block.
5. Projected account spend today/month after change > cap → Block (+ alert).
6. Cumulative change today > `max_budget_change_pct_per_day` → clamp to limit, or Block if clamped value is below a minimum useful step (5%).
7. Campaign min/max budget → clamp.
8. Action in `approval_required_for` or exceeds auto limits → NeedsApproval.
9. Otherwise → Allow.

Emergency stop (pacing job) is the only path that may reduce budgets beyond 20% or pause campaigns without approval, and it always alerts as `critical`.

Guardrails are pure functions with property tests (§20): for any random proposal sequence, total projected spend never exceeds caps and no entity changes more than 20% per day.

---

## 13. LLM layer and token economics

### 13.1 When the LLM is called
| Trigger | Tier | Typical tokens (in / out) | Frequency |
|---|---|---|---|
| Rule `escalate` (anomaly, ambiguous) | small | 1,000 / 200 | 5–15 per day, batched into 1–3 requests |
| Triage says "needs reasoning" | large | 10,000 / 2,000 | 0–2 per day |
| Creative refresh (fatigue) | large | 30,000 / 5,000 | weekly |
| Daily digest narrative | small | 2,000 / 500 | daily |
| Search-term relevance (optional) | small | 1,500 / 300 per batch of 50 terms | daily |
Never called for: pulling data, computing metrics, scoring, enforcing budgets, or executing changes.

### 13.2 Prompt input schema (compact JSON, no raw rows)
```json
{
  "client": {"objective": "conversions", "target_cpa": 500, "currency": "INR"},
  "case": {
    "id": "esc_2026_11_11_07",
    "reason": "cpa_spike_anomaly",
    "entity": {"platform": "meta", "type": "ad_set", "id": "123", "name": "Retargeting 30d", "learning": false},
    "metrics": {
      "d1":  {"spend": 4200, "conv": 2, "cpa": 2100, "ctr": 0.011, "freq": 4.1},
      "d7":  {"spend": 21000, "conv": 30, "cpa": 700, "ctr": 0.014},
      "d28": {"cpa": 520, "ctr": 0.016}
    },
    "score": {"p_beats_target": 0.18, "cvr_p10": 0.021, "cvr_p90": 0.034},
    "recent_changes": [{"at": "2026-11-09", "action": "budget_change", "pct": 15}],
    "peer_context": {"rank": 4, "of": 5}
  },
  "allowed_actions": ["noop", "pause", "budget_change(-20..+20)", "escalate_large", "flag_human"],
  "output_schema": {"action": "string", "params": "object", "confidence": "0-1", "reason": "<=40 words"}
}
```
The model must answer in JSON matching `output_schema`; anything outside `allowed_actions` is discarded. LLM output is only ever a proposal and still goes through the guardrail.

### 13.3 Token controls
- Summariser sends top movers only (max 20 entities per call), rounded numbers, short keys.
- Static system prompt + playbook (from mureo skill text) placed first so it is prompt-cached; per-case JSON last.
- Batch: all triage cases from one job in one request (array of cases).
- Use the provider's batch API for the weekly job (not time-sensitive).
- Cache by `prompt_hash`: identical case within 24 h → reuse result, 0 tokens.
- Token budget controller: before each call, `if used_today + estimate > LLM_DAILY_TOKEN_CAP or cost_month + est_cost > LLM_MONTHLY_COST_CAP_USD: skip, mark case "needs human", alert`. Priority order when near the cap: anomaly triage > analyst > digest > creative.
- Every call recorded in `llm_calls` with tokens and cost; dashboard shows daily/monthly totals.

### 13.4 Monthly cost estimate (estimate, not a quote)
Assumptions: 30-day month; triage 10 cases/day at 1,000 in / 200 out; analyst 1/day at 10,000 / 2,000; weekly creative 4.3 runs at 30,000 / 5,000; digest daily at 2,000 / 500; no caching or batch discount applied (real cost lower). Example prices use Anthropic's published list prices checked 11 Nov 2026 at https://docs.claude.com/en/docs/about-claude/pricing: small tier Claude Haiku 4.5 at $1 / MTok input and $5 / MTok output; large tier Claude Sonnet 4.6 at $3 / MTok input and $15 / MTok output. Any provider works; plug its prices into the same formula.

| Workload | Tokens / month (in / out) | Est. cost (USD) |
|---|---|---|
| Triage (small) | 300K / 60K | 0.60 |
| Digest (small) | 60K / 15K | 0.14 |
| Analyst (large) | 300K / 60K | 1.80 |
| Weekly creative (large) | 129K / 21.5K | 0.71 |
| Total | 789K / 156.5K | 3.25 |

Formula: `cost = Σ (in_tokens × in_price + out_tokens × out_price) / 1,000,000`.
Ceiling: with `LLM_DAILY_TOKEN_CAP=100000` the hard maximum is 3M tokens/month; all on the large tier at roughly 85% input / 15% output that is about $14.40, so `LLM_MONTHLY_COST_CAP_USD=20` is a safe ceiling.

---

## 14. Approval flow

1. Guardrail returns NeedsApproval → `proposals.status = awaiting_approval`, create `approvals` row with a one-time token (hash stored).
2. Notify on configured channels: summary line, before → after, reason, expected impact, links "Approve" / "Reject" (deep link to dashboard `…/approvals/{id}?t=…`, which still requires login unless the signed link mode is explicitly enabled).
3. Decision via dashboard, CLI (`ads approve 42`), or notification link. Write `audit_log`.
4. On approve: re-run guardrail (state may have changed) → executor.
5. Expiry: 24 h default; expired proposals are never executed.
6. Batch approvals: dashboard allows approving a group (e.g. all negative keywords from today's run).

---

## 15. Executor

Per proposal:
1. Read current state from the platform (not from DB) → `before`.
2. Build `rollback_payload` (exact inverse: previous budget, previous status, remove added negative, etc.).
3. Dry-run: Google via adloop preview step; Meta via validate-only/dry-run where the connector supports it, otherwise a local simulation that validates params and limits.
4. Apply via write connector. New ads/campaigns are always created PAUSED.
5. Read back and verify `after` matches intent; mismatch → mark `verify_status=failed`, alert, attempt rollback.
6. Write `change_log`; schedule evaluations at D+3 and D+7.

---

## 16. Evaluation and rollback

- Baseline: the entity's 7 days before the change (same weekdays), adjusted by the parent-level trend over the same post-period (difference-in-differences against unchanged peers in the same campaign). Holdout: for budget moves, unchanged sibling ad sets act as control.
- Primary metric: CPA (or ROAS for value campaigns). Guard metric: spend delivery (budget increases must actually spend).
- Verdict at D+3 (early warning) and D+7 (final):
  - `worse` if `P(metric_after worse than baseline by > 15%) > 0.8` → auto-rollback (subject to guardrails; rollback is always allowed to reduce spend).
  - `better` if `P(better by > 5%) > 0.7`.
  - else `neutral`; `insufficient_data` if under minimums (extend to D+14 once).
- Learning: per `rule_key`, keep win/loss counts. If a rule's rollback rate > 40% over ≥ 10 changes, auto-tighten its threshold one step and alert; if win rate > 70% over ≥ 20 changes, propose (not apply) loosening. Weekly LLM analyst reads this table to suggest playbook edits, always as proposals.

---

## 17. API endpoints (FastAPI, prefix `/api/v1`, all behind `get_current_user`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login` | env login → httpOnly JWT cookie |
| POST | `/auth/logout` | |
| GET | `/auth/me` | |
| GET | `/overview?range=7d` | spend vs budget, CPA/ROAS, pace, alerts count |
| GET | `/campaigns` · `/campaigns/{id}` | entities with latest metrics + score |
| GET | `/ad-groups/{id}` · `/ads/{id}` · `/keywords?ad_group_id=` | |
| GET | `/metrics?entity_type=&entity_id=&grain=day|hour&from=&to=` | time series |
| GET | `/rankings?level=ad|ad_group|keyword&platform=` | ranked table with intervals |
| GET | `/proposals?status=` · POST `/proposals/{id}/approve` · `/reject` | |
| POST | `/proposals` | manual proposal (still guardrailed) |
| GET | `/changes` · POST `/changes/{id}/rollback` | change feed, manual rollback |
| GET | `/evaluations?change_id=` | |
| GET/PUT | `/rules` · `/limits` | view/edit YAML (validated, versioned, audited) |
| GET | `/llm/usage?range=30d` | tokens and cost |
| GET | `/alerts` · POST `/alerts/{id}/ack` | |
| POST | `/control/mode` | read_only / suggest / auto_small / auto |
| POST | `/control/kill-switch` | on/off |
| POST | `/control/pause-all` | emergency pause of managed campaigns (approval-free, audited) |
| POST | `/jobs/{name}/run` | manual trigger |
| GET | `/health` · `/metrics` | liveness, Prometheus |

---

## 18. Dashboard pages and charts

Next.js with `basePath: process.env.NEXT_PUBLIC_BASE_PATH`, all fetches relative to `NEXT_PUBLIC_API_URL`.

| Page | Content |
|---|---|
| Login | user + password (env admin) |
| Overview | spend today vs cap (gauge), month-to-date vs monthly cap, pace line, CPA and ROAS 28-day line with target line, Meta vs Google split, open approvals, critical alerts, current mode + kill switch |
| Campaigns | table: status, budget, spend 7d, CPA, ROAS, composite score, learning flag; drill-down to ad sets/ad groups and ads |
| Rankings | per level: ranked table with score and credible-interval bars, low-data tag, filters by platform/campaign |
| Creatives | ad thumbnails/text, CTR trend, frequency, fatigue flag, generated draft variants awaiting approval |
| Keywords & search terms | Google keyword table, negative keyword candidates, wasted spend total |
| Approvals | pending queue with before → after, reason, approve/reject, batch approve |
| Change log | feed with evaluation verdicts (better/neutral/worse), rollback button |
| Budgets & rules | caps, limits, rules YAML viewer/editor with validation and diff |
| AI usage | tokens/day by tier, cost MTD vs cap, calls by purpose |
| Alerts | list, ack |
| System | job runs, last sync times, connector health |

---

## 19. CLI

`ads` (Typer), talks to the API using a token from `ads login`.

| Command | Does |
|---|---|
| `ads login` | store session token locally (0600 file) |
| `ads status` | mode, kill switch, spend vs caps, pace, open approvals, last job runs |
| `ads proposals [--status pending]` | list |
| `ads approve <id> [...]` / `ads reject <id> --reason` | |
| `ads changes [--since 7d]` | change feed with verdicts |
| `ads rollback <change_id>` | |
| `ads pause-all [--platform meta|google] --confirm` | emergency pause of managed campaigns |
| `ads mode <read_only|suggest|auto_small|auto>` | |
| `ads kill-switch <on|off>` | |
| `ads run <job>` | trigger job now |
| `ads rankings --level ad --top 20` | |
| `ads dry-run <proposal_id>` | show what would be sent to the platform |
| `ads llm-usage` | tokens/cost today and MTD |

---

## 20. Security

- Secrets only in `.env` (gitignored) or the secret store of the host; `.env.example` has no values. Pre-commit `gitleaks` scan.
- Only the credentials provider reads secrets; connectors receive short-lived objects, logs redact tokens.
- Admin password stored as argon2/bcrypt hash; JWT in httpOnly, Secure, SameSite=Lax cookie; login rate-limited; CSRF token on state-changing requests.
- Least privilege: Meta system-user token with only `ads_read` + `ads_management` on the one ad account; Google OAuth scoped to Ads only on the one customer id. A separate read-only credential set for `MODE=read_only`.
- Write paths gated by mode + kill switch + guardrail; no endpoint can bypass the guardrail except `pause-all` (spend-reducing only).
- `audit_log` is append-only (DB role without UPDATE/DELETE on that table).
- HTTPS via reverse proxy; Postgres not exposed publicly; daily encrypted backups.
- LLM: no PII or tokens in prompts; only aggregated metrics and ad text.

---

## 21. Observability

- Structured JSON logs with `job`, `client_id`, `proposal_id`, `change_id`.
- Prometheus metrics: job duration/success, API errors by connector, rows ingested, proposals by status, guardrail blocks by reason, changes applied, rollbacks, LLM tokens and cost, spend pace.
- Alerts (to Slack/email): job failed twice in a row, data stale > 2 h, token expiring within 7 days, pace > 1.30, rollback executed, LLM cap hit, verify failure.
- Daily digest (06:15): yesterday's spend vs cap, CPA/ROAS vs target, top 3 winners/losers, changes made, pending approvals, LLM cost.

---

## 22. Testing strategy

| Level | What |
|---|---|
| Unit | feature formulas, Beta/Gamma posteriors against known values, rule expressions, summariser token size |
| Property (hypothesis) | guardrails: caps never exceeded, ≤20%/day per entity, learning entities never touched, kill switch blocks all writes |
| Contract | recorded connector responses (fixtures) to catch API/MCP schema changes |
| Replay / backtest | run rules over 60–90 days of historical data; compare proposals with what a human would have done; tune thresholds before going live |
| Integration | Google Ads test account (Google offers test accounts for the Ads API); Meta: a dedicated low-budget test campaign kept PAUSED, plus dry-run paths |
| Staged rollout | `read_only` → `suggest` (≥ 2 weeks, compare proposals vs outcomes) → `auto_small` on 1–2 opted-in campaigns → wider |
| Dashboard | Playwright smoke: login, overview loads, approve flow, base-path routing |
| LLM | golden cases: fixed JSON inputs with expected action class; fail CI on invalid JSON or disallowed action |

---

## 23. Phased milestones

| Phase | Duration | Deliverables | Exit criteria |
|---|---|---|---|
| 0. Setup | 3–4 days | repo, docker-compose, Postgres + Alembic, `.env.example`, credentials provider, Meta app + long-lived token, Google Cloud project + Ads API + OAuth, connector smoke tests, CI | both platforms readable from a script |
| 1. Read-only ingestion + dashboard | 1.5–2 weeks | entities + hourly/daily ingestion, pacing job (alert only), env login, Overview/Campaigns/System pages, CLI `status` | 7 days of clean data, dashboard numbers match Ads Managers within 2% |
| 2. Scoring + suggest mode | 1.5 weeks | features, ranking, rules YAML, guardrails, proposals, approvals UI/CLI, daily digest | 2 weeks of proposals reviewed; ≥ 80% judged sensible by Govinda |
| 3. Guarded auto-actions (Meta first) | 1.5 weeks | executor with dry-run/snapshot/verify, evaluator + auto-rollback, emergency stop, `auto_small` mode, change log page | 2 weeks live on opted-in campaigns, no guardrail breach, rollbacks working |
| 4. LLM layer | 1 week | summariser, triage + analyst, token controller, creative drafts (paused, approval), AI usage page | LLM cost within cap; triage decisions agree with human on golden set |
| 5. Google parity | 1.5 weeks | adloop writes, keyword + search-term rules, negatives flow, RSA asset refresh, Google rankings | same exit criteria as Phase 3 for Google |
| 6. Integration into main system | 1 week | `main_system_auth`, `remote_provider` for credentials, route-based frontend deployment, DB migration plan | runs under main domain at `/ads-autopilot` with main-system login |

Total: about 9–11 weeks of one developer; Phases 4 and 5 can run in parallel with two people.

---

## 24. Integration into the main system

Only three seams change; everything else is untouched.

1. **Auth adapter** — `AUTH_MODE=main_system`: `get_current_user()` verifies the main system's JWT (JWKS URL) or session cookie and maps roles (`viewer`, `approver`, `admin`). Env login is disabled.
2. **Credentials provider** — `CREDENTIALS_PROVIDER=remote`: `get_credentials(client_id, platform)` calls the main backend's credentials endpoint with a service token; results cached in memory for a short TTL, never written to disk. Token refresh stays the main backend's responsibility.
3. **Frontend route** — the main system's router/Nginx proxies `/ads-autopilot/*` to the dashboard container and `/ads-autopilot/api/*` to the API. No code change because `basePath` is already configurable. Option later: embed pages as a micro-frontend.

Data: keep a separate Postgres schema `ads_autopilot` (or separate DB) at first; `client_id` everywhere means turning on more clients is a config change. Multi-client later needs: per-client scheduler fan-out, per-client token caps (already keyed by client), and client selector in UI.

Main-system team needs to provide: JWT format/JWKS, role names, credentials endpoint contract, route prefix, and where notifications should go.

---

## 25. Risks and open questions

### Risks
| Risk | Mitigation |
|---|---|
| Attribution delay makes yesterday's CPA look bad | re-pull 7 days daily; judge on settled windows; D+7 final verdict |
| Resetting Meta learning phase with frequent edits | ≤ 20%/day, 72 h cooldown, learning protection |
| MCP server breaking changes / abandonment | pin versions, contract tests, connector interface allows switching to direct API |
| Token expiry stops the system silently | expiry monitor + alert 7 days ahead; data-staleness alert |
| Rule over-fitting / bad thresholds | backtest, suggest-mode period, auto-tightening on rollback rate |
| LLM hallucinated actions | JSON schema, allowed-actions whitelist, guardrail after LLM |
| Manual edits in Ads Manager conflict with engine | detect external changes hourly, 48 h cooldown |
| Small account = little data | stronger priors, longer windows, more "low data" holds |

### Open questions for Govinda
1. Target CPA / ROAS per campaign, and which conversion event counts (purchase, lead)?
2. Account daily and monthly caps, and currency?
3. Which campaigns are opted in to automation first?
4. Is revenue value tracked on both platforms (needed for ROAS)? Use GA4 as cross-check?
5. Approval channel preference: Slack, WhatsApp (which provider), or email?
6. Who besides you can approve?
7. Hosting: your own server, the main system's cloud, or a small VPS?
8. LLM provider preference and monthly cap?
9. Meta: system user token via Business Manager available?
10. Google: is there an MCC (manager account)?
11. Main system: JWT/session format and how Meta credentials are stored there (for Phase 6).
