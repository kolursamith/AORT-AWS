"""Event contracts (schema version 1.0) published through the transactional outbox."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from backend.aorts.contracts.enums import SCHEMA_VERSION, Severity


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


# Transaction ids carry the remitter bank (``TXN<bank>-<16 hex>``) so the switch can bind an id it
# has never seen to the signed caller: bank A2 cannot fence or read bank A1's transactions.
_TXN_NAMESPACE = re.compile(r"^TXN([A-Z][A-Z0-9]{0,7})-[0-9A-F]{16}$")


def new_transaction_id(bank_id: str) -> str:
    return f"TXN{bank_id}-{uuid.uuid4().hex[:16].upper()}"


def transaction_id_bank(transaction_id: str) -> str | None:
    """The remitter bank encoded in a transaction id, or None for ids without a namespace (legacy)."""
    m = _TXN_NAMESPACE.match(transaction_id)
    return m.group(1) if m else None


class DomainEvent(BaseModel):
    """Envelope for every transaction-state event (matches API_CONTRACTS.md example)."""

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    schema_version: str = SCHEMA_VERSION
    timestamp: datetime = Field(default_factory=utcnow)
    transaction_id: str | None = None
    route_id: str | None = None
    bank_id: str | None = None
    amount_paise: int | None = None
    event_type: str
    transaction_version: int | None = None
    correlation_id: str | None = None
    payload: dict = Field(default_factory=dict)


class DispatchJob(BaseModel):
    """One status check approved by the dispatcher (SQS message body)."""

    job_id: str = Field(default_factory=lambda: new_id("job"))
    schema_version: str = SCHEMA_VERSION
    created_at: datetime = Field(default_factory=utcnow)
    transaction_id: str
    route_id: str
    bank_id: str
    expected_version: int
    decision_id: str | None = None
    correlation_id: str | None = None


class SecurityAlert(BaseModel):
    alert_id: str = Field(default_factory=lambda: new_id("alert"))
    schema_version: str = SCHEMA_VERSION
    timestamp: datetime = Field(default_factory=utcnow)
    category: str
    severity: Severity = Severity.WARNING
    actor: str | None = None
    route_id: str | None = None
    transaction_id: str | None = None
    reason: str
    metadata: dict = Field(default_factory=dict)


class AuditRecord(BaseModel):
    event_id: str
    seq: int
    timestamp: datetime
    experiment_id: str | None = None
    correlation_id: str | None = None
    actor: str
    transaction_id: str | None = None
    route_id: str | None = None
    action: str
    decision: str
    reason: str | None = None
    previous_version: int | None = None
    new_version: int | None = None
    metadata: dict = Field(default_factory=dict)
    prev_hash: str
    hash: str


class ExperimentMetadata(BaseModel):
    experiment_id: str
    scenario: str
    policy: str = "mock"
    seed: int | None = None
    config: dict = Field(default_factory=dict)
    started_at: datetime
    ended_at: datetime | None = None
    environment: dict = Field(default_factory=dict)
    status: str = "RUNNING"
    results: dict = Field(default_factory=dict)
