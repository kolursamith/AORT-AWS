"""Thread-safe per-bank fixed-window quota for a single mock switch instance."""

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    accepted: bool
    remaining: int
    retry_after_seconds: float


class PerBankRateLimiter:
    def __init__(self, quota: int, window_seconds: float = 1.0) -> None:
        if quota < 1 or window_seconds <= 0:
            raise ValueError("quota and window must be positive")
        self.quota = quota
        self.window_seconds = window_seconds
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def check(self, bank_id: str, *, now: float | None = None) -> RateLimitResult:
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            start, used = self._windows.get(bank_id, (timestamp, 0))
            if timestamp - start >= self.window_seconds:
                start, used = timestamp, 0
            accepted = used < self.quota
            if accepted:
                used += 1
            self._windows[bank_id] = (start, used)
            return RateLimitResult(
                accepted=accepted,
                remaining=max(0, self.quota - used),
                retry_after_seconds=0.0
                if accepted
                else max(0.0, self.window_seconds - (timestamp - start)),
            )
