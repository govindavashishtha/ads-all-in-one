# VERSIONS — pinned SDK and platform API versions (BUILD_PLAN §4.1, §26)

Python packages are pinned exactly in `pyproject.toml` and locked in `uv.lock`. The SDK pins
below are the latest releases resolved on **11 Nov 2026**; whether they target the platform API
versions we want is still `VERIFY-P0`.

## Platform APIs (to pin at Phase 0)

| Item | Value | Where set | Verified? | Doc URL / date |
|---|---|---|---|---|
| Meta Marketing API version | _unset_ | `META_API_VERSION` (env) | ☐ | |
| Google Ads API version | `v26` (pinned as `GOOGLE_ADS_API_VERSION` in `engine/connectors/google_read.py`; newest version shipped in `google-ads==35.0.0`) | `engine/connectors/google_*.py` | ☐ | |
| GA4 Data API (optional) | v1beta via `google-analytics-data==0.23.3` | extra `ga4` | ☐ | |

## Runtime packages (exact pins)

| Package | Version | Purpose |
|---|---|---|
| facebook-business | 26.0.4 | Meta Marketing API SDK (connectors only) |
| google-ads | 35.0.0 | Google Ads API client (connectors only) |
| google-analytics-data | 0.23.3 | optional GA4 cross-check |
| sqlalchemy | 2.1.8 | ORM / queries |
| alembic | 1.20.1 | migrations |
| psycopg[binary] | 3.3.8 | Postgres driver |
| pydantic / pydantic-settings | 2.14.3 / 2.16.2 | models, settings |
| fastapi / uvicorn | 0.162.4 / 0.54.0 | API |
| apscheduler | 3.11.3 | scheduler (3.x per plan) |
| numpy / scipy | 2.5.4 / 1.18.1 | posteriors |
| simpleeval | 1.0.8 | rule expressions |
| typer | 0.29.1 | CLI |
| httpx | 0.28.1 | HTTP client (CLI, notifications) |
| structlog | 26.1.0 | JSON logs |
| prometheus-client | 0.26.0 | metrics |
| argon2-cffi | 25.1.0 | admin password hash |
| pyyaml | 6.0.3 | seed YAML |

Dev: pytest 9.2.0, hypothesis 6.170.15, ruff 0.17.4, mypy 2.4.1, types-pyyaml.
Toolchain: Python 3.12, uv 0.13.7, Postgres 16, Caddy 2, Node 22 (dashboard).

## Change procedure

Bumping an SDK or API version: update the pin, re-record contract fixtures
(`tests/contract/`), re-run the full suite, and add a row with date + reason below.

| Date | Change | Reason |
|---|---|---|
| 2026-11-11 | initial pins | Phase 0 foundation |

## Write path (Phase 3 / 5 code, 12 Nov 2026)

`engine/connectors/meta_write.py` and `google_write.py` use the same SDK pins as the readers
(`facebook-business==26.0.4`, `google-ads==35.0.0`, Google Ads API `v26`). The Meta Marketing
API version is still unset (`META_API_VERSION`, VERIFY-P0). These modules are exercised only
through mocked SDK clients and `engine/connectors/fakes_write.py` until write tokens exist and
the Phase 3 gate is signed off. Assumptions that need a doc check: V-11, V-12, V-25, V-28, V-29.
