"""Outbox readers publish only after the ledger transaction commits."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.ledger.models import OutboxEvent


def unpublished(session: Session, limit: int = 100) -> list[OutboxEvent]:
    statement = (
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
        .order_by(OutboxEvent.id)
        .limit(limit)
    )
    return list(session.scalars(statement))
