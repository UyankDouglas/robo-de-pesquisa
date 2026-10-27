from __future__ import annotations

import threading
import time

from core.retry import RateLimiter, RetryPolicy, interruptible_sleep


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> bool:
        self.sleeps.append(round(seconds, 3))
        self.now += seconds
        return True


def test_backoff_is_progressive_and_capped():
    policy = RetryPolicy(max_retries=5, base_seconds=2, max_seconds=10)
    assert [policy.delay(n) for n in range(1, 6)] == [2, 4, 8, 10, 10]
    assert policy.delay(0) == 0


def test_rate_limiter_enforces_interval():
    clock = FakeClock()
    limiter = RateLimiter(min_interval=3, clock=clock)
    assert limiter.acquire(clock.sleep) and clock.sleeps == []  # 1ª consulta não espera
    clock.now += 1
    assert limiter.acquire(clock.sleep)
    assert clock.sleeps == [2.0]  # completa os 3 s desde a anterior


def test_rate_limiter_per_minute_and_block():
    clock = FakeClock()
    limiter = RateLimiter(min_interval=0, max_per_minute=2, clock=clock)
    limiter.acquire(clock.sleep)
    limiter.acquire(clock.sleep)
    limiter.acquire(clock.sleep)  # 3ª dentro do mesmo minuto precisa esperar a janela
    assert clock.sleeps == [60.0]
    limiter.block_for(15)  # ex.: Retry-After do site
    limiter.acquire(clock.sleep)
    assert clock.sleeps[-1] == 15.0


def test_interruptible_sleep_stops_on_event():
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()
    started = time.monotonic()
    assert interruptible_sleep(5, stop) is False
    assert time.monotonic() - started < 1
    assert interruptible_sleep(0.05, threading.Event()) is True
