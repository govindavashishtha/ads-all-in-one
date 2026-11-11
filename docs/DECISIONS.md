# Decisions log

Where BUILD_PLAN v1.1 is silent or ambiguous, the conservative option was chosen and recorded
here. IDs are referenced from code comments (`DECISIONS D-n`). Phase 0 decisions are in
[AGENT_GUIDE.md](AGENT_GUIDE.md#decisions-made-in-phase-0-where-the-plan-was-silent-or-ambiguous).
Nothing here sets a business input (targets, caps, accounts, opt-ins); those stay I-1…I-8.

## Phase 2 — features, ranking, rules, guardrail, approvals, notify

### Windows and features
- **D-1 `as_of` = yesterday (account-local).** The daily pipeline scores the last complete
  account day; `_1d` = `as_of`, `_7d/_28d/_30d` = N complete days ending `as_of`.
- **D-2 Settled scoring window.** §10 says the scoring window "excludes the most recent
  `settle_days`". Read conservatively: the window *ends* `settle_days` before `as_of`
  (settle_days = 2, as_of 11-10 → s14 = 10-26..11-08). The prior window ends on the same day.
- **D-3 Learning inheritance for features.** `learning` is stored coalesced (NULL → true,
  §8.2). Ads and keywords carry their ad group's effective learning; a Google ad group
  carries its campaign's (Google reports learning at campaign level).
- **D-4 Effectively active only.** Scores, ranks and rules are computed only for entities
  whose own status and every ancestor's status is active. Paused/removed entities keep
  features (history) but get no score and no proposal.
- **Money in features** is `Decimal`, quantised to the cent and capped at the NUMERIC(14,2)
  maximum (a CPA over ~1e12 cannot be stored otherwise).

### Ranking (§10)
- **D-5 Google ad groups.** §10.6 has no Google ad-group rule and no peer group for it; Google
  ad groups use the Meta ad-set rule (`not learning and spend_s14 ≥ 3 × target_cpa`) and rank
  within their campaign.
- **D-6 Prior fallback chain and AOV.** Parent (campaign, leave-one-out) → ad account
  (leave-one-out) → the target itself (θ = 1/target_cpa, CTR 1%). When no parent has value
  data the AOV prior falls back to `target_cpa × target_roas` (target-implied AOV), or
  None when no ROAS target exists. The Gamma prior shape is floored at 0.1 (§10.3).
- Ties in `rank_metric` break by higher spend, then lower id (deterministic).

### Rules (§11)
- **D-7 Default risk.** A rule without `risk` creates proposals with `risk = medium`
  (needs approval in `auto_small`). Escalations are stored with status **`escalated`**
  (new value, migration 0002) and risk medium; the triage step stays skipped (Phase 4) and the
  digest lists them for a human.
- **D-8 Expressions.** simpleeval with only `min/max/abs`; Decimals become floats inside the
  evaluator (comparison only); any exception (e.g. `None > 1`) is false. Rules are evaluated
  in `(priority, key)` order, first match wins, `noop` is logged and never stored.
- **D-9 Search-term proposals.** `entity_type = search_term`, `entity_id` = the latest
  `search_terms.id` of the (ad group, term) pair in the 30-day window; `params` carry
  `term`, `ad_group_id`, `level=ad_group`, `match_type=exact`. Terms already negative
  (case-insensitive) in the ad group are skipped. They run in the daily `rules` step on the
  previous day's pull.

### Guardrail (§12.3) — pure function, conservative extensions
- **D-10 Scope extras (check 4 → `unsupported_scope`).** `spend_direction` is re-derived from
  the action and must equal the stored value; invalid/zero `change_pct` blocks; account-level
  actions block; **every action inside a shared-budget campaign blocks** (v1 does not
  support shared budgets — this also blocks `add_negative` there, which is stricter than
  needed; loosening it is a human decision); `campaign_only` scope allows only the campaign.
- **D-11 Budget arithmetic (check 8).** The band `start_of_day × (1 ± max_pct)` and
  `[min, max]_daily_budget` are rounded *inward* to the cent, so a 2-dp result can never
  leave the band (invariant 2). If they do not overlap, or clamping removes or reverses the
  change, or the change is below `min_useful_change_pct`, the result is `change_too_small`.
- **D-12 Learning.** Check 6 uses the entity's own learning flag with D-3 inheritance.
  Invariant 4 says "no Allow for a learning entity unless privileged", but check 6 only blocks
  budget changes and pauses. To satisfy it, check 11 adds the approval reason
  `entity_learning`, so a non-privileged action on a learning entity (e.g. `add_negative`)
  always needs a human. A human approval (`approved=True`) can then Allow it — the property
  test encodes exactly that.
- **D-13 `start_of_day_budget`.** Latest `entity_history` row at or before the account's
  local midnight; if history starts later (first sync today), the earliest row; if none, the
  current budget.
- **D-14 State facts.** MTD spend = settled daily account rows this month + today's hourly
  rows (includes today, conservative). `auto_pauses_applied_today` counts every engine pause
  with a proposal (approved ones too). Active budget units include unmanaged campaigns; shared
  Google budgets count once per shared budget id.
- `Allow` in the daily pipeline sets status `approved` (the executor, Phase 3, is the only
  thing that applies). Approved proposals are re-checked every run with `approved=True` on
  fresh state; a Block moves them to `blocked` and notifies.
- A Block with `cap_exceeded` also raises a warn alert (as §12.3 requires for budget blocks).

### Allocation (§10.7)
- **D-15 Drop and rebalance.** After zero-sum balancing, deltas below `min_useful_change_pct`
  are dropped and the remainder re-balanced, repeated until stable (≤ n+1 passes); cents are
  rounded toward zero and the residual is taken from the largest move on the larger side, so
  the pool total is exactly unchanged. Seed = `as_of` as YYYYMMDD.
- **D-16 Objective traffic.** Allocation is defined on θ (conversions per currency unit); for
  `objective = traffic` the weekly allocation is skipped (logged).
- Allocation proposals: `source = allocation`, risk low, `params.change_pct` (4 dp) plus
  `target_daily_budget` and `pool`; they go through the same guardrail and approvals.
  Members in cooldown (engine or manual) or learning or not data-sufficient are excluded.
  Note for the executor: the guardrail recomputes the new budget from `change_pct`
  (half-even to the cent, then clamped), so an applied pool can differ from
  `target_daily_budget` by a cent per member; apply the guardrail's `new_daily_budget`.

### Approvals and notify (§14)
- **D-17 Notification outbox.** New table `notifications` (migration 0002): one row per
  message (approval request / reminder / expiry / blocked-after-approval, digest, alert) with
  `UNIQUE (client_id, dedupe_key)`; `notify_dispatch` (every 5 min) delivers queued rows.
- **D-18 Log-only delivery.** Slack / email / WhatsApp adapters exist but are only
  *deliverable* when configured **and** a transport is injected; none is injected by default
  (approval channel is input I-5, and secrets may only come via `get_credentials`). Every
  message therefore goes to the structured log (`channel=log`). Nothing is ever sent.
- **D-19 Single source of truth.** `engine.approvals.service` owns decisions (`decide`),
  requests, reminders (at `expires_at − 12 h`), expiry (at `expires_at`, 24 h TTL) and the
  re-check; `api/services/approvals.py` is a thin wrapper. `approvals.channel` = configured
  channel names or `dashboard`.
- Expiry runs every 15 min (`approval_expiry`) and also in the daily `approvals` step.

### Learning notes (§16.4)
- `rule_stats` rows exist for every actionable rule; verdicts are recorded only at D+7/D+14
  and never for `insufficient_data` (wired for Phase 3's evaluator). The weekly review only
  raises an info alert (n ≥ 20; > 40 % worse → consider tightening, > 70 % better → consider
  loosening); it never changes rules.

### Backtest
- Replays `features → scores → rules (+ search terms) → guardrail` in memory with today's
  entity state, active rules/limits from the DB, guardrail in `read_only` plus a shadow
  `suggest` run (`--assume-managed` optional). No writes. See the limitations section of
  [backtest-synthetic.md](backtest-synthetic.md).

## Phase 4 — LLM layer (§13)

- **D-20 LLM key.** `engine.credentials` is the platform-credentials seam (lead-owned), so the
  `LLM_API_KEY` `SecretStr` is unwrapped only inside the provider adapter that sends it
  (`engine/llm/providers.py`) and never logged.
- **D-21 Triaged escalations keep status `escalated`.** No new proposal status (that would
  change the OpenAPI enum and the dashboard). The result is recorded in `params.triage`
  (`outcome` = `proposal`/`noop`/`flag_human`, `action`, `confidence`, `llm_call_id`,
  `proposal_id`, `cause`), which also makes the step idempotent. LLM proposals have
  `rule_key = NULL` (dedupe by source), `source = llm_triage | llm_analyst`, `risk = medium`,
  `params = {change_pct?, escalation_id, llm_call_id}`. Daily triage only takes escalations of
  the run's `as_of`; fatigue escalations (rule `reason: fatigue`) are left to the weekly
  creative step (last 7 days).
- **D-22 Priority near a cap (§13.3).** Lower priorities may only use a share of each cap:
  triage 100 %, analyst 90 %, digest 80 %, search terms 75 %, creative 70 %.
- **D-23 Engine-side guards on model answers.** Per-case `allowed_actions` excludes `pause` /
  `budget_change` where the guardrail would block anyway (campaign_only children, learning ad
  groups/campaigns, unmanaged campaigns, non-budget units, shared budgets). A `pause` or
  `budget_change` with confidence < 0.5 becomes `flag_human`. Duplicate or missing results for a
  case → `flag_human`; any invalid item marks the ledger row `invalid_output` (never cached).
- **D-24 Enabling the LLM without new env keys.** `.env.example` must equal plan §7.2 exactly,
  so there is no `LLM_ENABLED` / `LLM_BASE_URL`: `LLM_PROVIDER` empty = disabled;
  `anthropic[:base]`, `openai[:base]` (https only), or `fake` (development only). A real provider
  without key, both models and all four prices stays disabled (alerts, `flag_human`).
- **D-25 Ledger semantics.** `llm_calls` holds provider calls only (`ok`, `invalid_output`,
  `error`); 24 h cache hits and budget skips write no row (a skip raises one `llm_cap` warn
  alert per client-timezone day). `result_ref` stores the raw response text of `ok` rows for the
  `prompt_hash` cache. Cached input tokens are charged at the full input price (upper bound).
  The pre-call estimate is chars ÷ 3 + `max_output_tokens`. Check + call + insert run in one
  transaction under a Postgres advisory lock; `created_at` comes from the job clock.
- **D-26 Creative drafts.** One `new_ad` proposal per ad group (`entity_type = ad_group`),
  `source = llm_analyst` (the enum has no creative source), `risk = high` so it needs approval
  in every mode regardless of `approval_required_for`, `params.create_status = PAUSED`,
  `params.drafts[] = {replaces_ad_id, escalation_ids, variants[{headline, body}]}`. Limits:
  headline ≤ 30, body ≤ 90 characters (Google RSA; VERIFY-P0 in Phase 5), ≤ 3 variants, no
  links/markup.

## Integration pass (API ↔ engine ↔ dashboard)
Ids use the `D-INT-` prefix so they never collide with phase decisions.
- **D-INT-1 `system_status` key/value table (0003).** Engine-only facts the API needs (pacing
  snapshot per platform, heartbeat result, per-platform operation counts) are upserted as JSONB
  under `(client_id, key)` (`pacing:<platform>`, `heartbeat`, `operations:<platform>`). The API
  never calls connectors or the heartbeat URL itself.
- **D-INT-2 Projected EOD only from a same-day snapshot.** `projected_eod` is the pacing job's
  last value for the account's current local date; otherwise `null` (no API-side guess). The
  expected cumulative line uses the same `load_curve`/`curve_at` functions as the pacing job;
  the total block is the daily-cap-weighted mix of the platform curves.
- **D-INT-3 Typed display fields.** Proposals/changes carry `entity_name`, `platform`,
  campaign, a `change {field, before, after, after_is_estimate}` and a typed
  `guardrail_result`; the dashboard does no name lookups or param parsing.
- **D-INT-4 API → engine trigger = queue table.** `POST /jobs/{name}/run` (admin, audited)
  inserts a `job_requests` row; a partial unique index allows one open request per job (409).
  The engine polls every minute (`FOR UPDATE SKIP LOCKED`), re-checks the `MANUAL_JOBS`
  whitelist (else `rejected`), runs the job through the normal runner (advisory lock +
  `job_runs` idempotency, so a manual run of an already-completed window is a no-op), and fails
  requests stuck `running` > 6 h. Phase 3/4 jobs stay 501; `retention`/`backfill` are CLI-only.
- **D-INT-5 Proxy trust and root path.** uvicorn trusts `X-Forwarded-*` only from the pinned
  proxy IP and runs with `--root-path /ads-autopilot` so `/api/v1/docs` works behind the
  prefix-stripping proxy. Cookies stay `Secure` (HTTPS required except `localhost`).

