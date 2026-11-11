"""Token bucket, retries with jitter (§9.3) and the API-operation counter (§4.3)."""

from __future__ import annotations

import datetime as dt
import random

import pytest

from engine.connectors.resilience import (
    OperationCounter,
    RetryPolicy,
    TokenBucket,
    call_with_retries,
)
from engine.enums import Platform


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def test_token_bucket_bursts_then_waits() -> None:
    c = FakeClock()
    b = TokenBucket(rate=2.0, capacity=3.0, clock=c.now, sleep=c.sleep)
    assert [b.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert b.acquire() == pytest.approx(0.5)
    with pytest.raises(ValueError):
        TokenBucket(rate=0, capacity=1)


def test_retries_stop_after_five_with_bounded_jitter() -> None:
    slept: list[float] = []
    policy = RetryPolicy(sleep=slept.append, rng=random.Random(1), base_delay=1, max_delay=8)
    calls = {"n": 0}

    def boom() -> int:
        calls["n"] += 1
        raise TimeoutError("transient")

    with pytest.raises(TimeoutError):
        call_with_retries(boom, is_retryable=lambda e: True, policy=policy, platform=Platform.META)
    assert calls["n"] == 6  # 1 try + 5 retries
    assert all(0 <= s <= min(8, 2**i) for i, s in enumerate(slept))
    # deterministic with a fixed seed (§0 rule 8)
    again: list[float] = []
    p2 = RetryPolicy(sleep=again.append, rng=random.Random(1), base_delay=1, max_delay=8)
    with pytest.raises(TimeoutError):
        call_with_retries(boom, is_retryable=lambda e: True, policy=p2, platform=Platform.META)
    assert again == slept


def test_non_retryable_raises_immediately() -> None:
    slept: list[float] = []
    empty: dict[str, int] = {}
    with pytest.raises(KeyError):
        call_with_retries(
            lambda: empty["x"],
            is_retryable=lambda e: False,
            policy=RetryPolicy(sleep=slept.append),
            platform=Platform.GOOGLE,
        )
    assert slept == []


def test_operation_counter_resets_per_day_and_alerts_at_80pct() -> None:
    c = OperationCounter(Platform.GOOGLE, daily_limit=10)
    d1, d2 = dt.date(2026, 11, 10), dt.date(2026, 11, 11)
    for _ in range(7):
        c.add(d1)
    assert not c.usage(d1).over_alert_threshold
    assert c.add(d1).over_alert_threshold  # 8/10
    assert c.usage(d2).used == 0
    assert c.add(d2).used == 1
    assert not OperationCounter(Platform.META, None).add(d1).over_alert_threshold
