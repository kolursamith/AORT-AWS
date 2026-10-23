"""Request contracts (schema version 1.0). Amounts are integer paise, never floats."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from backend.aorts.contracts.enums import SCHEMA_VERSION, SwitchStatus

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,79}$"
BANK_PATTERN = r"^[A-Z][A-Z0-9]{0,7}$"
ROUTE_PATTERN = r"^[A-Z][A-Z0-9]{0,7}-[A-Z][A-Z0-9]{0,7}$"
MAX_AMOUNT_PAISE = 10_000_000_00  # 1 crore rupees: synthetic upper bound


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PaymentRequest(StrictModel):
    """Client -> remitter: start a synthetic payment."""

    sender_account_id: str = Field(pattern=ID_PATTERN)
    beneficiary_account_id: str = Field(pattern=ID_PATTERN)
    beneficiary_bank_id: str = Field(pattern=BANK_PATTERN)
    amount_paise: StrictInt = Field(gt=0, le=MAX_AMOUNT_PAISE)
    currency: str = Field(default="INR", pattern=r"^INR$")
    idempotency_key: str = Field(min_length=8, max_length=80, pattern=ID_PATTERN)
    correlation_id: str | None = Field(default=None, pattern=ID_PATTERN)
    deadline_seconds: int | None = Field(default=None, gt=0, le=7 * 24 * 3600)


class SwitchPayRequest(StrictModel):
    """Remitter -> switch."""

    schema_version: str = SCHEMA_VERSION
    transaction_id: str = Field(pattern=ID_PATTERN)
    remitter_bank_id: str = Field(pattern=BANK_PATTERN)
    beneficiary_bank_id: str = Field(pattern=BANK_PATTERN)
    beneficiary_account_id: str = Field(pattern=ID_PATTERN)
    route_id: str = Field(pattern=ROUTE_PATTERN)
    amount_paise: StrictInt = Field(gt=0, le=MAX_AMOUNT_PAISE)
    correlation_id: str | None = Field(default=None, pattern=ID_PATTERN)


class StatusCheckRequest(StrictModel):
    """Remitter bank (or dispatcher acting for it) -> switch."""

    schema_version: str = SCHEMA_VERSION
    transaction_id: str = Field(pattern=ID_PATTERN)
    bank_id: str = Field(pattern=BANK_PATTERN)
    route_id: str = Field(pattern=ROUTE_PATTERN)
    correlation_id: str | None = Field(default=None, pattern=ID_PATTERN)


class BeneficiaryCreditRequest(StrictModel):
    """Switch -> beneficiary bank."""

    schema_version: str = SCHEMA_VERSION
    transaction_id: str = Field(pattern=ID_PATTERN)
    operation_id: str = Field(pattern=ID_PATTERN)
    beneficiary_account_id: str = Field(pattern=ID_PATTERN)
    amount_paise: StrictInt = Field(gt=0, le=MAX_AMOUNT_PAISE)
    route_id: str = Field(pattern=ROUTE_PATTERN)


class StatusEvidence(StrictModel):
    """An authenticated outcome statement about one transaction.

    Created only from a signature-verified switch response (or a signed
    status-response event). ``source`` is the *authenticated* identity, never
    a value copied from an unauthenticated payload.
    """

    transaction_id: str = Field(pattern=ID_PATTERN)
    status: SwitchStatus
    source: str
    switch_reference: str | None = None
    observed_at: datetime
    authenticated: bool = False
    reason: str | None = None
    # v1.1 provenance: the EvidenceRecord written when this response was received and verified.
    evidence_id: str | None = None


class TransactionStateEvent(StrictModel):
    """Signed status-response / state-change event delivered to the remitter.

    This is the object replayed in Attack 3.
    """

    event_id: str = Field(pattern=ID_PATTERN)
    schema_version: str = SCHEMA_VERSION
    timestamp: datetime
    transaction_id: str = Field(pattern=ID_PATTERN)
    route_id: str = Field(pattern=ROUTE_PATTERN)
    bank_id: str = Field(pattern=BANK_PATTERN)
    amount_paise: StrictInt = Field(gt=0, le=MAX_AMOUNT_PAISE)
    event_type: str
    transaction_version: int = Field(ge=1)
    correlation_id: str | None = Field(default=None, pattern=ID_PATTERN)
    payload: dict = Field(default_factory=dict)


class ReversalRequest(StrictModel):
    operation_id: str = Field(pattern=ID_PATTERN)
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=200)
    correlation_id: str | None = Field(default=None, pattern=ID_PATTERN)


class EligibilityRequest(StrictModel):
    """Track A -> Track B: may this operation be executed right now?"""

    transaction_id: str = Field(pattern=ID_PATTERN)
    proposed_action: str = Field(pattern=r"^(STATUS_CHECK|REVERSAL|SETTLE|ESCALATE)$")
    expected_version: int | None = Field(default=None, ge=1)


class RouteTelemetry(StrictModel):
    """One route-health observation reported to the twin (Attack 2 surface)."""

    event_id: str = Field(pattern=ID_PATTERN)
    schema_version: str = SCHEMA_VERSION
    source_id: str = Field(pattern=ID_PATTERN)
    route_id: str = Field(pattern=ROUTE_PATTERN)
    sequence: int = Field(ge=0)
    timestamp: datetime
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    throughput_tps: float | None = None
    timeout_rate: float | None = None
    backlog: int | None = None
    health_score: float | None = None
    correlation_id: str | None = Field(default=None, pattern=ID_PATTERN)

    @field_validator("timestamp")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError(
                "timestamp must include a UTC offset (ISO-8601, e.g. 2026-10-14T10:00:15Z)"
            )
        return value


class RecoveryDecisionRequest(StrictModel):
    """Track B -> controller: ask for a recovery decision for one tick."""

    decision_request_id: str = Field(pattern=ID_PATTERN)
    timestamp: datetime
    routes: list[dict] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)
    correlation_id: str | None = Field(default=None, pattern=ID_PATTERN)


class DispatchResultObservation(StrictModel):
    """Track B -> controller: what happened to a dispatched status check."""

    transaction_id: str = Field(pattern=ID_PATTERN)
    route_id: str = Field(pattern=ROUTE_PATTERN)
    admitted: bool
    status: str | None = None
    latency_ms: float | None = None
    rejection_reason: str | None = None
    timestamp: datetime


class FaultConfig(StrictModel):
    """Application-level fault injection for one target (route/bank/service)."""

    target: str = Field(
        pattern=r"^(switch|remitter|beneficiary:[A-Z][A-Z0-9]{0,7}|route:[A-Z][A-Z0-9]{0,7})$"
    )
    latency_ms: int = Field(default=0, ge=0, le=60_000)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    drop_response_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    unavailable: bool = False
    capacity_workers: int | None = Field(default=None, ge=1, le=256)
    note: str | None = Field(default=None, max_length=200)


class DefenseConfig(StrictModel):
    rate_limiting: bool = True
    tier2_credits: bool = True
    telemetry_validation: bool = True
    replay_protection: bool = True


class AttackRunRequest(StrictModel):
    experiment_id: str | None = Field(default=None, pattern=ID_PATTERN)
    duration_seconds: float = Field(default=5.0, gt=0, le=120)
    rate_per_second: float = Field(default=50.0, gt=0, le=1000)
    target_route: str = Field(default="A3-B", pattern=ROUTE_PATTERN)
    defenses_enabled: bool | None = None
    seed: int = 42
