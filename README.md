# ads-autopilot

Self-hosted service that pulls Meta Ads and Google Ads data on a schedule, ranks ads with
confidence-aware scores, enforces budget limits in code, and proposes/applies guarded changes.
Plan: [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) (v1.1). Status: **Phase 2 (read-only / suggest)** —
ingestion, features, ranking, rules, guardrail, approvals, log-only notifications, digest,
weekly allocation and a backtest (`uv run python -m engine.backtest --out report.md`). No
writes to ad platforms. Decisions: [docs/DECISIONS.md](docs/DECISIONS.md).

```bash
cp .env.example .env    # fill in; never commit .env
make setup && make lint && make test
make migrate && make bootstrap
make up                 # docker compose: postgres, engine, api, dashboard, proxy
```

Open `http://localhost/ads-autopilot` (dashboard) and `/ads-autopilot/api/v1/health`.
Contributors and agents: read [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Phase 0 verification status:
[docs/VERIFIED.md](docs/VERIFIED.md), [docs/VERSIONS.md](docs/VERSIONS.md).
