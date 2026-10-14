"""Bounded in-memory queue for local demos only."""

from collections import deque


class FullQueueError(RuntimeError):
    pass


class CheckQueue:
    def __init__(self, capacity: int = 1000) -> None:
        self.capacity = capacity
        self._items: deque[dict] = deque()

    def put(self, item: dict) -> None:
        if len(self._items) >= self.capacity:
            raise FullQueueError("status-check queue capacity reached")
        self._items.append(dict(item))

    def get(self) -> dict | None:
        return self._items.popleft() if self._items else None

    def __len__(self) -> int:
        return len(self._items)
