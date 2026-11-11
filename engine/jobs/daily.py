"""``daily_pipeline`` (plan §9.1): steps in strict order, each its own ``job_runs`` row with
``window_key = as_of`` (client-timezone yesterday), skipped if already ``success``.

This module owns the ORDER and the ingestion steps (``repull``, ``windows``). Later steps are
registered by their owners with ``register_step(name, fn)``; an unregistered step is recorded
``skipped`` ("not implemented") — not ``success`` — so it runs once implemented. A failing
step stops the pipeline (later steps don't run) and re-raises; ``engine.jobs.schedule``
retries once after 10 minutes, then raises a ``critical`` alert.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

import structlog
from sqlalchemy.orm import Session

from engine.jobs.common import client_yesterday
from engine.jobs.ingestion import repull_step, windows_step
from engine.jobs.runner import JobContext, StepOutcome, StepResult, job_lock, run_step

log = structlog.get_logger(__name__)

JOB = "daily_pipeline"
STEPS: tuple[str, ...] = (
    "repull",
    "windows",
    "features",
    "evaluate",
    "score",
    "rules",
    "triage",
    "guardrail",
    "execute",
    "approvals",
    "digest",
)
OWNERS = {
    "features": "ranking agent",
    "evaluate": "executor agent",
    "score": "ranking agent",
    "rules": "ranking agent",
    "triage": "LLM agent",
    "guardrail": "ranking agent",
    "execute": "executor agent",
    "approvals": "API agent",
    "digest": "API agent (notify)",
}

StepFn = Callable[[JobContext, Session], StepOutcome]
_HANDLERS: dict[str, StepFn] = {"repull": repull_step, "windows": windows_step}


def _not_implemented(reason: str, _s: Session) -> StepOutcome:
    return StepOutcome(skipped_reason=reason)


def register_step(name: str, fn: StepFn) -> None:
    if name not in STEPS:
        raise ValueError(f"unknown daily_pipeline step {name!r} (order is fixed, §9.1)")
    _HANDLERS[name] = fn


def daily_pipeline(ctx: JobContext) -> list[StepResult]:
    as_of = client_yesterday(ctx.now(), ctx.settings.timezone)
    window = as_of.isoformat()
    results: list[StepResult] = []
    with job_lock(ctx.engine, ctx.client_id, JOB) as got:
        if not got:
            log.info("job.locked", job=JOB)
            return results
        for step in STEPS:
            handler = _HANDLERS.get(step)
            if handler is None:
                reason = f"not implemented yet (owner: {OWNERS.get(step, '?')})"
                results.append(run_step(ctx, JOB, step, window, partial(_not_implemented, reason)))
                continue
            # run_step re-raises on failure, so later steps never run after a failed one
            results.append(run_step(ctx, JOB, step, window, partial(handler, ctx)))
    return results
