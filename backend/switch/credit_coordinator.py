"""Atomic per-route inquiry credits for a single-process mock switch."""

import threading


class RouteCredits:
    def __init__(self) -> None:
        self._credits: dict[str, int] = {}
        self._lock = threading.Lock()

    def replace(self, route_id: str, amount: int) -> None:
        if amount < 0:
            raise ValueError("credits cannot be negative")
        with self._lock:
            self._credits[route_id] = amount

    def consume(self, route_id: str) -> bool:
        with self._lock:
            available = self._credits.get(route_id, 0)
            if available < 1:
                return False
            self._credits[route_id] = available - 1
            return True
