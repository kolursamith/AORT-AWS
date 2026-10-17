"""Response contracts (schema version 1.0)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.aorts.contracts.enums import SCHEMA_VERSION, ErrorCode, SwitchStatus


class ErrorBody(BaseModel):
    """Every non-2xx response from every AORTS service uses this shape."""

    error_code: ErrorCode
    message: str
    retryable: bool = False
    details: dict = Field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION


class TransactionView(BaseModel):
    transaction_id: str
    sender_account_id: str
    beneficiary_account_id: str
    remitter_bank_id: str
    beneficiary_bank_id: str
    route_id: str
    amount_paise: int
    currency: str
    state: str
    financial_outcome: str
    workflow_state: str
    version: int
    idempotency_key: str
    checks_used: int
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime
    next_eligible_at: datetime | None = None
    last_check_at: datetime | None = None
    correlation_id: str | None = None


class PaymentResponse(BaseModel):
    transaction: TransactionView
    replayed: bool = False
    message: str = ""


class SwitchPayResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    transaction_id: str
    status: SwitchStatus
    switch_reference: str | None = None
    reason: str | None = None


class StatusCheckResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    transaction_id: str
    status: SwitchStatus
    switch_reference: str | None = None
    route_id: str | None = None
    reason: str | None = None
    served_from: str = "switch_record"


class BeneficiaryCreditResponse(BaseModel):
    schema_version: str = SCHEMA_VERSION
    transaction_id: str
    operation_id: str
    status: SwitchStatus
    reason: str | None = None
    duplicate: bool = False


class OperationResult(BaseModel):
    """Result of a safety-engine-mediated financial operation."""

    transaction_id: str
    operation_id: str | None = None
    decision: str
    reason: str | None = None
    duplicate: bool = False
    previous_version: int | None = None
    new_version: int | None = None
    state: str | None = None
    financial_outcome: str | None = None


class EligibilityResponse(BaseModel):
    transaction_id: str
    proposed_action: str
    eligible: bool
    reasons: list[str] = Field(default_factory=list)
    current_state: str | None = None
    current_version: int | None = None


class QuotaView(BaseModel):
    bank_id: str
    bank_tokens: float
    bank_rate_per_sec: float
    credits: float
    credit_cap: float
    credit_refill_per_sec: float
    defenses: dict


class RecoveryDecision(BaseModel):
    """Controller -> Track B. Track B treats every field as a *recommendation*."""

    decision_id: str
    schema_version: str = SCHEMA_VERSION
    timestamp: datetime
    route_budgets: dict[str, int] = Field(default_factory=dict)
    transaction_ids: list[str] = Field(default_factory=list)
    policy: str = "unknown"
    fallback: bool = False
    fallback_reason: str | None = None
    predicted: dict = Field(default_factory=dict)
