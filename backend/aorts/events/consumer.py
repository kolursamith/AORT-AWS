"""Idempotent event consumer: each (consumer, event_id) is handled at most once.

The ``processed_events`` row and the handler's own database writes commit in the
same transaction, so a redelivered event (SQS / EventBridge / outbox retry) is
recognised and skipped even across restarts and replicas.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.aorts.banking.ledger.models import ProcessedEvent


class IdempotentConsumer:
    def __init__(self, db, name: str, handler: Callable[[Session, dict], dict | None]):
        self.db = db
        self.name = name
        self.handler = handler
        self.processed = 0
        self.duplicates = 0

    def __call__(self, event: dict) -> bool:
        """Returns True if handled now, False if it was a duplicate delivery."""
        with self.db.session() as s:
            s.add(
                ProcessedEvent(
                    consumer=self.name,
                    event_id=event["event_id"],
                    transaction_id=event.get("transaction_id"),
                    result={},
                )
            )
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                self.duplicates += 1
                return False
            result = self.handler(s, event) or {}
            s.get(ProcessedEvent, (self.name, event["event_id"])).result = result
        self.processed += 1
        return True
