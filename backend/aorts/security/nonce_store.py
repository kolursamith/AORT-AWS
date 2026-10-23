"""Replay-nonce store.

The local implementation is an in-process, thread-safe TTL map. It is enough to
block replays against a single service replica. With several replicas, use a
shared store (e.g. DynamoDB conditional put on the nonce or a Postgres unique
row); see SECURITY.md. Financial idempotency never depends on this store.
"""

from __future__ import annotations

import threading
import time


class NonceStore:
    def __init__(self, ttl_seconds: int = 600, max_entries: int = 500_000):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def check_and_store(self, key_id: str, nonce: str) -> bool:
        """Return True if (key_id, nonce) is fresh, False if it was already used."""
        token = f"{key_id}:{nonce}"
        now = time.monotonic()
        with self._lock:
            if now - self._last_sweep > 30 or len(self._seen) > self.max_entries:
                self._sweep(now)
            expiry = self._seen.get(token)
            if expiry is not None and expiry > now:
                return False
            self._seen[token] = now + self.ttl
            return True

    def _sweep(self, now: float) -> None:
        self._seen = {k: v for k, v in self._seen.items() if v > now}
        self._last_sweep = now

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()
