"""Rate limiting, retries and API-operation accounting for connectors (plan §4.3, §9.3, §21).

- ``TokenBucket``: per-platform client-side rate limit.
- ``call_with_retries``: exponential backoff with full jitter, max 5 retries, only for errors the
  platform classifies as rate-limit / transient (the predicate is supplied per platform).
- ``OperationCounter``: API operations per account-local day per platform, exported as the
  Prometheus gauge ``ads_api_operations_today`` and checked against 80% of the daily limit.

The counter lives in process memory. It resets on restart (documented limitation: after a
restart the count under-reports until the next day). Persisting it is a later improvement.
"""

from __future__ import annotations

import datetime as dt
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from prometheus_client import Counter, Gauge

from engine.enums import Platform

MAX_RETRIES = 5  # §9.3

API_CALLS = Counter("ads_api_calls_total", "Platform API calls", ["platform", "kind"])
API_ERRORS = Counter("ads_api_errors_total", "Platform API errors", ["platform", "retryable"])
OPERATIONS_TODAY = Gauge(
    "ads_api_operations_today", "Platform API operations used today", ["platform"]
)


class TokenBucket:
    """Classic token bucket. ``rate`` tokens per second, burst ``capacity``."""

    def __init__(
        self,
        rate: float,
        capacity: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be > 0")
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        """Block until ``tokens`` are available; returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                need = (tokens - self._tokens) / self.rate
            self._sleep(need)
            waited += need


@dataclass
class RetryPolicy:
    max_retries: int = MAX_RETRIES
    base_delay: float = 1.0
    max_delay: float = 60.0
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=lambda: random.Random(0))  # noqa: S311

    def delay(self, attempt: int) -> float:
        """Full jitter: uniform(0, min(max_delay, base * 2**attempt))."""
        cap = min(self.max_delay, self.base_delay * (2**attempt))
        return self.rng.uniform(0, cap)


def call_with_retries[T](
    fn: Callable[[], T],
    *,
    is_retryable: Callable[[BaseException], bool],
    policy: RetryPolicy,
    platform: Platform,
) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            retryable = is_retryable(exc)
            API_ERRORS.labels(platform=platform.value, retryable=str(retryable).lower()).inc()
            if not retryable or attempt >= policy.max_retries:
                raise
            policy.sleep(policy.delay(attempt))
            attempt += 1


@dataclass(frozen=True, slots=True)
class OperationUsage:
    platform: Platform
    day: dt.date
    used: int
    limit: int | None

    @property
    def fraction(self) -> float | None:
        return None if not self.limit else self.used / self.limit

    @property
    def over_alert_threshold(self) -> bool:
        """§4.3: alert at 80% of the access level's daily limit."""
        f = self.fraction
        return f is not None and f >= ALERT_FRACTION


ALERT_FRACTION = 0.80


class OperationCounter:
    """Counts operations per platform per account-local day (thread-safe)."""

    def __init__(self, platform: Platform, daily_limit: int | None) -> None:
        self.platform = platform
        self.daily_limit = daily_limit
        self._day: dt.date | None = None
        self._used = 0
        self._lock = threading.Lock()

    def add(self, day: dt.date, n: int = 1) -> OperationUsage:
        with self._lock:
            if day != self._day:
                self._day, self._used = day, 0
            self._used += n
            OPERATIONS_TODAY.labels(platform=self.platform.value).set(self._used)
            return OperationUsage(self.platform, day, self._used, self.daily_limit)

    def usage(self, day: dt.date) -> OperationUsage:
        with self._lock:
            used = self._used if day == self._day else 0
            return OperationUsage(self.platform, day, used, self.daily_limit)
