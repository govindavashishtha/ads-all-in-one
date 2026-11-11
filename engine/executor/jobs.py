"""Scheduler / pipeline entry points for the write path (plan §9 job table, §15, §16, §12.4).

| job / step                | when                       | does                                   |
|---------------------------|----------------------------|----------------------------------------|
| daily step 4 ``evaluate`` | daily_pipeline             | due D+3 / D+7 / D+14 evaluations       |
| daily step 9 ``execute``  | daily_pipeline             | approved proposals                     |
| ``execute_approved``      | every 5 min                | approved proposals (prompt after a     |
|                           |                            | human approval, §14.5 step 5; D-30)    |
| ``evaluate``              | hourly :40                 | due evaluations                        |
| ``reconcile``             | hourly :20 + engine start  | ``applying`` rows older than 5 min     |
| ``emergency_restore``     | hourly :10                 | §12.4 step 6                           |
| ``token_reset``           | 00:00                      | previous day's LLM usage metric only   |
| ``write_requests``        | every minute               | API-queued rollback/restore/pause-all  |

Steps run inside ``run_step`` (job_runs idempotency) but do their platform writes in their
own committed transactions (write-ahead), never in the step's transaction.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from zoneinfo import ZoneInfo

import structlog
from prometheus_client import Gauge
from sqlalchemy.orm import Session

from engine.evaluation.evaluate import evaluate_due
from engine.executor.apply import env_from_ctx
from engine.executor.execute import execute_approved
from engine.executor.gate import write_gate
from engine.executor.reconcile import reconcile
from engine.executor.requests import process_write_requests
from engine.jobs.common import client_window_key_hour, client_yesterday
from engine.jobs.runner import JobContext, StepOutcome, StepResult, job_lock, run_step

log = structlog.get_logger(__name__)

LLM_TOKENS_PREV_DAY = Gauge("ads_llm_tokens_previous_day", "LLM tokens used yesterday")
LLM_COST_PREV_DAY = Gauge("ads_llm_cost_usd_previous_day", "LLM cost (USD) yesterday")


def execute_step(ctx: JobContext, s: Session) -> StepOutcome:
    gate = write_gate(s, ctx.settings, reducing=False)
    if not gate.open and gate.reason and not gate.kill_switch:
        # nothing can be written (execution disabled / read_only / above HARD_MAX_MODE):
        # recorded 'skipped' with the reason (re-runs next time); approved proposals wait
        return StepOutcome(skipped_reason=f"write gate closed: {gate.reason}")
    res = execute_approved(env_from_ctx(ctx))
    n = sum(v for k, v in res["counts"].items() if k != "skipped")
    return StepOutcome(rows=n, details=res)


def evaluate_step(ctx: JobContext, _s: Session) -> StepOutcome:
    counts = evaluate_due(env_from_ctx(ctx))
    return StepOutcome(rows=sum(counts.values()), details={"evaluations": counts})


def _slot(ctx: JobContext, minutes: int) -> str:
    local = ctx.now().astimezone(ZoneInfo(ctx.settings.timezone))
    return local.replace(
        minute=local.minute - local.minute % minutes, second=0, microsecond=0
    ).strftime("%Y-%m-%dT%H:%M")


def _locked(
    ctx: JobContext, job: str, window: str, body: Callable[[Session], StepOutcome]
) -> StepResult | None:
    with job_lock(ctx.engine, ctx.client_id, job) as got:
        if not got:
            return None
        return run_step(ctx, job, None, window, body)


def execute_approved_job(ctx: JobContext) -> StepResult | None:
    return _locked(ctx, "execute_approved", _slot(ctx, 5), lambda s: execute_step(ctx, s))


def evaluate_job(ctx: JobContext) -> StepResult | None:
    window = client_window_key_hour(ctx.now(), ctx.settings.timezone)
    return _locked(ctx, "evaluate", window, lambda s: evaluate_step(ctx, s))


def reconcile_job(ctx: JobContext) -> StepResult | None:
    def body(_s: Session) -> StepOutcome:
        counts = reconcile(env_from_ctx(ctx))
        details: dict[str, object] = dict(counts)
        return StepOutcome(rows=sum(counts.values()), details=details)

    return _locked(ctx, "reconcile", _slot(ctx, 15), body)


def emergency_restore_job(ctx: JobContext) -> StepResult | None:
    from engine.pacing.emergency import auto_restore

    def body(_s: Session) -> StepOutcome:
        res = auto_restore(env_from_ctx(ctx))
        return StepOutcome(rows=len(res), details={"restores": res})

    window = client_window_key_hour(ctx.now(), ctx.settings.timezone)
    return _locked(ctx, "emergency_restore", window, body)


def token_reset_job(ctx: JobContext) -> StepResult | None:
    """No-op on data (§9): publishes yesterday's LLM usage (computed from ``llm_calls``)."""
    from engine.llm.budget import current_usage

    def body(s: Session) -> StepOutcome:
        z = ZoneInfo(ctx.settings.timezone)
        today = ctx.now().astimezone(z).date()
        last_moment = dt.datetime.combine(today, dt.time(), tzinfo=z) - dt.timedelta(seconds=1)
        usage = current_usage(s, ctx.client_id, last_moment, ctx.settings.timezone)
        LLM_TOKENS_PREV_DAY.set(usage.tokens_today)
        LLM_COST_PREV_DAY.set(float(usage.cost_today))
        log.info(
            "llm.usage_previous_day", tokens=usage.tokens_today, cost_usd=str(usage.cost_today)
        )
        return StepOutcome(
            details={"tokens": usage.tokens_today, "cost_usd": str(usage.cost_today)}
        )

    window = client_yesterday(ctx.now(), ctx.settings.timezone).isoformat()
    return _locked(ctx, "token_reset", window, body)


def write_requests_job(ctx: JobContext) -> int:
    return process_write_requests(env_from_ctx(ctx))


def startup_reconcile(ctx: JobContext) -> dict[str, int]:
    """§15: runs once at engine start, before the scheduler. Never raises."""
    try:
        return reconcile(env_from_ctx(ctx))
    except Exception:
        log.exception("reconcile.startup_failed")
        return {}
