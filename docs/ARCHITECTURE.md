# Architecture map

Source of truth: [BUILD_PLAN.md](BUILD_PLAN.md) v1.1 (§3 diagram). This page maps the plan
onto the code.

```
scheduler (engine/main.py, APScheduler 3.x + pg advisory locks)
  └─ jobs/ ──► connectors/ (ReadConnector) ──► ingest/ ──► Postgres
                                   ▲
            credentials/get_credentials(client_id, platform, access)
Postgres ──► features/ ──► ranking/ ──► rules/ ──► proposals
                                          └─ escalate ──► llm/ (triage/analyst, budget.py caps)
proposals ──► guardrails/check.py (pure) ──► Allow ──► executor/ (WriteConnector) ──► change_log
                                         ├─ NeedsApproval ──► approvals/ ──► executor/
                                         └─ Block ──► alerts
change_log ──► evaluation/ (D+3, D+7, D+14) ──► executor/rollback.py
pacing/ (15 min) ──► pacing/emergency.py (WriteConnector, pause only)
Postgres ──► api/ (FastAPI /api/v1, auth/adapter.py) ──► dashboard/ (Next.js) and cli/ (Typer)
proxy/Caddyfile: /ads-autopilot/api/* → api (prefix stripped); /ads-autopilot/* → dashboard
api POST /jobs/{name}/run ──► job_requests ──► engine poller (1 min) ──► whitelisted job
api POST rollback|restore|pause-all ──► write_requests ──► engine poller (1 min) ──► executor
engine (pacing, heartbeat, op counters) ──► system_status ──► api /overview, /system
clients.execution_enabled + mode + HARD_MAX_MODE + kill switch ──► write gate (D-28)
```

| Layer | Package | Key interface (defined in Phase 0) |
|---|---|---|
| Settings | `engine/config.py` | `Settings`, `get_settings()`; HARD_* ceilings, env kill switch |
| Shared vocab | `engine/enums.py` | `Platform`, `EntityType`, `Mode`, `Role`, `Action`, statuses |
| DB | `engine/db/` | SQLAlchemy models for the 25 §8.3 tables + `notifications` (0002) + `job_requests`, `system_status` (0003) + `write_requests` and executor columns (0004; 29 tables), `session.py` |
| Migrations | `db/` | Alembic, revisions `0001`–`0004` |
| Executor | `engine/executor/` | plan, write gate, apply (write-ahead + validate_only), execute, reconcile, rollback; fake platforms in `connectors/fakes_write.py` |
| Evaluation | `engine/evaluation/` | D+3 safety, D+7/D+14 Gamma-Poisson verdict, `rule_stats` |
| Emergency | `engine/pacing/emergency.py` | pause managed campaigns, restore, pause-all; wired to `jobs.pacing.EMERGENCY_HANDLER` |
| Seeding | `engine/bootstrap.py` | `bootstrap(session, settings)` (§7.1) |
| Credentials | `engine/credentials/` | `get_credentials()`, `CredentialsProvider`, env + remote stub |
| Connectors | `engine/connectors/base.py`, `money.py` | `ReadConnector`, `WriteConnector`, normalised records, money conversion |
| Rules | `engine/rules/registry.py`, `schema.py` | variable registry, rules config validation |
| Guardrail | `engine/guardrails/check.py`, `limits_schema.py` | `check(proposal, state, approved=) -> Allow\|NeedsApproval\|Block` |
| LLM | `engine/llm/` (Phase 4) | `LLMClient` (`client.py`), `LLMGateway`, `triage_step`, `creative_step`; see [engine/llm/README.md](../engine/llm/README.md) |
| Engine → API facts | `engine/system_status.py` | `put_status`/`get_statuses`: pacing snapshot, heartbeat, per-platform op counts |
| Job queue | `engine/jobs/requests.py`, `manual.py` | `process_job_requests(ctx, jobs)`; `MANUAL_JOBS` whitelist |
| Auth | `api/auth/adapter.py` | `get_current_user(request) -> User` |
| API | `api/main.py` | `/api/v1` router, `/api/v1/health` |
| CLI | `cli/ads.py` | Typer app `ads` |

Runtime config flow: `.env` + `engine/rules/rules.yaml` + `engine/guardrails/limits.yaml`
seed the DB once; after that `clients` (mode, kill switch) and the active `config_versions`
rows are authoritative. `HARD_*` and `KILL_SWITCH=true` stay env-only overrides.
