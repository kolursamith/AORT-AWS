"""Canonical v1 messages shared by the controller and mock banking services."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class EventType(StrEnum):
    PAYMENT_INITIATED = "payment_initiated"
    PAYMENT_TIMEOUT = "payment_timeout"
    STATUS_REQUESTED = "status_requested"
    STATUS_RESOLVED = "status_resolved"
    ESCALATED = "escalated"


class FinancialOutcome(StrEnum):
    UNKNOWN = "unknown"
    CREDITED = "credited"
    NOT_CREDITED = "not_credited"


class WorkflowState(StrEnum):
    INITIATED = "initiated"
    AMBIGUOUS = "ambiguous"
    CHECK_PENDING = "check_pending"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class StatusOutcome(StrEnum):
    UNKNOWN = "unknown"
    CREDITED = "credited"
    NOT_CREDITED = "not_credited"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("occurred_at", "timestamp", "expires_at", check_fields=False)
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a UTC offset")
        return value


class TransactionEvent(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: Identifier
    experiment_id: Identifier
    transaction_id: Identifier
    bank_id: Identifier
    route_id: Identifier
    amount_minor: int = Field(ge=1, le=10**12)
    currency: Currency
    event_type: EventType
    financial_outcome: FinancialOutcome
    workflow_state: WorkflowState
    version: int = Field(ge=1)
    occurred_at: datetime


class RouteTelemetry(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    experiment_id: Identifier
    route_id: Identifier
    timestamp: datetime
    payment_rps: float = Field(ge=0, le=1_000_000)
    check_rps: float = Field(ge=0, le=1_000_000)
    observed_throughput_rps: float = Field(ge=0, le=1_000_000)
    p95_latency_ms: float = Field(ge=0, le=3_600_000)
    timeout_rate: float = Field(ge=0, le=1)
    queue_depth: int = Field(ge=0, le=100_000_000)
    source_id: Identifier
    sequence: int = Field(ge=0)


class RecoveryDecision(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    experiment_id: Identifier
    route_id: Identifier
    decision_id: Identifier
    budget_rps: float = Field(ge=0, le=1_000_000)
    selected_transaction_ids: list[Identifier] = Field(max_length=10_000)
    predicted_delay_ms: float = Field(ge=0, le=3_600_000)
    predicted_new_ambiguous: int = Field(ge=0)
    decision_reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    expires_at: datetime

    @field_validator("selected_transaction_ids")
    @classmethod
    def unique_transactions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("selected transaction IDs must be unique")
        return value


class StatusCheckResult(ContractModel):
    transaction_id: Identifier
    route_id: Identifier
    check_id: Identifier
    outcome: StatusOutcome
    authoritative: bool
    response_version: int = Field(ge=1)
    latency_ms: float = Field(ge=0, le=3_600_000)
    evidence_source_id: Identifier | None = None

    @model_validator(mode="after")
    def authoritative_requires_evidence(self) -> StatusCheckResult:
        if self.authoritative and not self.evidence_source_id:
            raise ValueError("authoritative results require a validated evidence source")
        return self
