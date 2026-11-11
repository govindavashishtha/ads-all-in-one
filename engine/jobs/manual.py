"""Names of jobs an admin may trigger via ``POST /jobs/{name}/run`` (migration 0003).

Import-light on purpose (no SQLAlchemy / connectors) so the API can validate names without
pulling in the job modules. Each name must be a key of ``engine.jobs.schedule.SCHEDULE``
(test-enforced). Read-side or idempotent per window; none writes to an ad platform (the
executor step stays skipped until Phase 3). ``retention`` (deletes rows) and ``backfill``
(CLI-only, long-running) are deliberately excluded.
"""

from __future__ import annotations

MANUAL_JOBS: tuple[str, ...] = (
    "pacing",
    "hourly_pull",
    "health_checks",
    "daily_pipeline",
    "search_terms",
    "weekly_strategy",
    "approval_expiry",
    "notify_dispatch",
)
