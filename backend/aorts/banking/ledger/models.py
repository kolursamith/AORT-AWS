"""PostgreSQL schema: the authoritative financial ledger and supporting tables.

All money columns are BIGINT paise. Financial idempotency is enforced by
database constraints, never by an in-memory cache:

* ``financial_operations.operation_id`` is the primary key;
* ``financial_operations.idempotency_key`` is unique;
* ``(transaction_id, operation_type, bank_id)`` is unique, so a transaction can be debited,
  settled, reversed, or credited at most once, whatever operation id is used.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BankIdentity(Base):
    """Synthetic service identities. Secrets are NOT stored here, only metadata."""

    __tablename__ = "bank_identities"
    identity_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(24))  # REMITTER | BENEFICIARY | SWITCH | SERVICE
    key_ids: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _ts()


class Account(Base):
    __tablename__ = "accounts"
    account_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    bank_id: Mapped[str] = mapped_column(ForeignKey("bank_identities.identity_id"), index=True)
    holder_name: Mapped[str] = mapped_column(String(120))
    account_type: Mapped[str] = mapped_column(String(16))
    balance_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    # Balance at seeding time; reconciliation checks balance == opening + credits - debits.
    opening_balance_paise: Mapped[int] = mapped_column(BigInteger, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()

    __table_args__ = (
        CheckConstraint(
            "account_type <> 'CUSTOMER' OR balance_paise >= 0",
            name="ck_accounts_customer_non_negative",
        ),
    )


class Transaction(Base):
    __tablename__ = "transactions"
    transaction_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    sender_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.account_id"))
    beneficiary_account_id: Mapped[str] = mapped_column(String(80))
    remitter_bank_id: Mapped[str] = mapped_column(String(16))
    beneficiary_bank_id: Mapped[str] = mapped_column(String(16))
    route_id: Mapped[str] = mapped_column(String(20), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    state: Mapped[str] = mapped_column(String(32))
    financial_outcome: Mapped[str] = mapped_column(String(16))
    workflow_state: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    checks_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ambiguous_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_eligible_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    switch_reference: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    # v1.1 provenance (migration 0002; NULL on rows created before it)
    experiment_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    check_in_flight_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_transactions_amount_positive"),
        CheckConstraint("version >= 1", name="ck_transactions_version_positive"),
        Index("ix_transactions_workflow_eligible", "workflow_state", "next_eligible_at"),
    )


class FinancialOperation(Base):
    __tablename__ = "financial_operations"
    operation_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String(80), index=True)
    bank_id: Mapped[str] = mapped_column(String(16))
    operation_type: Mapped[str] = mapped_column(String(16))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    references_operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("financial_operations.operation_id"), nullable=True
    )
    result: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = _ts()

    __table_args__ = (
        UniqueConstraint(
            "transaction_id", "operation_type", "bank_id", name="uq_fin_ops_once_per_txn"
        ),
        CheckConstraint("amount_paise > 0", name="ck_fin_ops_amount_positive"),
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    entry_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(
        ForeignKey("financial_operations.operation_id"), index=True
    )
    transaction_id: Mapped[str] = mapped_column(String(80), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.account_id"), index=True)
    direction: Mapped[str] = mapped_column(String(6))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _ts()

    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_ledger_amount_positive"),
        CheckConstraint("direction IN ('DEBIT','CREDIT')", name="ck_ledger_direction"),
    )


class TransactionEvent(Base):
    """Append-only history of every accepted state transition."""

    __tablename__ = "transaction_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True)
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.transaction_id"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(40))
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_state: Mapped[str] = mapped_column(String(32))
    previous_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_version: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = _ts()


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True)
    aggregate_id: Mapped[str] = mapped_column(String(80), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()
    available_at: Mapped[datetime] = _ts()
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dead_lettered: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (Index("ix_outbox_unpublished", "published_at", "available_at"),)


class ProcessedEvent(Base):
    """Idempotent-consumer ledger: (consumer, event_id) can be processed once."""

    __tablename__ = "processed_events"
    consumer: Mapped[str] = mapped_column(String(40), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    transaction_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    processed_at: Mapped[datetime] = _ts()


class AuditEvent(Base):
    """Hash-chained audit log (tamper-evident, NOT tamper-proof; see SECURITY.md)."""

    __tablename__ = "audit_events"
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(80), unique=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    experiment_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    actor: Mapped[str] = mapped_column(String(40))
    transaction_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    route_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    previous_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"
    experiment_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    scenario: Mapped[str] = mapped_column(String(60))
    policy: Mapped[str] = mapped_column(String(40), default="mock")
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    environment: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = _ts()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SwitchTransaction(Base):
    """The mock switch's own authoritative record used to answer status checks."""

    __tablename__ = "switch_transactions"
    transaction_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    remitter_bank_id: Mapped[str] = mapped_column(String(16))
    beneficiary_bank_id: Mapped[str] = mapped_column(String(16))
    beneficiary_account_id: Mapped[str] = mapped_column(String(80))
    route_id: Mapped[str] = mapped_column(String(20), index=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    # NOT_FORWARDED | FORWARDING | DONE. A beneficiary is only fenced once forwarding is DONE.
    forward_state: Mapped[str] = mapped_column(String(16), default="NOT_FORWARDED")
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    switch_reference: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()


class RuntimeConfig(Base):
    """Shared runtime switches (fault injection, defense toggles), cached ~1 s per process."""

    __tablename__ = "runtime_config"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_by: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = _ts()


class TwinState(Base):
    """Local stand-in for the DynamoDB twin-state table (same conditional-write semantics)."""

    __tablename__ = "twin_state"
    pk: Mapped[str] = mapped_column(String(120), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = _ts()


class TelemetryObservation(Base):
    __tablename__ = "telemetry_observations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(80), index=True)
    source_id: Mapped[str] = mapped_column(String(80))
    route_id: Mapped[str] = mapped_column(String(20), index=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    reported: Mapped[dict] = mapped_column(JSON)
    measured: Mapped[dict] = mapped_column(JSON, default=dict)
    verdict: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column()
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    experiment_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = _ts()


# --------------------------------------------------------------------------- v1.1 provenance


class EvidenceRecord(Base):
    """A switch response exactly as received and verified (migration 0002).

    The signature material is stored as received; ``verified`` is the result of
    ``request_auth.verify_response`` at receipt time, never a caller's assertion.
    """

    __tablename__ = "evidence_records"
    evidence_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(String(80), index=True)
    evidence_kind: Mapped[str] = mapped_column(String(40))
    issuer_id: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(80))
    http_status: Mapped[int] = mapped_column(Integer)
    reported_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reported_transaction_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    body_sha256: Mapped[str] = mapped_column(String(64))
    response_timestamp: Mapped[str | None] = mapped_column(String(20), nullable=True)
    key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signature_hex: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signature_alg: Mapped[str] = mapped_column(String(32))
    signed_fields: Mapped[list] = mapped_column(JSON)
    verified: Mapped[bool] = mapped_column(Boolean)
    verifier_id: Mapped[str] = mapped_column(String(40))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    check_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    latency_ms: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = _ts()

    __table_args__ = (
        CheckConstraint(
            "NOT verified OR (key_id IS NOT NULL AND signature_hex IS NOT NULL)",
            name="ck_evidence_verified_has_signature",
        ),
        CheckConstraint(
            "evidence_kind IN ('ORIGINAL_PAYMENT_RESPONSE','STATUS_CHECK_RESPONSE',"
            "'RECONCILIATION_RECORD')",
            name="ck_evidence_kind",
        ),
    )


class TelemetryWindow(Base):
    """One measured telemetry window of the v1.1 producer (migration 0002)."""

    __tablename__ = "v11_telemetry_windows"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(String(80))
    source_id: Mapped[str] = mapped_column(String(80))
    route_id: Mapped[str] = mapped_column(String(20))
    sequence: Mapped[int] = mapped_column(BigInteger)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    measurement: Mapped[dict] = mapped_column(JSON)
    verdict: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column()
    reasons: Mapped[list] = mapped_column(JSON)
    cross_check: Mapped[str] = mapped_column(String(16))
    validator_id: Mapped[str] = mapped_column(String(40))
    validator_version: Mapped[str] = mapped_column(String(40))
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("experiment_id", "source_id", "sequence", name="uq_v11_window_seq"),
        UniqueConstraint("experiment_id", "source_id", "window_start", name="uq_v11_window_start"),
        CheckConstraint("window_end > window_start", name="ck_v11_window_positive"),
        CheckConstraint("sequence >= 0", name="ck_v11_window_sequence"),
    )


class V11DeliveryState(Base):
    __tablename__ = "v11_delivery_state"
    stream: Mapped[str] = mapped_column(String(40), primary_key=True)
    last_id: Mapped[int] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = _ts()


class V11DeadLetter(Base):
    __tablename__ = "v11_dead_letters"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stream: Mapped[str] = mapped_column(String(40))
    ref_id: Mapped[str] = mapped_column(String(80))
    error_code: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = _ts()

    __table_args__ = (UniqueConstraint("stream", "ref_id", name="uq_v11_dead_letter"),)
