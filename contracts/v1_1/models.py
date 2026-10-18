"""AORTS shared contract v1.1: provisional working interface baseline.

These Pydantic models are wire formats only. They perform structural and cross-field
validation; they never authenticate, verify signatures, read clocks, touch databases or
make network calls. Authentication and signature verification belong to Track B at the
trust boundary. See contracts/v1_1/README.md for semantics and state tables.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)

from contracts.v1_1.errors import (
    CHECK_COUNTING,
    CHECK_REJECTION_CODES,
    CHECK_TIMEOUT_CODES,
    CREDIT_REJECTION_CODES,
    CheckCounting,
    ErrorCode,
)

SCHEMA_VERSION = "1.1"
SchemaVersion = Literal["1.1"]

# Floating-point comparisons between rates and the counts they were derived from.
_RATE_TOLERANCE = 1e-9


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
VersionLabel = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._+:-]*$"),
]
Text = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
Signature = Annotated[
    str, StringConstraints(min_length=1, max_length=2048, pattern=r"^[A-Za-z0-9+/=_-]+$")
]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_as_utc)]
NonNegativeFloat = Annotated[float, Field(ge=0, strict=True)]
PositiveFloat = Annotated[float, Field(gt=0, strict=True)]
Probability = Annotated[float, Field(ge=0, le=1, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


class ContractModel(BaseModel):
    """Immutable, closed-world base: unknown fields, NaN and infinity are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


# --------------------------------------------------------------------------- enums


class FinancialOutcome(StrEnum):
    """Observed evidence about the beneficiary credit. UNKNOWN is not FAILURE."""

    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class LedgerState(StrEnum):
    """Authoritative PostgreSQL ledger state, owned by Track B."""

    DEBITED = "DEBITED"
    CONFIRMED_SUCCESS = "CONFIRMED_SUCCESS"
    CONFIRMED_FAILED = "CONFIRMED_FAILED"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"
    REVERSED = "REVERSED"
    CLOSED = "CLOSED"


class RecoveryWorkflowState(StrEnum):
    """Non-authoritative recovery status in the Track A transaction projection."""

    ACTIVE = "ACTIVE"
    AMBIGUOUS = "AMBIGUOUS"
    CHECK_IN_FLIGHT = "CHECK_IN_FLIGHT"
    ESCALATED = "ESCALATED"
    HANDOFF_RECONCILIATION = "HANDOFF_RECONCILIATION"
    QUARANTINED = "QUARANTINED"
    CLOSED = "CLOSED"


class TransactionEventType(StrEnum):
    """Facts already committed by the backend. None of these is a command."""

    PAYMENT_INITIATED = "PAYMENT_INITIATED"
    PAYMENT_RESPONSE_RECEIVED = "PAYMENT_RESPONSE_RECEIVED"
    PAYMENT_TIMED_OUT = "PAYMENT_TIMED_OUT"
    STATUS_CHECK_DISPATCHED = "STATUS_CHECK_DISPATCHED"
    STATUS_CHECK_ANSWERED = "STATUS_CHECK_ANSWERED"
    STATUS_CHECK_REJECTED = "STATUS_CHECK_REJECTED"
    STATUS_CHECK_TIMED_OUT = "STATUS_CHECK_TIMED_OUT"
    REVERSAL_COMPLETED = "REVERSAL_COMPLETED"
    RECONCILIATION_HANDOFF = "RECONCILIATION_HANDOFF"
    RECONCILIATION_RESOLVED = "RECONCILIATION_RESOLVED"
    EVIDENCE_CONFLICT_RECORDED = "EVIDENCE_CONFLICT_RECORDED"
    TRANSACTION_CLOSED = "TRANSACTION_CLOSED"


class EvidenceKind(StrEnum):
    ORIGINAL_PAYMENT_RESPONSE = "ORIGINAL_PAYMENT_RESPONSE"
    STATUS_CHECK_RESPONSE = "STATUS_CHECK_RESPONSE"
    RECONCILIATION_RECORD = "RECONCILIATION_RECORD"


class CheckResultKind(StrEnum):
    AUTHORITATIVE_SUCCESS = "AUTHORITATIVE_SUCCESS"
    AUTHORITATIVE_FAILURE = "AUTHORITATIVE_FAILURE"
    PENDING = "PENDING"
    UNKNOWN = "UNKNOWN"


class ObservationScope(StrEnum):
    """What a telemetry sample can see. A real remitter bank only has its own traffic."""

    REMITTER_OWN_TRAFFIC = "REMITTER_OWN_TRAFFIC"
    SWITCH_ROUTE = "SWITCH_ROUTE"
    SWITCH_GLOBAL = "SWITCH_GLOBAL"


class TelemetryTrustStatus(StrEnum):
    AUTHENTICATED = "AUTHENTICATED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    UNKNOWN_SOURCE = "UNKNOWN_SOURCE"


class TelemetryValidationFlag(StrEnum):
    RANGE_VIOLATION = "RANGE_VIOLATION"
    CROSS_CHECK_DEVIATION = "CROSS_CHECK_DEVIATION"
    STATISTICAL_ANOMALY = "STATISTICAL_ANOMALY"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    CLOCK_SKEW = "CLOCK_SKEW"


class CrossCheckStatus(StrEnum):
    CONSISTENT = "CONSISTENT"
    INCONSISTENT = "INCONSISTENT"
    UNAVAILABLE = "UNAVAILABLE"


class FreshnessStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    NO_DATA = "NO_DATA"


class QuarantineReason(StrEnum):
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    CONFLICTING_EVENT = "CONFLICTING_EVENT"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"


class DecisionMode(StrEnum):
    """Accepted decision 5: no configured conservative fallback mode exists yet."""

    NORMAL = "NORMAL"
    FAIL_CLOSED = "FAIL_CLOSED"
    WARMING_UP = "WARMING_UP"


class DecisionReason(StrEnum):
    SAFE_BUDGET_SELECTED = "SAFE_BUDGET_SELECTED"
    NO_SAFE_BUDGET = "NO_SAFE_BUDGET"
    NO_ELIGIBLE_TRANSACTIONS = "NO_ELIGIBLE_TRANSACTIONS"
    NO_CAPACITY_ESTIMATE = "NO_CAPACITY_ESTIMATE"
    TELEMETRY_STALE = "TELEMETRY_STALE"
    TELEMETRY_UNTRUSTED = "TELEMETRY_UNTRUSTED"
    CONTROLLER_WARMING_UP = "CONTROLLER_WARMING_UP"
    BOOTSTRAP_FAIL_CLOSED = "BOOTSTRAP_FAIL_CLOSED"


class Tier2LimitClass(StrEnum):
    CALM = "CALM"
    NORMAL = "NORMAL"
    STORM = "STORM"


class TimingRegime(StrEnum):
    TIER1_COMPLIANT = "TIER1_COMPLIANT"
    RESEARCH_SCALED = "RESEARCH_SCALED"
    TIER2_EXPERIMENTAL = "TIER2_EXPERIMENTAL"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    SOURCE_CLAIMED_UNVERIFIED = "SOURCE_CLAIMED_UNVERIFIED"
    RESEARCH_PARAMETER = "RESEARCH_PARAMETER"


class FirstCheckReference(StrEnum):
    INITIATED = "INITIATED"
    TIMED_OUT = "TIMED_OUT"


class DeadlineRuleKind(StrEnum):
    FIXED_DURATION = "FIXED_DURATION"
    REGULATORY_CALENDAR = "REGULATORY_CALENDAR"


# Financial outcomes consistent with each authoritative ledger state.
OUTCOMES_FOR_LEDGER_STATE: dict[LedgerState, frozenset[FinancialOutcome]] = {
    LedgerState.DEBITED: frozenset({FinancialOutcome.UNKNOWN, FinancialOutcome.PENDING}),
    LedgerState.RECONCILIATION_PENDING: frozenset(
        {FinancialOutcome.UNKNOWN, FinancialOutcome.PENDING}
    ),
    LedgerState.CONFIRMED_SUCCESS: frozenset({FinancialOutcome.SUCCESS}),
    LedgerState.CONFIRMED_FAILED: frozenset({FinancialOutcome.FAILURE}),
    LedgerState.REVERSED: frozenset({FinancialOutcome.FAILURE}),
    LedgerState.CLOSED: frozenset({FinancialOutcome.SUCCESS, FinancialOutcome.FAILURE}),
}
# Outcomes that must be backed by an evidence reference.
EVIDENCED_OUTCOMES = frozenset(
    {FinancialOutcome.SUCCESS, FinancialOutcome.FAILURE, FinancialOutcome.PENDING}
)
TERMINAL_OUTCOMES = frozenset({FinancialOutcome.SUCCESS, FinancialOutcome.FAILURE})
_UNRESOLVED_OUTCOMES = frozenset({FinancialOutcome.UNKNOWN, FinancialOutcome.PENDING})
_OUTCOME_FOR_CHECK_RESULT: dict[CheckResultKind, frozenset[FinancialOutcome]] = {
    CheckResultKind.AUTHORITATIVE_SUCCESS: frozenset({FinancialOutcome.SUCCESS}),
    CheckResultKind.AUTHORITATIVE_FAILURE: frozenset({FinancialOutcome.FAILURE}),
    CheckResultKind.PENDING: frozenset({FinancialOutcome.PENDING}),
    CheckResultKind.UNKNOWN: _UNRESOLVED_OUTCOMES,
}
_EVIDENCED_CHECK_RESULTS = frozenset(
    {
        CheckResultKind.AUTHORITATIVE_SUCCESS,
        CheckResultKind.AUTHORITATIVE_FAILURE,
        CheckResultKind.PENDING,
    }
)
_STATUS_CHECK_EVENTS = frozenset(
    {
        TransactionEventType.STATUS_CHECK_DISPATCHED,
        TransactionEventType.STATUS_CHECK_ANSWERED,
        TransactionEventType.STATUS_CHECK_REJECTED,
        TransactionEventType.STATUS_CHECK_TIMED_OUT,
    }
)
_AMBIGUITY_WORKFLOW_STATES = frozenset(
    {
        RecoveryWorkflowState.AMBIGUOUS,
        RecoveryWorkflowState.CHECK_IN_FLIGHT,
        RecoveryWorkflowState.ESCALATED,
        RecoveryWorkflowState.HANDOFF_RECONCILIATION,
    }
)


def _check_outcome_matches_ledger(ledger_state: LedgerState, outcome: FinancialOutcome) -> None:
    if outcome not in OUTCOMES_FOR_LEDGER_STATE[ledger_state]:
        raise ValueError(
            f"financial_outcome {outcome} is inconsistent with ledger state {ledger_state}"
        )


def _check_deadline_pair(deadline_at: datetime | None, policy_id: str | None) -> None:
    if (deadline_at is None) != (policy_id is None):
        raise ValueError("deadline_at and deadline_policy_id must be provided together")


# ------------------------------------------------------------------ identity & events


class RouteDescriptor(ContractModel):
    """Registry entry that gives an opaque route_id its endpoints (accepted decision 2)."""

    schema_version: SchemaVersion
    experiment_id: Identifier
    route_id: Identifier
    remitter_bank_id: Identifier
    switch_id: Identifier
    beneficiary_bank_id: Identifier

    @model_validator(mode="after")
    def _distinct_endpoints(self) -> "RouteDescriptor":
        if self.remitter_bank_id == self.beneficiary_bank_id:
            raise ValueError("remitter_bank_id and beneficiary_bank_id must differ")
        return self


class EvidenceReference(ContractModel):
    """Pointer to evidence stored and verified by Track B. Carries no signature."""

    evidence_id: Identifier
    evidence_kind: EvidenceKind
    issuer_id: Identifier
    response_version: NonNegativeInt


class EvidenceSignature(ContractModel):
    """Signing metadata only (accepted decision 7). Never verified inside a model."""

    signature_alg: Identifier
    key_id: Identifier
    signature: Signature
    signed_fields: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_signed_fields(self) -> "EvidenceSignature":
        if duplicates := _duplicates(self.signed_fields):
            raise ValueError(f"signed_fields contains duplicates: {duplicates}")
        return self


class TransactionEvent(ContractModel):
    """Notification of a fact committed in the authoritative ledger (via the outbox)."""

    schema_version: SchemaVersion
    experiment_id: Identifier
    event_id: Identifier
    transaction_id: Identifier
    route_id: Identifier
    remitter_bank_id: Identifier
    beneficiary_bank_id: Identifier
    event_type: TransactionEventType
    ledger_state: LedgerState
    financial_outcome: FinancialOutcome
    state_version: PositiveInt
    amount_minor: PositiveInt
    currency: CurrencyCode = "INR"
    event_timestamp: UtcDatetime
    source_id: Identifier
    source_sequence: NonNegativeInt
    correlation_id: Identifier
    causation_id: Identifier | None = None
    check_id: Identifier | None = None
    check_result: CheckResultKind | None = None
    result_code: ErrorCode | None = None
    counted_check_attempts: NonNegativeInt
    evidence_reference: EvidenceReference | None = None
    deadline_at: UtcDatetime | None = None
    deadline_policy_id: Identifier | None = None

    @model_validator(mode="after")
    def _semantics(self) -> "TransactionEvent":
        kind = self.event_type
        outcome = self.financial_outcome
        if self.remitter_bank_id == self.beneficiary_bank_id:
            raise ValueError("remitter_bank_id and beneficiary_bank_id must differ")
        _check_deadline_pair(self.deadline_at, self.deadline_policy_id)

        timeout_kinds = (
            TransactionEventType.PAYMENT_TIMED_OUT,
            TransactionEventType.STATUS_CHECK_TIMED_OUT,
        )
        if kind is TransactionEventType.PAYMENT_TIMED_OUT and outcome is not (
            FinancialOutcome.UNKNOWN
        ):
            raise ValueError(
                f"{ErrorCode.TIMEOUT_CANNOT_IMPLY_OUTCOME}: a payment timeout leaves the "
                "financial outcome UNKNOWN"
            )
        if kind in timeout_kinds and outcome in TERMINAL_OUTCOMES:
            raise ValueError(
                f"{ErrorCode.TIMEOUT_CANNOT_IMPLY_OUTCOME}: a timeout cannot establish {outcome}"
            )

        _check_outcome_matches_ledger(self.ledger_state, outcome)
        if outcome in EVIDENCED_OUTCOMES and self.evidence_reference is None:
            raise ValueError(
                f"{ErrorCode.EVIDENCE_REQUIRED}: outcome {outcome} requires evidence_reference"
            )

        is_check_event = kind in _STATUS_CHECK_EVENTS
        if is_check_event != (self.check_id is not None):
            raise ValueError("check_id is required for and only allowed on STATUS_CHECK_* events")
        is_answer = kind is TransactionEventType.STATUS_CHECK_ANSWERED
        if is_answer != (self.check_result is not None):
            raise ValueError(
                "check_result is required for and only allowed on STATUS_CHECK_ANSWERED"
            )
        coded_kinds = (
            TransactionEventType.STATUS_CHECK_REJECTED,
            TransactionEventType.STATUS_CHECK_TIMED_OUT,
        )
        if (kind in coded_kinds) != (self.result_code is not None):
            raise ValueError(
                "result_code is required for and only allowed on STATUS_CHECK_REJECTED and "
                "STATUS_CHECK_TIMED_OUT"
            )

        expected = _expected_state_for_event(kind)
        if expected is not None:
            allowed_ledger, allowed_outcomes = expected
            if self.ledger_state not in allowed_ledger:
                raise ValueError(f"{kind} requires ledger_state in {sorted(allowed_ledger)}")
            if outcome not in allowed_outcomes:
                raise ValueError(f"{kind} requires financial_outcome in {sorted(allowed_outcomes)}")

        if kind is TransactionEventType.PAYMENT_INITIATED and (
            self.state_version != 1 or self.counted_check_attempts != 0
        ):
            raise ValueError("PAYMENT_INITIATED must have state_version 1 and no counted checks")
        if (
            self.check_result is not None
            and outcome not in _OUTCOME_FOR_CHECK_RESULT[self.check_result]
        ):
            raise ValueError(
                f"check_result {self.check_result} is inconsistent with outcome {outcome}"
            )
        if (
            kind is TransactionEventType.STATUS_CHECK_TIMED_OUT
            and self.result_code not in CHECK_TIMEOUT_CODES
        ):
            raise ValueError(
                f"STATUS_CHECK_TIMED_OUT requires result_code in {sorted(CHECK_TIMEOUT_CODES)}"
            )
        if (
            kind is TransactionEventType.STATUS_CHECK_REJECTED
            and self.result_code not in CHECK_REJECTION_CODES
        ):
            raise ValueError(
                "STATUS_CHECK_REJECTED requires a pre-dispatch or dispatcher-declared code"
            )

        introduced_kind = _evidence_kind_introduced_by(kind, self.check_result)
        if (
            introduced_kind is not None
            and self.evidence_reference is not None
            and self.evidence_reference.evidence_kind is not introduced_kind
        ):
            raise ValueError(f"{kind} must reference evidence of kind {introduced_kind}")
        return self


def _expected_state_for_event(
    kind: TransactionEventType,
) -> tuple[frozenset[LedgerState], frozenset[FinancialOutcome]] | None:
    """Ledger states and outcomes an event type may report, where the type constrains them."""

    unresolved_debited = (frozenset({LedgerState.DEBITED}), _UNRESOLVED_OUTCOMES)
    rules: dict[
        TransactionEventType, tuple[frozenset[LedgerState], frozenset[FinancialOutcome]]
    ] = {
        TransactionEventType.PAYMENT_INITIATED: (
            frozenset({LedgerState.DEBITED}),
            frozenset({FinancialOutcome.UNKNOWN}),
        ),
        TransactionEventType.PAYMENT_TIMED_OUT: (
            frozenset({LedgerState.DEBITED}),
            frozenset({FinancialOutcome.UNKNOWN}),
        ),
        TransactionEventType.PAYMENT_RESPONSE_RECEIVED: (
            frozenset(
                {
                    LedgerState.DEBITED,
                    LedgerState.CONFIRMED_SUCCESS,
                    LedgerState.CONFIRMED_FAILED,
                    LedgerState.REVERSED,
                }
            ),
            EVIDENCED_OUTCOMES,
        ),
        TransactionEventType.STATUS_CHECK_DISPATCHED: unresolved_debited,
        TransactionEventType.STATUS_CHECK_REJECTED: unresolved_debited,
        TransactionEventType.STATUS_CHECK_TIMED_OUT: unresolved_debited,
        TransactionEventType.REVERSAL_COMPLETED: (
            frozenset({LedgerState.REVERSED}),
            frozenset({FinancialOutcome.FAILURE}),
        ),
        TransactionEventType.RECONCILIATION_HANDOFF: (
            frozenset({LedgerState.RECONCILIATION_PENDING}),
            _UNRESOLVED_OUTCOMES,
        ),
        TransactionEventType.RECONCILIATION_RESOLVED: (
            frozenset(
                {
                    LedgerState.CONFIRMED_SUCCESS,
                    LedgerState.CONFIRMED_FAILED,
                    LedgerState.REVERSED,
                }
            ),
            TERMINAL_OUTCOMES,
        ),
        TransactionEventType.TRANSACTION_CLOSED: (
            frozenset({LedgerState.CLOSED}),
            TERMINAL_OUTCOMES,
        ),
    }
    return rules.get(kind)


def _evidence_kind_introduced_by(
    kind: TransactionEventType, check_result: CheckResultKind | None
) -> EvidenceKind | None:
    if kind is TransactionEventType.PAYMENT_RESPONSE_RECEIVED:
        return EvidenceKind.ORIGINAL_PAYMENT_RESPONSE
    if kind is TransactionEventType.RECONCILIATION_RESOLVED:
        return EvidenceKind.RECONCILIATION_RECORD
    if kind is TransactionEventType.STATUS_CHECK_ANSWERED and check_result in (
        _EVIDENCED_CHECK_RESULTS
    ):
        return EvidenceKind.STATUS_CHECK_RESPONSE
    return None


# ------------------------------------------------------------------------ telemetry


class RouteTelemetry(ContractModel):
    """One observation window from one source. null means not observed; 0 means measured 0."""

    schema_version: SchemaVersion
    experiment_id: Identifier
    route_id: Identifier
    source_id: Identifier
    observation_scope: ObservationScope
    sequence: NonNegativeInt
    observation_window_start: UtcDatetime
    observation_window_end: UtcDatetime
    observation_timestamp: UtcDatetime
    payment_rps: NonNegativeFloat
    check_rps: NonNegativeFloat
    observed_throughput_rps: NonNegativeFloat | None
    completed_payment_rps: NonNegativeFloat | None = None
    completed_check_rps: NonNegativeFloat | None = None
    p50_latency_ms: NonNegativeFloat | None
    p95_latency_ms: NonNegativeFloat | None
    latency_sample_count: NonNegativeInt
    timeout_rate: Probability | None
    timeout_count: NonNegativeInt | None = None
    timeout_denominator_count: NonNegativeInt | None = None
    queue_depth: NonNegativeInt | None

    @model_validator(mode="after")
    def _semantics(self) -> "RouteTelemetry":
        if self.observation_window_end <= self.observation_window_start:
            raise ValueError("observation_window_end must be later than observation_window_start")
        if self.observation_timestamp < self.observation_window_end:
            raise ValueError("observation_timestamp cannot precede observation_window_end")

        p50, p95 = self.p50_latency_ms, self.p95_latency_ms
        if self.latency_sample_count == 0 and (p50 is not None or p95 is not None):
            raise ValueError("latencies must be null when latency_sample_count is 0")
        if p50 is not None and p95 is not None and p95 < p50:
            raise ValueError("p95_latency_ms cannot be below p50_latency_ms")

        total = self.observed_throughput_rps
        payments, checks = self.completed_payment_rps, self.completed_check_rps
        if (
            total is not None
            and payments is not None
            and checks is not None
            and abs(payments + checks - total) > _RATE_TOLERANCE * max(1.0, total)
        ):
            raise ValueError(
                "observed_throughput_rps must equal completed_payment_rps + completed_check_rps"
            )

        count, denominator = self.timeout_count, self.timeout_denominator_count
        if (count is None) != (denominator is None):
            raise ValueError("timeout_count and timeout_denominator_count must be given together")
        if count is not None and denominator is not None:
            if count > denominator:
                raise ValueError("timeout_count cannot exceed timeout_denominator_count")
            if denominator == 0:
                if self.timeout_rate is not None:
                    raise ValueError("timeout_rate must be null when its denominator is 0")
            elif (
                self.timeout_rate is None
                or abs(self.timeout_rate - count / denominator) > _RATE_TOLERANCE
            ):
                raise ValueError(
                    "timeout_rate must equal timeout_count / timeout_denominator_count"
                )
        return self


class ValidatedRouteTelemetry(ContractModel):
    """Track B trust-boundary envelope; the only form in which telemetry reaches the twin."""

    schema_version: SchemaVersion
    experiment_id: Identifier
    route_id: Identifier
    telemetry: RouteTelemetry
    trust_status: TelemetryTrustStatus
    validator_id: Identifier
    validator_version: VersionLabel
    validated_at: UtcDatetime
    validation_flags: tuple[TelemetryValidationFlag, ...] = ()
    cross_check_status: CrossCheckStatus

    @model_validator(mode="after")
    def _semantics(self) -> "ValidatedRouteTelemetry":
        if self.experiment_id != self.telemetry.experiment_id:
            raise ValueError(f"{ErrorCode.EXPERIMENT_MISMATCH}: envelope and telemetry differ")
        if self.route_id != self.telemetry.route_id:
            raise ValueError(f"{ErrorCode.ROUTE_IDENTITY_MISMATCH}: envelope and telemetry differ")
        if duplicates := _duplicates(self.validation_flags):
            raise ValueError(f"validation_flags contains duplicates: {duplicates}")
        if self.validated_at < self.telemetry.observation_timestamp:
            raise ValueError("validated_at cannot precede the telemetry observation_timestamp")
        deviation = TelemetryValidationFlag.CROSS_CHECK_DEVIATION in self.validation_flags
        if deviation != (self.cross_check_status is CrossCheckStatus.INCONSISTENT):
            raise ValueError(
                "CROSS_CHECK_DEVIATION flag must be present exactly when cross_check_status is "
                "INCONSISTENT"
            )
        return self

    @property
    def is_flagged(self) -> bool:
        return bool(self.validation_flags)


class SourceSequenceMark(ContractModel):
    """Highest sequence accepted from one telemetry source (a watermark entry)."""

    source_id: Identifier
    sequence: NonNegativeInt


def _check_unique_sources(marks: tuple[SourceSequenceMark, ...], field_name: str) -> None:
    if duplicates := _duplicates(mark.source_id for mark in marks):
        raise ValueError(f"{field_name} lists a source more than once: {duplicates}")


class RouteTwinSnapshot(ContractModel):
    """Immutable view of a route twin. Measured, estimated and assumed values stay separate.

    Accepted decision 9: flagged-but-authentic telemetry is kept apart from the trusted
    observation that estimators may use. Estimates stay null until an estimator exists.
    """

    schema_version: SchemaVersion
    experiment_id: Identifier
    route_id: Identifier
    snapshot_version: NonNegativeInt
    model_version: VersionLabel
    last_updated_at: UtcDatetime
    last_observed_at: UtcDatetime | None
    latest_sequence_per_source: tuple[SourceSequenceMark, ...] = ()
    freshness_status: FreshnessStatus
    # Measured: copied from observations, never computed.
    latest_trusted_observation: RouteTelemetry | None = None
    latest_flagged_observation: ValidatedRouteTelemetry | None = None
    # Estimated: produced by a named estimator; all null before Stage 5.
    estimated_capacity_rps: NonNegativeFloat | None = None
    capacity_lower_bound_rps: NonNegativeFloat | None = None
    estimated_queue_depth: NonNegativeFloat | None = None
    estimated_timeout_probability: Probability | None = None
    telemetry_confidence: Probability | None = None
    estimator_id: Identifier | None = None
    # Assumed: experiment configuration, never presented as measured.
    assumed_configured_capacity_rps: PositiveFloat | None = None
    assumed_client_timeout_s: PositiveFloat | None = None

    @model_validator(mode="after")
    def _semantics(self) -> "RouteTwinSnapshot":
        _check_unique_sources(self.latest_sequence_per_source, "latest_sequence_per_source")
        trusted = self.latest_trusted_observation
        flagged = self.latest_flagged_observation
        observations = [obs for obs in (trusted, flagged.telemetry if flagged else None) if obs]

        for obs in observations:
            if obs.experiment_id != self.experiment_id:
                raise ValueError(f"{ErrorCode.EXPERIMENT_MISMATCH}: nested observation")
            if obs.route_id != self.route_id:
                raise ValueError(f"{ErrorCode.ROUTE_IDENTITY_MISMATCH}: nested observation")
        if flagged is not None:
            if flagged.trust_status is not TelemetryTrustStatus.AUTHENTICATED:
                raise ValueError("latest_flagged_observation must be AUTHENTICATED telemetry")
            if not flagged.is_flagged:
                raise ValueError("latest_flagged_observation must carry at least one flag")

        watermarks = {mark.source_id: mark.sequence for mark in self.latest_sequence_per_source}
        for obs in observations:
            if obs.source_id not in watermarks or obs.sequence > watermarks[obs.source_id]:
                raise ValueError("an observation is beyond latest_sequence_per_source")

        if self.freshness_status is FreshnessStatus.NO_DATA:
            if observations or self.last_observed_at is not None:
                raise ValueError("NO_DATA snapshots cannot contain observations")
        else:
            if not observations or self.last_observed_at is None:
                raise ValueError(f"{self.freshness_status} snapshots require an observation")
            latest = max(obs.observation_timestamp for obs in observations)
            if self.last_observed_at != latest:
                raise ValueError("last_observed_at must equal the latest observation_timestamp")

        estimates = (
            self.estimated_capacity_rps,
            self.capacity_lower_bound_rps,
            self.estimated_queue_depth,
            self.estimated_timeout_probability,
            self.telemetry_confidence,
        )
        has_estimate = any(value is not None for value in estimates)
        if has_estimate != (self.estimator_id is not None):
            raise ValueError("estimator_id is required exactly when an estimate is present")
        if (
            self.estimated_capacity_rps is not None
            and self.capacity_lower_bound_rps is not None
            and self.capacity_lower_bound_rps > self.estimated_capacity_rps
        ):
            raise ValueError("capacity_lower_bound_rps cannot exceed estimated_capacity_rps")
        return self


# ------------------------------------------------------------- transaction projection


class TransactionProjection(ContractModel):
    """Non-authoritative recovery view of one transaction. It never authorizes money."""

    schema_version: SchemaVersion
    experiment_id: Identifier
    transaction_id: Identifier
    route_id: Identifier
    remitter_bank_id: Identifier
    beneficiary_bank_id: Identifier
    amount_minor: PositiveInt
    currency: CurrencyCode = "INR"
    financial_outcome: FinancialOutcome
    ledger_state_observed: LedgerState
    workflow_state: RecoveryWorkflowState
    version: PositiveInt
    last_applied_event_id: Identifier
    initiated_at: UtcDatetime
    ambiguity_created_at: UtcDatetime | None = None
    last_authoritative_evidence_at: UtcDatetime | None = None
    last_evidence_reference: EvidenceReference | None = None
    last_check_at: UtcDatetime | None = None
    last_check_id: Identifier | None = None
    check_in_flight_id: Identifier | None = None
    checks_used: NonNegativeInt
    last_rejection_code: ErrorCode | None = None
    next_eligible_at: UtcDatetime | None = None
    deadline_at: UtcDatetime | None = None
    deadline_policy_id: Identifier | None = None
    deadline_escalated: StrictBool
    terminal: StrictBool
    quarantine_reason: QuarantineReason | None = None

    @model_validator(mode="after")
    def _semantics(self) -> "TransactionProjection":
        state = self.workflow_state
        outcome = self.financial_outcome
        if self.remitter_bank_id == self.beneficiary_bank_id:
            raise ValueError("remitter_bank_id and beneficiary_bank_id must differ")
        _check_outcome_matches_ledger(self.ledger_state_observed, outcome)
        _check_deadline_pair(self.deadline_at, self.deadline_policy_id)

        if outcome in EVIDENCED_OUTCOMES and self.last_evidence_reference is None:
            raise ValueError(
                f"{ErrorCode.EVIDENCE_REQUIRED}: outcome {outcome} requires last_evidence_reference"
            )
        if outcome in TERMINAL_OUTCOMES and self.last_authoritative_evidence_at is None:
            raise ValueError(f"outcome {outcome} requires last_authoritative_evidence_at")

        is_closed = state is RecoveryWorkflowState.CLOSED
        if self.terminal != is_closed:
            raise ValueError("terminal must be true exactly when workflow_state is CLOSED")
        if is_closed and outcome not in TERMINAL_OUTCOMES:
            raise ValueError("a CLOSED projection requires a SUCCESS or FAILURE outcome")
        if is_closed and self.next_eligible_at is not None:
            raise ValueError("a CLOSED projection cannot have next_eligible_at")
        if (state is RecoveryWorkflowState.QUARANTINED) != (self.quarantine_reason is not None):
            raise ValueError("quarantine_reason is required exactly when QUARANTINED")
        if (state is RecoveryWorkflowState.CHECK_IN_FLIGHT) != (
            self.check_in_flight_id is not None
        ):
            raise ValueError("check_in_flight_id is required exactly when CHECK_IN_FLIGHT")
        if state is RecoveryWorkflowState.ESCALATED and not self.deadline_escalated:
            raise ValueError("an ESCALATED projection must have deadline_escalated set")
        if state is RecoveryWorkflowState.ACTIVE and self.ambiguity_created_at is not None:
            raise ValueError("an ACTIVE projection has not become ambiguous")
        if state in _AMBIGUITY_WORKFLOW_STATES and self.ambiguity_created_at is None:
            raise ValueError(f"{state} requires ambiguity_created_at")
        if self.ambiguity_created_at is not None and self.ambiguity_created_at < self.initiated_at:
            raise ValueError("ambiguity_created_at cannot precede initiated_at")
        if (self.last_check_at is None) != (self.last_check_id is None):
            raise ValueError("last_check_at and last_check_id must be provided together")
        return self


# ------------------------------------------------------------------------- decisions


class TransactionVersionReference(ContractModel):
    """Projection version a decision relied on; the dispatcher re-checks it."""

    transaction_id: Identifier
    version: PositiveInt


class DecisionRequest(ContractModel):
    schema_version: SchemaVersion
    experiment_id: Identifier
    route_id: Identifier
    requested_at: UtcDatetime
    min_telemetry_watermark: tuple[SourceSequenceMark, ...] = ()

    @model_validator(mode="after")
    def _semantics(self) -> "DecisionRequest":
        _check_unique_sources(self.min_telemetry_watermark, "min_telemetry_watermark")
        return self


_FAIL_CLOSED_REASONS = frozenset(
    {
        DecisionReason.NO_CAPACITY_ESTIMATE,
        DecisionReason.TELEMETRY_STALE,
        DecisionReason.TELEMETRY_UNTRUSTED,
        DecisionReason.BOOTSTRAP_FAIL_CLOSED,
    }
)
_NORMAL_REASONS = frozenset(
    {
        DecisionReason.SAFE_BUDGET_SELECTED,
        DecisionReason.NO_SAFE_BUDGET,
        DecisionReason.NO_ELIGIBLE_TRANSACTIONS,
    }
)
_REASONS_FOR_MODE: dict[DecisionMode, frozenset[DecisionReason]] = {
    DecisionMode.NORMAL: _NORMAL_REASONS,
    DecisionMode.FAIL_CLOSED: _FAIL_CLOSED_REASONS,
    DecisionMode.WARMING_UP: frozenset({DecisionReason.CONTROLLER_WARMING_UP}),
}


class RecoveryDecision(ContractModel):
    """A recommendation that bounds dispatch. It is never permission to bypass checks."""

    schema_version: SchemaVersion
    experiment_id: Identifier
    route_id: Identifier
    decision_id: Identifier
    policy_version: VersionLabel
    model_version: VersionLabel
    timing_policy_id: Identifier
    created_at: UtcDatetime
    expires_at: UtcDatetime
    decision_mode: DecisionMode
    budget_rps: NonNegativeInt
    selected_transaction_ids: tuple[Identifier, ...] = ()
    state_versions: tuple[TransactionVersionReference, ...] = ()
    predicted_delay_ms: NonNegativeFloat | None = None
    predicted_new_ambiguous: NonNegativeFloat | None = None
    prediction_horizon_s: PositiveFloat | None = None
    reason_code: DecisionReason
    reason_detail: Text
    telemetry_watermark: tuple[SourceSequenceMark, ...] = ()
    latest_observation_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _semantics(self) -> "RecoveryDecision":
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.reason_code not in _REASONS_FOR_MODE[self.decision_mode]:
            raise ValueError(f"reason {self.reason_code} is not valid for {self.decision_mode}")
        if self.decision_mode is not DecisionMode.NORMAL and (
            self.budget_rps != 0 or self.selected_transaction_ids
        ):
            raise ValueError(
                f"{self.decision_mode} decisions must have zero budget and no selection"
            )

        selected = self.selected_transaction_ids
        if duplicates := _duplicates(selected):
            raise ValueError(f"selected_transaction_ids contains duplicates: {duplicates}")
        if tuple(ref.transaction_id for ref in self.state_versions) != selected:
            raise ValueError(
                "state_versions must list exactly the selected transactions, in the same order"
            )
        if self.reason_code is DecisionReason.NO_ELIGIBLE_TRANSACTIONS and selected:
            raise ValueError("NO_ELIGIBLE_TRANSACTIONS decisions cannot select transactions")
        validity_s = (self.expires_at - self.created_at).total_seconds()
        if len(selected) > self.budget_rps * validity_s:
            raise ValueError("selection exceeds budget_rps over the decision validity period")

        predictions = (self.predicted_delay_ms, self.predicted_new_ambiguous)
        if any(value is not None for value in predictions) and self.prediction_horizon_s is None:
            raise ValueError("predictions require prediction_horizon_s")
        _check_unique_sources(self.telemetry_watermark, "telemetry_watermark")
        return self


# ------------------------------------------------------------------- status checking


class StatusCheckRequest(ContractModel):
    schema_version: SchemaVersion
    experiment_id: Identifier
    check_id: Identifier
    transaction_id: Identifier
    route_id: Identifier
    remitter_bank_id: Identifier
    decision_id: Identifier | None = None
    lease_id: Identifier | None = None
    requested_at: UtcDatetime


class StatusCheckResult(ContractModel):
    """Returned whenever the switch actually answered (so the check was dispatched)."""

    schema_version: SchemaVersion
    experiment_id: Identifier
    check_id: Identifier
    transaction_id: Identifier
    route_id: Identifier
    check_result: CheckResultKind
    evidence_reference: EvidenceReference | None = None
    evidence_signature: EvidenceSignature | None = None
    evidence_verified: StrictBool
    counts_against_limit: StrictBool
    counted_check_attempts: PositiveInt
    latency_ms: NonNegativeFloat
    responded_at: UtcDatetime

    @model_validator(mode="after")
    def _semantics(self) -> "StatusCheckResult":
        has_evidence = self.evidence_reference is not None and self.evidence_signature is not None
        if self.check_result in _EVIDENCED_CHECK_RESULTS and not has_evidence:
            raise ValueError(
                f"{ErrorCode.EVIDENCE_REQUIRED}: {self.check_result} requires evidence_reference "
                "and evidence_signature"
            )
        if (self.evidence_reference is None) != (self.evidence_signature is None):
            raise ValueError("evidence_reference and evidence_signature must be given together")
        if (
            self.evidence_reference is not None
            and self.evidence_reference.evidence_kind is not EvidenceKind.STATUS_CHECK_RESPONSE
        ):
            raise ValueError("status-check evidence must be of kind STATUS_CHECK_RESPONSE")
        if self.evidence_verified and not has_evidence:
            raise ValueError("evidence_verified requires evidence")
        if not self.counts_against_limit:
            raise ValueError("an answered check was dispatched and counts against the limit")
        return self

    @property
    def is_authoritative(self) -> bool:
        """True only for an AUTHORITATIVE_* result whose evidence Track B has verified."""

        return self.evidence_verified and self.check_result in (
            CheckResultKind.AUTHORITATIVE_SUCCESS,
            CheckResultKind.AUTHORITATIVE_FAILURE,
        )


class ErrorResponse(ContractModel):
    schema_version: SchemaVersion
    error_code: ErrorCode
    message: Text
    retryable: StrictBool
    counts_against_limit: StrictBool | None
    retry_after_ms: NonNegativeInt | None = None
    correlation_id: Identifier | None = None
    experiment_id: Identifier | None = None

    @model_validator(mode="after")
    def _semantics(self) -> "ErrorResponse":
        rule = CHECK_COUNTING[self.error_code]
        counted = self.counts_against_limit
        required = {CheckCounting.COUNTED: True, CheckCounting.NOT_COUNTED: False}
        if rule in required and counted is not required[rule]:
            raise ValueError(f"{self.error_code} requires counts_against_limit={required[rule]}")
        if rule is CheckCounting.DECLARED_BY_DISPATCHER and counted is None:
            raise ValueError(f"{self.error_code} requires the dispatcher to declare counting")
        if rule is CheckCounting.NOT_APPLICABLE and counted is not None:
            raise ValueError(f"{self.error_code} does not concern check counting")
        if self.retry_after_ms is not None and not self.retryable:
            raise ValueError("retry_after_ms is only meaningful for retryable errors")
        return self


# ------------------------------------------------------------- experimental credits


class ExperimentalCheckLimits(ContractModel):
    """Tier 2 per-transaction limits proposed for research; not an adopted NPCI rule."""

    first_check_delay_s: NonNegativeFloat
    max_counted_checks: NonNegativeInt
    limit_class: Tier2LimitClass


class CreditLease(ContractModel):
    """Tier 2 check credits issued by the mock switch only (accepted decision 14)."""

    schema_version: SchemaVersion
    experiment_id: Identifier
    lease_id: Identifier
    regime: Literal["EXPERIMENTAL_MOCK_SWITCH"]
    route_id: Identifier
    bank_id: Identifier
    credits_granted: NonNegativeInt
    credits_consumed: NonNegativeInt
    issued_at: UtcDatetime
    expires_at: UtcDatetime
    coordinator_version: VersionLabel
    limits: ExperimentalCheckLimits | None = None

    @model_validator(mode="after")
    def _semantics(self) -> "CreditLease":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if self.credits_consumed > self.credits_granted:
            raise ValueError("credits_consumed cannot exceed credits_granted")
        return self


class CreditConsumption(ContractModel):
    schema_version: SchemaVersion
    experiment_id: Identifier
    lease_id: Identifier
    check_id: Identifier
    consumed_at: UtcDatetime
    accepted: StrictBool
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def _semantics(self) -> "CreditConsumption":
        if self.accepted and self.error_code is not None:
            raise ValueError("an accepted consumption cannot carry an error_code")
        if not self.accepted and self.error_code not in CREDIT_REJECTION_CODES:
            raise ValueError(
                f"a rejected consumption requires error_code in {sorted(CREDIT_REJECTION_CODES)}"
            )
        return self


# --------------------------------------------------------------------- timing policy


class DeadlineRule(ContractModel):
    """How a resolution deadline is derived. No calendar calculation is implemented."""

    kind: DeadlineRuleKind
    duration_s: PositiveFloat | None = None
    calendar_id: Identifier | None = None

    @model_validator(mode="after")
    def _semantics(self) -> "DeadlineRule":
        if self.kind is DeadlineRuleKind.FIXED_DURATION and (
            self.duration_s is None or self.calendar_id is not None
        ):
            raise ValueError("FIXED_DURATION requires duration_s and no calendar_id")
        if self.kind is DeadlineRuleKind.REGULATORY_CALENDAR and (
            self.calendar_id is None or self.duration_s is not None
        ):
            raise ValueError("REGULATORY_CALENDAR requires calendar_id and no duration_s")
        return self


class TimingPolicy(ContractModel):
    """Configuration of check timing and deadlines. Values are data, never hard-coded."""

    schema_version: SchemaVersion
    policy_id: Identifier
    regime: TimingRegime
    verification_status: VerificationStatus
    first_check_delay_s: NonNegativeFloat
    first_check_reference: FirstCheckReference
    min_check_spacing_s: NonNegativeFloat
    max_counted_checks: NonNegativeInt
    check_window_s: PositiveFloat
    stop_error_codes: tuple[Identifier, ...] = ()
    client_timeout_s: PositiveFloat
    check_timeout_s: PositiveFloat
    deadline_rule: DeadlineRule
    escalation_window_s: NonNegativeFloat
    escalation_extra_checks: NonNegativeInt

    @model_validator(mode="after")
    def _semantics(self) -> "TimingPolicy":
        verified = self.verification_status is VerificationStatus.VERIFIED
        if self.regime is TimingRegime.TIER1_COMPLIANT and not verified:
            raise ValueError(
                "TIER1_COMPLIANT requires verification_status VERIFIED; label assumed values "
                "RESEARCH_SCALED instead"
            )
        if self.regime is TimingRegime.TIER2_EXPERIMENTAL and verified:
            raise ValueError("TIER2_EXPERIMENTAL limits are proposals and cannot be VERIFIED")
        if self.escalation_extra_checks > 0 and self.regime is not TimingRegime.TIER2_EXPERIMENTAL:
            raise ValueError("escalation_extra_checks are only allowed in TIER2_EXPERIMENTAL")
        if self.deadline_rule.kind is DeadlineRuleKind.REGULATORY_CALENDAR and not verified:
            raise ValueError("a REGULATORY_CALENDAR deadline requires a VERIFIED policy")
        if duplicates := _duplicates(self.stop_error_codes):
            raise ValueError(f"stop_error_codes contains duplicates: {duplicates}")
        if (
            self.max_counted_checks > 1
            and (self.max_counted_checks - 1) * self.min_check_spacing_s > self.check_window_s
        ):
            raise ValueError("max_counted_checks cannot fit within check_window_s at this spacing")
        return self
