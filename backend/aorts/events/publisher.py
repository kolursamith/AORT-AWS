"""Outbox relay: moves committed outbox rows to an event sink (at-least-once).

A crash between "published to sink" and "marked published" re-publishes the
event; consumers must therefore be idempotent. The relay never pretends that a
database commit and a cloud publish are one atomic operation.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

from backend.aorts.events.outbox import claim_batch, mark_failed, mark_published

logger = logging.getLogger("aorts.outbox")


class LocalSink:
    """Default sink: keeps recent events in memory and fans out to local subscribers."""

    name = "local"

    def __init__(self, keep: int = 1000):
        self.recent: deque = deque(maxlen=keep)
        self.subscribers: list = []

    def publish(self, events: list[dict]) -> list[str]:
        failed = []
        for e in events:
            self.recent.append(e)
            for sub in self.subscribers:
                try:
                    sub(e)
                except Exception:  # noqa: BLE001 - a failing subscriber triggers redelivery
                    logger.exception("local subscriber failed for %s", e.get("event_id"))
                    failed.append(e["event_id"])
                    break
        return failed


def make_sink(settings):
    if settings.event_sink == "eventbridge":
        from backend.aorts.events.eventbridge_adapter import EventBridgeSink

        return EventBridgeSink(settings.eventbridge_bus_name, settings.aws_region)
    return LocalSink()


class OutboxRelay:
    def __init__(self, db, sink, batch_size: int = 10, interval_s: float = 0.5):
        self.db = db
        self.sink = sink
        self.batch = batch_size
        self.interval = interval_s
        self.published = 0
        self.failed = 0
        self._stop = threading.Event()

    def run_once(self) -> int:
        with self.db.session() as s:
            rows = claim_batch(s, self.batch)
            if not rows:
                return 0
            try:
                failed_ids = set(self.sink.publish([r.payload for r in rows]))
            except Exception as exc:  # noqa: BLE001 - whole batch retried with backoff
                failed_ids = {r.event_id for r in rows}
                logger.warning("sink publish failed: %s", type(exc).__name__)
            ok = [r.id for r in rows if r.event_id not in failed_ids]
            for r in rows:
                if r.event_id in failed_ids:
                    mark_failed(s, r, "publish failed")
            mark_published(s, ok)
        self.published += len(ok)
        self.failed += len(failed_ids)
        return len(ok)

    def start(self) -> None:
        def _loop():
            while not self._stop.is_set():
                try:
                    while self.run_once() == self.batch:
                        pass
                except Exception:  # noqa: BLE001
                    logger.exception("outbox relay iteration failed")
                self._stop.wait(self.interval)

        threading.Thread(target=_loop, name="outbox-relay", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
