"""Names of jobs an admin may trigger via ``POST /jobs/{name}/run`` (migration 0003).

Import-light on purpose (no SQLAlchemy / connectors) so the API can validate names without
pulling in the job modules. Each name must be a key of ``engine.jobs.schedule.SCHEDULE``
(test-enforced). Read-side or idempotent per window. Jobs that can write to a platform
(``daily_pipeline`` step ``execute``, ``execute_approved``, ``reconcile``,
``emergency_restore``) only do so through the executor, i.e. behind the write gate (D-28)
and the guardrail. ``retention`` (deletes rows) and ``backfill`` (CLI-only, long-running)
are deliberately excluded.
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
    # Phase 3 (executor agent)
    "emergency_restore",
    "reconcile",
    "evaluate",
    "execute_approved",
    "token_reset",
)
