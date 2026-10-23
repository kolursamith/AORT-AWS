"""Bounded worker pool per destination bank: the switch's downstream capacity model.

Each beneficiary bank (destination) gets ``workers`` concurrent slots. A request
waits at most ``wait_timeout`` seconds for a slot, otherwise it is rejected as
SWITCH_OVERLOADED. Bank B and Bank C have independent pools, so saturating B
cannot consume C's capacity.
"""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager


class DestinationPool:
    def __init__(self, name: str, workers: int, wait_timeout: float):
        self.name = name
        self.workers = workers
        self.wait_timeout = wait_timeout
        self.in_flight = 0
        self.waiting = 0
        self.rejected = 0
        self.recovery_in_flight = 0  # slots held by limited (recovery) callers
        self.max_recovery_in_flight = 0
        self._cond = threading.Condition()

    def resize(self, workers: int) -> None:
        with self._cond:
            self.workers = max(1, workers)
            self._cond.notify_all()

    def limit_for_recovery(self, reserve_fraction: float) -> int:
        """Slots recovery traffic may occupy: the rest is reserved for primary payments."""
        if reserve_fraction <= 0:
            return self.workers
        reserved = max(1, math.ceil(self.workers * reserve_fraction))
        return max(1, self.workers - reserved) if self.workers > 1 else 1

    def acquire(self, timeout: float | None = None, limit: int | None = None) -> bool:
        """Take a slot. ``limit`` caps occupancy for this caller class (recovery) below ``workers``."""
        deadline = time.monotonic() + (self.wait_timeout if timeout is None else timeout)
        with self._cond:
            self.waiting += 1
            try:
                while self.in_flight >= (
                    self.workers if limit is None else min(limit, self.workers)
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.rejected += 1
                        return False
                    self._cond.wait(remaining)
                self.in_flight += 1
                if limit is not None:
                    self.recovery_in_flight += 1
                    self.max_recovery_in_flight = max(
                        self.max_recovery_in_flight, self.recovery_in_flight
                    )
                return True
            finally:
                self.waiting -= 1

    def release(self, limited: bool = False) -> None:
        with self._cond:
            self.in_flight -= 1
            if limited:
                self.recovery_in_flight -= 1
            self._cond.notify_all()  # waiters have different limits: wake all so none sleeps past a free slot

    @contextmanager
    def slot(self, timeout: float | None = None, limit: int | None = None):
        ok = self.acquire(timeout, limit)
        try:
            yield ok
        finally:
            if ok:
                self.release(limited=limit is not None)

    def view(self) -> dict:
        with self._cond:
            return {
                "destination": self.name,
                "workers": self.workers,
                "in_flight": self.in_flight,
                "recovery_in_flight": self.recovery_in_flight,
                "max_recovery_in_flight": self.max_recovery_in_flight,
                "waiting": self.waiting,
                "rejected_total": self.rejected,
            }


class CapacityModel:
    def __init__(self, destinations: list[str], workers: int, wait_timeout: float):
        self.pools = {d: DestinationPool(d, workers, wait_timeout) for d in destinations}
        self.default_workers = workers

    def pool(self, destination: str) -> DestinationPool:
        return self.pools[destination]

    def apply_overrides(self, overrides: dict[str, int | None]) -> None:
        for dest, pool in self.pools.items():
            wanted = overrides.get(dest) or self.default_workers
            if pool.workers != wanted:
                pool.resize(wanted)

    def view(self) -> list[dict]:
        return [p.view() for p in self.pools.values()]
