"""Espera progressiva (backoff exponencial), controle de ritmo e esperas interrompíveis."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

Sleeper = Callable[[float], bool]
"""Função que espera N segundos e retorna False se a espera foi interrompida (cancelamento)."""


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_seconds: float = 2.0
    max_seconds: float = 60.0
    factor: float = 2.0

    def delay(self, retry_number: int) -> float:
        """Espera antes da N-ésima nova tentativa (1, 2, 3...): base, base*2, base*4... até o máximo."""
        if retry_number < 1:
            return 0.0
        return min(self.base_seconds * (self.factor ** (retry_number - 1)), self.max_seconds)


def interruptible_sleep(seconds: float, *interrupt: threading.Event, step: float = 0.1) -> bool:
    """Dorme em pequenos passos. Retorna False assim que qualquer evento for sinalizado."""
    deadline = time.monotonic() + max(seconds, 0)
    while True:
        if any(ev.is_set() for ev in interrupt):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(step, remaining))


class RateLimiter:
    """Garante o intervalo mínimo entre consultas e, opcionalmente, um teto por minuto.

    O intervalo efetivo é o maior entre o configurado pelo operador e o exigido pelo site
    (``site_min_interval`` declarado no adaptador).
    """

    def __init__(
        self,
        min_interval: float = 0.0,
        max_per_minute: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.min_interval = max(min_interval, 0.0)
        self.max_per_minute = max(max_per_minute, 0)
        self.clock = clock
        self._last: float | None = None
        self._window: deque[float] = deque()
        self._blocked_until = 0.0

    def block_for(self, seconds: float) -> None:
        """O serviço pediu para esperar (ex.: Retry-After)."""
        self._blocked_until = max(self._blocked_until, self.clock() + max(seconds, 0))

    def time_until_allowed(self) -> float:
        now = self.clock()
        wait = max(self._blocked_until - now, 0.0)
        if self._last is not None:
            wait = max(wait, self._last + self.min_interval - now)
        if self.max_per_minute:
            while self._window and now - self._window[0] >= 60:
                self._window.popleft()
            if len(self._window) >= self.max_per_minute:
                wait = max(wait, self._window[0] + 60 - now)
        return max(wait, 0.0)

    def acquire(self, sleeper: Sleeper) -> bool:
        """Espera até poder consultar. Retorna False se a espera foi interrompida."""
        while True:
            wait = self.time_until_allowed()
            if wait <= 0:
                break
            if not sleeper(wait):
                return False
        now = self.clock()
        self._last = now
        if self.max_per_minute:
            self._window.append(now)
        return True
