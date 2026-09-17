"""Circuit breakers and rolling health stats (spec §15, §21).

Every provider/model deployment has a breaker:

    CLOSED    — normal traffic allowed
    OPEN      — provider failing repeatedly; NO normal traffic. After
                ``open_for`` seconds a single probe request is allowed.
    HALF_OPEN — probe in flight; success closes, failure re-opens.

A breaker that opens stops a failing provider from slowing down every
request. Stats (latency, error rate over a rolling window) feed the
"latency" and "balanced" routing policies.

v1 is in-process state: correct for a single gateway replica. The class
interface is deliberately small so a Redis-backed implementation (shared
across replicas, spec §12) can replace it without touching the engine —
swap ``CircuitRegistry`` for a distributed one behind the same methods.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass(slots=True)
class RollingStats:
    """Rolling window of (ok, latency_ms) samples for ranking (spec §21)."""

    window_s: float = 300.0
    samples: deque = field(default_factory=lambda: deque(maxlen=200))

    def record(self, ok: bool, latency_ms: int) -> None:
        self.samples.append((time.monotonic(), ok, latency_ms))

    def _fresh(self) -> list[tuple[bool, int]]:
        cutoff = time.monotonic() - self.window_s
        return [(ok, ms) for ts, ok, ms in self.samples if ts >= cutoff]

    def avg_latency_ms(self) -> float | None:
        fresh = [ms for ok, ms in self._fresh() if ok]
        if not fresh:
            return None
        return sum(fresh) / len(fresh)

    def error_rate(self) -> float | None:
        fresh = self._fresh()
        if not fresh:
            return None
        return sum(1 for ok, _ in fresh if not ok) / len(fresh)


class CircuitBreaker:
    """One provider/model deployment's breaker."""

    def __init__(self, key: str, *, threshold: int = 3, open_for_s: float = 30.0) -> None:
        self.key = key
        self.threshold = max(1, threshold)
        self.open_for_s = open_for_s
        self.state = "closed"          # closed | open | half_open
        self.failures = 0              # consecutive failures
        self.opened_at: float | None = None
        self.half_open_probes = 0

    # ------------------------------------------------------------ API --
    def allow(self) -> bool:
        now = time.monotonic()
        if self.state == "open":
            if self.opened_at is not None and now - self.opened_at >= self.open_for_s:
                self.state = "half_open"
                self.half_open_probes = 0
            else:
                return False
        if self.state == "half_open":
            # One probe at a time: while a probe is in flight we stay quiet.
            if self.half_open_probes >= 1:
                return False
            self.half_open_probes = 1
        return True

    def record_success(self) -> None:
        self.state = "closed"
        self.failures = 0
        self.opened_at = None
        self.half_open_probes = 0

    def record_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open":
            self.state = "open"
            self.opened_at = time.monotonic()
            return
        if self.failures >= self.threshold:
            self.state = "open"
            self.opened_at = time.monotonic()

    # --------------------------------------------------------- debug --
    def snapshot(self) -> dict:
        return {
            "key": self.key,
            "state": self.state,
            "consecutive_failures": self.failures,
            "opened_at": self.opened_at,
        }


class CircuitRegistry:
    """All breakers + stats, keyed by provider/model deployment."""

    def __init__(
        self,
        *,
        threshold: int = 3,
        open_for_s: float = 30.0,
        window_s: float = 300.0,
    ) -> None:
        self.threshold = threshold
        self.open_for_s = open_for_s
        self.window_s = window_s
        self._breakers: dict[str, CircuitBreaker] = {}
        self._stats: dict[str, RollingStats] = {}

    def _breaker(self, key: str) -> CircuitBreaker:
        b = self._breakers.get(key)
        if b is None:
            b = CircuitBreaker(key, threshold=self.threshold, open_for_s=self.open_for_s)
            self._breakers[key] = b
        return b

    def _stats_for(self, key: str) -> RollingStats:
        s = self._stats.get(key)
        if s is None:
            s = RollingStats(window_s=self.window_s)
            self._stats[key] = s
        return s

    # ------------------------------------------------------------ API --
    def allow(self, key: str) -> bool:
        return self._breaker(key).allow()

    def record_success(self, key: str, latency_ms: int) -> None:
        self._breaker(key).record_success()
        self._stats_for(key).record(True, latency_ms)

    def record_failure(self, key: str, latency_ms: int) -> None:
        self._breaker(key).record_failure()
        self._stats_for(key).record(False, latency_ms)

    def avg_latency_ms(self, key: str) -> float | None:
        return self._stats_for(key).avg_latency_ms()

    def error_rate(self, key: str) -> float | None:
        return self._stats_for(key).error_rate()

    def snapshot(self) -> list[dict]:
        return [b.snapshot() for b in self._breakers.values()]
