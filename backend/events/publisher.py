"""Event publisher interface and development collector."""

from typing import Protocol


class Publisher(Protocol):
    def publish(self, topic: str, payload: dict) -> None: ...


class CollectingPublisher:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def publish(self, topic: str, payload: dict) -> None:
        self.events.append((topic, dict(payload)))
