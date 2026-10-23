"""Transactional outbox: domain events are inserted in the same DB transaction as
the state change, then relayed asynchronously by :mod:`backend.aorts.events.publisher`.

The relay gives *at-least-once* delivery. Consumers must be idempotent
(see :mod:`backend.aorts.events.consumer`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.aorts.banking.ledger.models import OutboxEvent
from backend.aorts.contracts.events import DomainEvent

MAX_ATTEMPTS = 8


def add_outbox(session: Session, event: DomainEvent) -> None:
    session.add(
        OutboxEvent(
            event_id=event.event_id,
            aggregate_id=event.transaction_id or "system",
            event_type=event.event_type,
            payload=event.model_dump(mode="json"),
        )
    )


def claim_batch(session: Session, limit: int = 100) -> list[OutboxEvent]:
    """Lock a batch of unpublished events (SKIP LOCKED lets several relays run)."""
    now = datetime.now(UTC)
    stmt = (
        select(OutboxEvent)
        .where(
            OutboxEvent.published_at.is_(None),
            OutboxEvent.dead_lettered.is_(False),
            OutboxEvent.available_at <= now,
        )
        .order_by(OutboxEvent.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.execute(stmt).scalars())


def mark_published(session: Session, ids: list[int]) -> None:
    if ids:
        session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(ids))
            .values(published_at=datetime.now(UTC))
        )


def mark_failed(session: Session, row: OutboxEvent, error: str) -> None:
    row.attempts += 1
    row.last_error = error[:1000]
    if row.attempts >= MAX_ATTEMPTS:
        row.dead_lettered = True  # local "DLQ": visible on the dashboard, never silently dropped
    else:
        row.available_at = datetime.now(UTC) + timedelta(seconds=min(60, 2**row.attempts))
