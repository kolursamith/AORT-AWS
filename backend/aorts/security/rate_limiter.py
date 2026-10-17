"""Token buckets (not thread-safe on their own; the admission controller holds the lock)."""

from __future__ import annotations

import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: float, clock=time.monotonic):
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = burst
        self.clock = clock
        self.updated = clock()

    def refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def available(self, n: float = 1.0) -> bool:
        self.refill()
        return self.tokens >= n

    def take(self, n: float = 1.0) -> None:
        self.tokens -= n

    def retry_after(self, n: float = 1.0) -> float:
        if self.tokens >= n or self.rate <= 0:
            return 0.0
        return (n - self.tokens) / self.rate

    def reconfigure(self, rate_per_sec: float, burst: float) -> None:
        self.refill()
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = min(self.tokens, burst)
