"""The one path to a provider (plan §13.3): cache → budget check → call → ledger.

Every provider call is recorded in ``llm_calls`` in its OWN committed transaction, so spend is
on the ledger even if the job step that asked for it later rolls back. The budget check, the
call and the ledger insert run under a Postgres advisory lock so two jobs cannot both pass the
check against the same remaining budget.

``llm_calls`` row semantics:
- ``status``: ``ok`` (valid output, cacheable) | ``invalid_output`` (schema/whitelist failed;
  tokens still count) | ``error`` (transport/provider failure, 0 tokens unless reported);
- ``result_ref``: the raw response text of ``ok`` rows, reused by the 24 h ``prompt_hash``
  cache (an identical request reuses it at 0 tokens and writes no row);
- ``created_at`` is set from the job clock, so day/month sums follow the pinned clock in tests.
Budget skips write no ``llm_calls`` row; they raise one deduped ``warn`` alert per day.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Engine, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from engine.config import Settings
from engine.db.models import LLMCall
from engine.enums import Severity
from engine.ingest.alerts import raise_alert
from engine.llm.budget import TokenBudget, current_usage, estimate
from engine.llm.client import LLMClient, LLMRequest
from engine.llm.fake import FakeLLM
from engine.llm.prices import ZERO_PRICE, TierPrice, require_tier_price, tier_price

log = structlog.get_logger(__name__)

CACHE_TTL = dt.timedelta(hours=24)
_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"ads-autopilot:llm_budget").digest()[:8], "big", signed=True
)


@dataclass(frozen=True, slots=True)
class CallResult:
    status: str  # ok | cache_hit | skipped_budget | error
    text: str | None = None
    llm_call_id: int | None = None
    detail: str = ""
    cost_usd: Decimal = Decimal(0)
    tokens: int = 0

    @property
    def has_text(self) -> bool:
        return self.text is not None


def prompt_hash(model: str, req: LLMRequest) -> str:
    body = json.dumps(
        {
            "model": model,
            "tier": req.tier.value,
            "purpose": req.purpose,
            "system": req.system,
            "messages": [[m.role, m.content] for m in req.messages],
            "max": req.max_output_tokens,
            "temp": req.temperature,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


class LLMGateway:
    def __init__(
        self,
        settings: Settings,
        client: LLMClient,
        *,
        engine: Engine,
        sessions: sessionmaker[Session],
        clock: Callable[[], dt.datetime],
        job: str,
    ) -> None:
        self.settings = settings
        self.client = client
        self.engine = engine
        self.sessions = sessions
        self.clock = clock
        self.job = job
        self.budget = TokenBudget(settings)

    def _price(self, req: LLMRequest) -> TierPrice:
        if isinstance(self.client, FakeLLM):
            return tier_price(self.settings, req.tier) or ZERO_PRICE
        return require_tier_price(self.settings, req.tier)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        conn = self.engine.connect()
        try:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK_KEY})
            conn.commit()
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
                conn.commit()
        finally:
            conn.close()

    def call(self, req: LLMRequest) -> CallResult:
        now = self.clock().astimezone(dt.UTC)
        cid = self.settings.client_id
        model = self.client.model_for(req.tier)
        h = prompt_hash(model, req)
        price = self._price(req)
        with self._lock(), self.sessions() as s, s.begin():
            cached = s.execute(
                select(LLMCall.id, LLMCall.result_ref)
                .where(
                    LLMCall.client_id == cid,
                    LLMCall.prompt_hash == h,
                    LLMCall.status == "ok",
                    LLMCall.created_at >= now - CACHE_TTL,
                    LLMCall.result_ref.is_not(None),
                )
                .order_by(LLMCall.created_at.desc())
                .limit(1)
            ).first()
            if cached is not None:
                log.info("llm.cache_hit", purpose=req.purpose, llm_call_id=cached.id)
                return CallResult("cache_hit", cached.result_ref, int(cached.id))

            est = estimate(req, price)
            usage = current_usage(s, cid, now, self.settings.timezone)
            decision = self.budget.check(usage, req.purpose, est)
            if not decision.allowed:
                local_day = now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
                raise_alert(
                    s,
                    cid,
                    dedupe_key=f"llm_cap:{local_day}",
                    severity=Severity.WARN,
                    kind="llm_cap",
                    message=f"LLM budget cap reached ({decision.cap}): {decision.detail}. "
                    "Escalations go to a human (flag_human) until the cap resets.",
                )
                log.warning("llm.skipped_budget", purpose=req.purpose, cap=decision.cap)
                return CallResult("skipped_budget", detail=f"{decision.cap}: {decision.detail}")

            try:
                resp = self.client.complete(req)
            except Exception as exc:  # degrade, never fail the pipeline (§13.1)
                row = self._record(s, now, req, model, h, 0, 0, 0, Decimal(0), None, "error")
                log.warning("llm.error", purpose=req.purpose, error=type(exc).__name__)
                return CallResult("error", llm_call_id=row, detail=type(exc).__name__)

            cost = price.cost(resp.input_tokens, resp.output_tokens)
            row = self._record(
                s,
                now,
                req,
                resp.model,
                h,
                resp.input_tokens,
                resp.cached_input_tokens,
                resp.output_tokens,
                cost,
                resp.latency_ms,
                "ok",
                result=resp.text,
            )
            return CallResult(
                "ok",
                resp.text,
                row,
                cost_usd=cost,
                tokens=resp.input_tokens + resp.output_tokens,
            )

    def _record(  # noqa: PLR0917 - one ledger row, every column positional on purpose
        self,
        s: Session,
        now: dt.datetime,
        req: LLMRequest,
        model: str,
        h: str,
        tin: int,
        tcached: int,
        tout: int,
        cost: Decimal,
        latency: int | None,
        status: str,
        result: str | None = None,
    ) -> int:
        row = LLMCall(
            client_id=self.settings.client_id,
            created_at=now,
            job=self.job,
            tier=req.tier.value,
            model=model,
            purpose=req.purpose,
            input_tokens=tin,
            cached_input_tokens=tcached,
            output_tokens=tout,
            cost_usd=cost,
            latency_ms=latency,
            prompt_hash=h,
            result_ref=result,
            status=status,
        )
        s.add(row)
        s.flush()
        return int(row.id)

    def mark_invalid(self, llm_call_id: int | None, why: str) -> None:
        """Output failed validation: keep the tokens on the ledger, never reuse it from cache."""
        if llm_call_id is None:
            return
        with self.sessions() as s, s.begin():
            s.execute(
                update(LLMCall)
                .where(LLMCall.id == llm_call_id, LLMCall.status == "ok")
                .values(status="invalid_output")
            )
        log.warning("llm.invalid_output", llm_call_id=llm_call_id, why=why[:300])
