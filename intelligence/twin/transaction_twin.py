"""Transaction Digital Twin (Stage 2): a non-authoritative projection of committed events.

The twin follows backend ``TransactionEvent`` facts and derives, for each synthetic
transaction, its recovery workflow and status-check eligibility. It never debits, credits,
reverses, refunds, closes ledger records or sends checks: the only outputs are frozen
``TransactionProjection`` snapshots and read-only eligibility results.

Semantics
---------
* Creation: ``PAYMENT_INITIATED`` creates a projection; there is no separate registration.
  Any other event for an unknown transaction is rejected as ``UNKNOWN_TRANSACTION`` and must
  be redelivered after the initiation (the in-memory twin does not buffer).
* Three separate dimensions: ``financial_outcome`` and ``ledger_state_observed`` are copied
  from backend events only; ``workflow_state`` is derived by the twin. A workflow change is
  never a financial mutation.
* Idempotency: an ``event_id`` seen before with identical content is a duplicate; with
  different content it is a conflict. Within a transaction, a ``state_version`` already
  applied with the same content (ignoring ``event_id`` and ``source_sequence``) is a
  duplicate; with different content it is a conflict that quarantines the projection. A
  lower, never-applied version is stale. A higher version applies; any gap is reported.
* Missing history: a transition that needs an event the twin has not seen (for example a
  check dispatch before the payment timeout) is rejected as ``INVALID_TRANSITION`` and leaves
  state untouched, so redelivery can succeed later. Contradicting authoritative outcomes, or
  ledger regressions between consecutive versions, quarantine the projection.
* Derived fields: ``ESCALATED``, ``deadline_escalated`` and ``next_eligible_at`` are computed
  from the stored facts, the ``TimingPolicy`` and the injected clock each time a projection is
  read. They never change the backend-owned ``version``.
* Atomicity: every candidate state is validated as a full contract projection before it is
  published; a failure leaves the previous projection unchanged.
* Concurrency: one lock serialises every public method; no I/O happens while it is held.
* Durability: deduplication memory is in-process only. It does not provide exactly-once
  guarantees across process restarts; a persistent inbox is later integration work.
"""

import hashlib
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from contracts.v1_1 import (
    SCHEMA_VERSION,
    ErrorCode,
    EvidenceReference,
    FinancialOutcome,
    FirstCheckReference,
    LedgerState,
    QuarantineReason,
    RecoveryWorkflowState,
    RouteDescriptor,
    TimingPolicy,
    TimingRegime,
    TransactionEvent,
    TransactionEventType,
    TransactionProjection,
    VerificationStatus,
)
from intelligence.common.clock import Clock

Workflow = RecoveryWorkflowState
EventType = TransactionEventType
Outcome = FinancialOutcome
Ledger = LedgerState

_TERMINAL_OUTCOMES = frozenset({Outcome.SUCCESS, Outcome.FAILURE})
# Ledger moves the backend may report between two consecutive versions (README table).
_LEDGER_SUCCESSORS: dict[LedgerState, frozenset[LedgerState]] = {
    Ledger.DEBITED: frozenset(
        {
            Ledger.DEBITED,
            Ledger.CONFIRMED_SUCCESS,
            Ledger.CONFIRMED_FAILED,
            Ledger.RECONCILIATION_PENDING,
        }
    ),
    Ledger.RECONCILIATION_PENDING: frozenset(
        {Ledger.RECONCILIATION_PENDING, Ledger.CONFIRMED_SUCCESS, Ledger.CONFIRMED_FAILED}
    ),
    Ledger.CONFIRMED_SUCCESS: frozenset({Ledger.CONFIRMED_SUCCESS, Ledger.CLOSED}),
    Ledger.CONFIRMED_FAILED: frozenset({Ledger.CONFIRMED_FAILED, Ledger.REVERSED}),
    Ledger.REVERSED: frozenset({Ledger.REVERSED, Ledger.CLOSED}),
    Ledger.CLOSED: frozenset({Ledger.CLOSED}),
}
# Stored (non-derived) workflow states from which each event type may be applied.
_ALLOWED_FROM: dict[TransactionEventType, frozenset[RecoveryWorkflowState]] = {
    EventType.PAYMENT_TIMED_OUT: frozenset({Workflow.ACTIVE}),
    EventType.PAYMENT_RESPONSE_RECEIVED: frozenset(
        {Workflow.ACTIVE, Workflow.AMBIGUOUS, Workflow.CHECK_IN_FLIGHT}
    ),
    EventType.STATUS_CHECK_DISPATCHED: frozenset({Workflow.AMBIGUOUS}),
    EventType.STATUS_CHECK_ANSWERED: frozenset({Workflow.AMBIGUOUS, Workflow.CHECK_IN_FLIGHT}),
    EventType.STATUS_CHECK_REJECTED: frozenset({Workflow.AMBIGUOUS, Workflow.CHECK_IN_FLIGHT}),
    EventType.STATUS_CHECK_TIMED_OUT: frozenset({Workflow.AMBIGUOUS, Workflow.CHECK_IN_FLIGHT}),
    EventType.REVERSAL_COMPLETED: frozenset({Workflow.CLOSED}),
    EventType.RECONCILIATION_HANDOFF: frozenset({Workflow.AMBIGUOUS}),
    EventType.RECONCILIATION_RESOLVED: frozenset(
        {Workflow.HANDOFF_RECONCILIATION, Workflow.QUARANTINED}
    ),
    EventType.EVIDENCE_CONFLICT_RECORDED: frozenset(
        {
            Workflow.ACTIVE,
            Workflow.AMBIGUOUS,
            Workflow.CHECK_IN_FLIGHT,
            Workflow.HANDOFF_RECONCILIATION,
            Workflow.CLOSED,
        }
    ),
    EventType.TRANSACTION_CLOSED: frozenset({Workflow.CLOSED}),
}
_CHECK_RESPONSE_EVENTS = frozenset(
    {
        EventType.STATUS_CHECK_ANSWERED,
        EventType.STATUS_CHECK_REJECTED,
        EventType.STATUS_CHECK_TIMED_OUT,
    }
)
_DERIVED_ESCALATION_STATES = frozenset(
    {Workflow.AMBIGUOUS, Workflow.CHECK_IN_FLIGHT, Workflow.HANDOFF_RECONCILIATION}
)


class TransactionTwinError(Exception):
    """Base class for Transaction Twin errors."""


class UnknownTransactionError(TransactionTwinError, LookupError):
    """The requested (experiment_id, transaction_id) has no projection."""


class TransactionTwinInvariantError(TransactionTwinError, RuntimeError):
    """The injected clock or internal state violated an invariant."""


class TransactionTwinConfig(BaseModel):
    """Twin configuration. Timing values live in the injected ``TimingPolicy``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # When true, PAYMENT_INITIATED must name a route registered via register_route().
    require_registered_routes: StrictBool = False


class ApplyStatus(StrEnum):
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    STALE_VERSION = "STALE_VERSION"
    CONFLICT = "CONFLICT"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    UNKNOWN_TRANSACTION = "UNKNOWN_TRANSACTION"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    QUARANTINED = "QUARANTINED"
    REJECTED_INVALID = "REJECTED_INVALID"


@dataclass(frozen=True, slots=True)
class TransactionApplyResult:
    status: ApplyStatus
    error_code: ErrorCode | None
    detail: str
    experiment_id: str
    transaction_id: str
    event_id: str
    event_version: int
    projection_version: int | None
    version_gap: int = 0
    quarantined: bool = False

    @property
    def applied(self) -> bool:
        return self.status is ApplyStatus.APPLIED


class EligibilityStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    NOT_YET_ELIGIBLE = "NOT_YET_ELIGIBLE"
    NOT_AMBIGUOUS = "NOT_AMBIGUOUS"
    CHECK_IN_FLIGHT = "CHECK_IN_FLIGHT"
    CHECK_LIMIT_REACHED = "CHECK_LIMIT_REACHED"
    CHECK_WINDOW_CLOSED = "CHECK_WINDOW_CLOSED"
    TERMINAL = "TERMINAL"
    QUARANTINED = "QUARANTINED"
    IN_RECONCILIATION = "IN_RECONCILIATION"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True, slots=True)
class CheckEligibility:
    """Whether a Tier 1 status check may be requested. It authorises nothing financial."""

    status: EligibilityStatus
    eligible_at: datetime | None
    reason: str
    policy_id: str
    policy_regime: TimingRegime
    policy_verification: VerificationStatus
    deadline_at: datetime | None
    deadline_verification: VerificationStatus | None
    deadline_escalated: bool

    @property
    def eligible(self) -> bool:
        return self.status is EligibilityStatus.ELIGIBLE


@dataclass(frozen=True, slots=True)
class EligibleTransaction:
    transaction_id: str
    eligible_at: datetime
    version: int
    checks_used: int
    deadline_at: datetime | None
    deadline_escalated: bool


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """Evidence kept when the twin refuses to choose between contradicting facts."""

    event_id: str
    event_version: int
    reason: QuarantineReason
    detail: str
    claimed_outcome: FinancialOutcome
    claimed_ledger_state: LedgerState
    evidence_reference: EvidenceReference | None
    held_outcome: FinancialOutcome
    held_evidence_reference: EvidenceReference | None


# ------------------------------------------------------------------ stored state


@dataclass(frozen=True)
class _TxnState:
    """Facts copied from backend events plus the twin's stored (non-derived) workflow."""

    experiment_id: str
    transaction_id: str
    route_id: str
    remitter_bank_id: str
    beneficiary_bank_id: str
    amount_minor: int
    currency: str
    financial_outcome: FinancialOutcome
    ledger_state: LedgerState
    workflow: RecoveryWorkflowState
    version: int
    last_applied_event_id: str
    initiated_at: datetime
    checks_used: int
    ambiguity_created_at: datetime | None = None
    last_authoritative_evidence_at: datetime | None = None
    last_evidence_reference: EvidenceReference | None = None
    last_check_at: datetime | None = None
    last_check_id: str | None = None
    check_in_flight_id: str | None = None
    last_rejection_code: ErrorCode | None = None
    deadline_at: datetime | None = None
    deadline_policy_id: str | None = None
    quarantine_reason: QuarantineReason | None = None


@dataclass
class _TxnRecord:
    state: _TxnState
    version_fingerprints: dict[int, str] = field(default_factory=dict)
    conflicts: list[ConflictRecord] = field(default_factory=list)


@dataclass(frozen=True)
class _Reject:
    status: ApplyStatus
    code: ErrorCode | None
    detail: str
    quarantine: QuarantineReason | None = None


def _event_fingerprint(event: TransactionEvent) -> str:
    return hashlib.sha256(event.model_dump_json().encode("utf-8")).hexdigest()


def _fact_fingerprint(event: TransactionEvent) -> str:
    """Content of the committed fact, ignoring delivery identity (event id, outbox position)."""

    payload = event.model_dump_json(exclude={"event_id", "source_sequence"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TransactionTwinInvariantError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


# ------------------------------------------------------------------ pure transition logic


def _initial_state(event: TransactionEvent) -> _TxnState:
    return _TxnState(
        experiment_id=event.experiment_id,
        transaction_id=event.transaction_id,
        route_id=event.route_id,
        remitter_bank_id=event.remitter_bank_id,
        beneficiary_bank_id=event.beneficiary_bank_id,
        amount_minor=event.amount_minor,
        currency=event.currency,
        financial_outcome=event.financial_outcome,
        ledger_state=event.ledger_state,
        workflow=Workflow.ACTIVE,
        version=event.state_version,
        last_applied_event_id=event.event_id,
        initiated_at=event.event_timestamp,
        checks_used=event.counted_check_attempts,
        deadline_at=event.deadline_at,
        deadline_policy_id=event.deadline_policy_id,
    )


def _identity_mismatch(state: _TxnState, event: TransactionEvent) -> str | None:
    for name in ("route_id", "remitter_bank_id", "beneficiary_bank_id", "amount_minor", "currency"):
        if getattr(state, name) != getattr(event, name):
            return name
    return None


def _check_consistency(state: _TxnState, event: TransactionEvent, gap: int) -> _Reject | None:
    """Reject facts that contradict what the backend already reported."""

    held, claimed = state.financial_outcome, event.financial_outcome
    if held in _TERMINAL_OUTCOMES and claimed in _TERMINAL_OUTCOMES and held != claimed:
        return _Reject(
            ApplyStatus.QUARANTINED,
            ErrorCode.EVIDENCE_CONFLICT,
            f"event claims {claimed} after verified {held}",
            QuarantineReason.EVIDENCE_CONFLICT,
        )
    if held in _TERMINAL_OUTCOMES and claimed not in _TERMINAL_OUTCOMES:
        return _Reject(
            ApplyStatus.QUARANTINED,
            None,
            f"outcome regressed from {held} to {claimed}",
            QuarantineReason.INVARIANT_VIOLATION,
        )
    if event.counted_check_attempts < state.checks_used:
        return _Reject(
            ApplyStatus.QUARANTINED,
            None,
            "counted_check_attempts decreased",
            QuarantineReason.INVARIANT_VIOLATION,
        )
    if event.ledger_state not in _LEDGER_SUCCESSORS[state.ledger_state]:
        detail = f"ledger {state.ledger_state} -> {event.ledger_state} is not a direct step"
        if gap:
            return _Reject(ApplyStatus.INVALID_TRANSITION, None, f"{detail}; events missing")
        return _Reject(ApplyStatus.QUARANTINED, None, detail, QuarantineReason.INVARIANT_VIOLATION)
    return None


def _next_workflow(state: _TxnState, event: TransactionEvent) -> RecoveryWorkflowState:
    kind = event.event_type
    if kind is EventType.EVIDENCE_CONFLICT_RECORDED:
        return Workflow.QUARANTINED
    if event.financial_outcome in _TERMINAL_OUTCOMES:
        return Workflow.CLOSED
    if kind is EventType.PAYMENT_TIMED_OUT:
        return Workflow.AMBIGUOUS
    if kind is EventType.STATUS_CHECK_DISPATCHED:
        return Workflow.CHECK_IN_FLIGHT
    if kind in _CHECK_RESPONSE_EVENTS:
        return Workflow.AMBIGUOUS
    if kind is EventType.RECONCILIATION_HANDOFF:
        return Workflow.HANDOFF_RECONCILIATION
    return state.workflow  # e.g. a PENDING original response


def transition(state: _TxnState, event: TransactionEvent, gap: int) -> _TxnState | _Reject:
    """Pure function: the next stored state, or why the event cannot be applied."""

    kind = event.event_type
    if kind is EventType.PAYMENT_INITIATED:
        return _Reject(ApplyStatus.INVALID_TRANSITION, None, "transaction already initiated")
    # Contradictions are checked first so they quarantine whatever event type carries them.
    # A reconciliation resolution is the backend's authoritative exit and is not checked.
    if kind is not EventType.RECONCILIATION_RESOLVED:
        rejection = _check_consistency(state, event, gap)
        if rejection is not None:
            return rejection
    allowed = _ALLOWED_FROM[kind]
    if state.workflow not in allowed:
        hint = f"; {gap} version(s) missing" if gap else ""
        return _Reject(
            ApplyStatus.INVALID_TRANSITION,
            None,
            f"{kind} cannot follow workflow {state.workflow}{hint}",
        )
    # REVERSAL_COMPLETED needs a prior confirmed failure: it is only allowed from CLOSED, a
    # held SUCCESS is an evidence conflict above, and the ledger table only lets REVERSED
    # follow CONFIRMED_FAILED.
    in_flight = state.check_in_flight_id
    if kind in _CHECK_RESPONSE_EVENTS and in_flight is not None and event.check_id != in_flight:
        return _Reject(
            ApplyStatus.INVALID_TRANSITION,
            None,
            f"check {event.check_id} does not match the check in flight ({in_flight})",
        )

    workflow = _next_workflow(state, event)
    updates: dict[str, object] = {
        "financial_outcome": event.financial_outcome,
        "ledger_state": event.ledger_state,
        "workflow": workflow,
        "version": event.state_version,
        "last_applied_event_id": event.event_id,
        "checks_used": event.counted_check_attempts,
    }
    if event.deadline_at is not None:
        updates["deadline_at"] = event.deadline_at
        updates["deadline_policy_id"] = event.deadline_policy_id
    if event.evidence_reference is not None and (
        event.evidence_reference != state.last_evidence_reference
    ):
        updates["last_evidence_reference"] = event.evidence_reference
        updates["last_authoritative_evidence_at"] = event.event_timestamp
    if kind is EventType.PAYMENT_TIMED_OUT:
        updates["ambiguity_created_at"] = event.event_timestamp
    if kind is EventType.STATUS_CHECK_DISPATCHED:
        updates["check_in_flight_id"] = event.check_id
        updates["last_check_id"] = event.check_id
        updates["last_check_at"] = event.event_timestamp
        updates["last_rejection_code"] = None
    if kind in _CHECK_RESPONSE_EVENTS:
        updates["check_in_flight_id"] = None
        if event.result_code is not None:
            updates["last_rejection_code"] = event.result_code
        if kind is not EventType.STATUS_CHECK_REJECTED and event.check_id != state.last_check_id:
            # The dispatch was not seen; the answer time is a conservative check time.
            updates["last_check_id"] = event.check_id
            updates["last_check_at"] = event.event_timestamp
    if workflow in (Workflow.CLOSED, Workflow.QUARANTINED, Workflow.HANDOFF_RECONCILIATION):
        updates["check_in_flight_id"] = None
    if workflow is Workflow.QUARANTINED:
        updates["quarantine_reason"] = QuarantineReason.EVIDENCE_CONFLICT
    elif state.workflow is Workflow.QUARANTINED:
        updates["quarantine_reason"] = None
    return replace(state, **updates)


def quarantine(state: _TxnState, reason: QuarantineReason) -> _TxnState:
    """Stop recovery without adopting any disputed fact."""

    return replace(
        state, workflow=Workflow.QUARANTINED, quarantine_reason=reason, check_in_flight_id=None
    )


# ------------------------------------------------------------------ eligibility and deadlines


def deadline_escalated(deadline_at: datetime | None, policy: TimingPolicy, now: datetime) -> bool:
    """Priority signal only: True once ``now`` is within the policy's escalation window."""

    if deadline_at is None:
        return False
    return now >= deadline_at - timedelta(seconds=policy.escalation_window_s)


def deadline_verification(
    deadline_policy_id: str | None, policy: TimingPolicy
) -> VerificationStatus | None:
    """How trustworthy a deadline is: known only when it was set under the injected policy."""

    if deadline_policy_id is None or deadline_policy_id != policy.policy_id:
        return None
    return policy.verification_status


def evaluate_eligibility(
    projection: TransactionProjection, policy: TimingPolicy, now: datetime
) -> CheckEligibility:
    """Pure Tier 1 eligibility. Escalation never adds checks; nothing here moves money.

    Contract gaps handled fail-closed: events carry only a cumulative counted-attempt total,
    so the attempt limit is applied to that total (it can only under-allow checks) and the
    check window is anchored at the configured first-check reference point. Policies with
    ``stop_error_codes`` are INDETERMINATE because events carry no switch response codes.
    """

    now = _require_aware(now, "now")

    def result(status, reason, eligible_at=None):
        return CheckEligibility(
            status=status,
            eligible_at=eligible_at,
            reason=reason,
            policy_id=policy.policy_id,
            policy_regime=policy.regime,
            policy_verification=policy.verification_status,
            deadline_at=projection.deadline_at,
            deadline_verification=deadline_verification(projection.deadline_policy_id, policy),
            deadline_escalated=projection.deadline_escalated,
        )

    state = projection.workflow_state
    if projection.terminal or state is Workflow.CLOSED:
        return result(EligibilityStatus.TERMINAL, "transaction is resolved")
    if state is Workflow.QUARANTINED:
        return result(EligibilityStatus.QUARANTINED, f"quarantined: {projection.quarantine_reason}")
    if state is Workflow.HANDOFF_RECONCILIATION:
        return result(EligibilityStatus.IN_RECONCILIATION, "handed to reconciliation")
    if state is Workflow.ACTIVE:
        return result(EligibilityStatus.NOT_AMBIGUOUS, "payment has not timed out")
    if state is Workflow.CHECK_IN_FLIGHT:
        return result(EligibilityStatus.CHECK_IN_FLIGHT, "a check is already in flight")
    if projection.last_rejection_code is ErrorCode.CHECK_LIMIT_REACHED:
        return result(EligibilityStatus.CHECK_LIMIT_REACHED, "backend reported the limit")
    if projection.checks_used >= policy.max_counted_checks:
        return result(
            EligibilityStatus.CHECK_LIMIT_REACHED,
            f"{projection.checks_used} of {policy.max_counted_checks} counted checks used",
        )
    if policy.stop_error_codes:
        return result(
            EligibilityStatus.INDETERMINATE,
            "policy stop codes cannot be evaluated: events carry no switch response codes",
        )
    if policy.first_check_reference is FirstCheckReference.INITIATED:
        anchor = projection.initiated_at
    else:
        anchor = projection.ambiguity_created_at
    if anchor is None:
        return result(EligibilityStatus.INDETERMINATE, "first-check reference time is unknown")
    if projection.checks_used > 0 and projection.last_check_at is None:
        return result(
            EligibilityStatus.INDETERMINATE, "counted checks exist but no check time is known"
        )

    earliest = anchor + timedelta(seconds=policy.first_check_delay_s)
    if projection.last_check_at is not None:
        spaced = projection.last_check_at + timedelta(seconds=policy.min_check_spacing_s)
        earliest = max(earliest, spaced)
    window_end = anchor + timedelta(seconds=policy.check_window_s)
    if earliest > window_end or now > window_end:
        return result(EligibilityStatus.CHECK_WINDOW_CLOSED, "check window has closed")
    if now < earliest:
        return result(EligibilityStatus.NOT_YET_ELIGIBLE, "first-check delay or spacing", earliest)
    return result(EligibilityStatus.ELIGIBLE, "eligible for a Tier 1 status check", earliest)


def _build_projection(state: _TxnState, policy: TimingPolicy, now: datetime):
    """Validate the stored state as a contract projection, adding clock-derived fields."""

    escalated = state.workflow in _DERIVED_ESCALATION_STATES and deadline_escalated(
        state.deadline_at, policy, now
    )
    workflow = state.workflow
    if workflow is Workflow.AMBIGUOUS and escalated:
        workflow = Workflow.ESCALATED
    fields = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": state.experiment_id,
        "transaction_id": state.transaction_id,
        "route_id": state.route_id,
        "remitter_bank_id": state.remitter_bank_id,
        "beneficiary_bank_id": state.beneficiary_bank_id,
        "amount_minor": state.amount_minor,
        "currency": state.currency,
        "financial_outcome": state.financial_outcome,
        "ledger_state_observed": state.ledger_state,
        "workflow_state": workflow,
        "version": state.version,
        "last_applied_event_id": state.last_applied_event_id,
        "initiated_at": state.initiated_at,
        "ambiguity_created_at": state.ambiguity_created_at,
        "last_authoritative_evidence_at": state.last_authoritative_evidence_at,
        "last_evidence_reference": state.last_evidence_reference,
        "last_check_at": state.last_check_at,
        "last_check_id": state.last_check_id,
        "check_in_flight_id": state.check_in_flight_id,
        "checks_used": state.checks_used,
        "last_rejection_code": state.last_rejection_code,
        "deadline_at": state.deadline_at,
        "deadline_policy_id": state.deadline_policy_id,
        "deadline_escalated": escalated,
        "terminal": state.workflow is Workflow.CLOSED,
        "quarantine_reason": state.quarantine_reason,
    }
    base = TransactionProjection.model_validate(fields)
    eligibility = evaluate_eligibility(base, policy, now)
    if eligibility.eligible_at is None:
        return base
    return TransactionProjection.model_validate(
        {**fields, "next_eligible_at": eligibility.eligible_at}
    )


class _ProjectionInvalid(Exception):
    pass


def _validated(state: _TxnState, policy: TimingPolicy, now: datetime) -> TransactionProjection:
    try:
        return _build_projection(state, policy, now)
    except ValidationError as error:
        raise _ProjectionInvalid(str(error)) from error


# ------------------------------------------------------------------ registry


class TransactionTwinRegistry:
    """In-memory transaction projections keyed by (experiment_id, transaction_id)."""

    def __init__(
        self,
        policy: TimingPolicy,
        clock: Clock,
        config: TransactionTwinConfig | None = None,
        routes: Iterable[RouteDescriptor] = (),
    ) -> None:
        if not isinstance(policy, TimingPolicy):
            raise TypeError("policy must be a TimingPolicy")
        if config is None:
            config = TransactionTwinConfig()
        if not isinstance(config, TransactionTwinConfig):
            raise TypeError("config must be a TransactionTwinConfig")
        self._policy = policy
        self._clock = clock
        self._config = config
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], _TxnRecord] = {}
        self._event_ids: dict[tuple[str, str], tuple[str, str]] = {}
        self._routes: dict[tuple[str, str], RouteDescriptor] = {}
        self._last_now: datetime | None = None
        for descriptor in routes:
            self.register_route(descriptor)

    @property
    def policy(self) -> TimingPolicy:
        return self._policy

    def register_route(self, descriptor: RouteDescriptor) -> bool:
        """Record route endpoints used to validate PAYMENT_INITIATED identities."""

        if not isinstance(descriptor, RouteDescriptor):
            raise TypeError("descriptor must be a RouteDescriptor")
        key = (descriptor.experiment_id, descriptor.route_id)
        with self._lock:
            existing = self._routes.get(key)
            if existing is not None:
                if existing == descriptor:
                    return False
                raise ValueError(f"route {key} is already registered differently")
            self._routes[key] = descriptor
            return True

    # -------------------------------------------------------------- event application

    def apply_event(self, event: TransactionEvent) -> TransactionApplyResult:
        if not isinstance(event, TransactionEvent):
            raise TypeError("apply_event expects a TransactionEvent")
        with self._lock:
            now = self._now()
            return self._apply(event, now)

    def replay(self, events: Iterable[TransactionEvent]) -> tuple[TransactionApplyResult, ...]:
        """Apply events in the given order; deterministic for a given clock schedule."""

        return tuple(self.apply_event(event) for event in events)

    def _apply(self, event: TransactionEvent, now: datetime) -> TransactionApplyResult:
        key = (event.experiment_id, event.transaction_id)
        record = self._records.get(key)

        def result(status, code, detail, gap=0):
            current = self._records.get(key)
            return TransactionApplyResult(
                status=status,
                error_code=code,
                detail=detail,
                experiment_id=event.experiment_id,
                transaction_id=event.transaction_id,
                event_id=event.event_id,
                event_version=event.state_version,
                projection_version=current.state.version if current else None,
                version_gap=gap,
                quarantined=bool(current and current.state.workflow is Workflow.QUARANTINED),
            )

        # A model built without validation (model_construct) must not alter state.
        try:
            TransactionEvent.model_validate(event.model_dump(warnings=False))
        except ValidationError as error:
            return result(ApplyStatus.REJECTED_INVALID, ErrorCode.SCHEMA_INVALID, str(error))

        fingerprint = _event_fingerprint(event)
        seen = self._event_ids.get((event.experiment_id, event.event_id))
        if seen is not None:
            if seen == (event.transaction_id, fingerprint):
                return result(ApplyStatus.DUPLICATE, ErrorCode.DUPLICATE_EVENT, "event redelivered")
            if record is not None and seen[0] == event.transaction_id:
                self._quarantine(record, event, QuarantineReason.CONFLICTING_EVENT, now, "event")
            return result(
                ApplyStatus.CONFLICT,
                ErrorCode.CONFLICTING_EVENT,
                "event_id reused with different content",
            )

        if record is None:
            return self._create(event, now, fingerprint, result)

        state = record.state
        field_name = _identity_mismatch(state, event)
        if field_name is not None:
            self._quarantine(record, event, QuarantineReason.IDENTITY_MISMATCH, now, field_name)
            code = (
                ErrorCode.ROUTE_IDENTITY_MISMATCH
                if field_name in ("route_id", "remitter_bank_id", "beneficiary_bank_id")
                else ErrorCode.CONFLICTING_EVENT
            )
            return result(ApplyStatus.IDENTITY_MISMATCH, code, f"{field_name} differs")

        version = event.state_version
        fact = _fact_fingerprint(event)
        known = record.version_fingerprints.get(version)
        if known == fact:
            self._event_ids[(event.experiment_id, event.event_id)] = (
                event.transaction_id,
                fingerprint,
            )
            return result(ApplyStatus.DUPLICATE, ErrorCode.DUPLICATE_EVENT, "fact already applied")
        if known is not None:
            self._quarantine(record, event, QuarantineReason.CONFLICTING_EVENT, now, "version")
            return result(
                ApplyStatus.CONFLICT,
                ErrorCode.CONFLICTING_EVENT,
                f"version {version} already applied with different content",
            )
        if version < state.version:
            return result(
                ApplyStatus.STALE_VERSION,
                ErrorCode.STALE_EVENT_VERSION,
                f"version {version} is older than {state.version}",
            )

        gap = version - state.version - 1
        if (
            state.workflow is Workflow.QUARANTINED
            and event.event_type is not EventType.RECONCILIATION_RESOLVED
        ):
            return result(
                ApplyStatus.QUARANTINED, None, "projection is quarantined; awaiting reconciliation"
            )
        outcome = transition(state, event, gap)
        if isinstance(outcome, _Reject):
            if outcome.quarantine is not None:
                self._quarantine(record, event, outcome.quarantine, now, outcome.detail)
            return result(outcome.status, outcome.code, outcome.detail, gap)
        try:
            _validated(outcome, self._policy, now)
        except _ProjectionInvalid as error:
            return result(ApplyStatus.INVALID_TRANSITION, None, f"invalid projection: {error}")

        record.state = outcome
        record.version_fingerprints[version] = fact
        self._event_ids[(event.experiment_id, event.event_id)] = (event.transaction_id, fingerprint)
        detail = f"applied; {gap} version(s) missing" if gap else "applied"
        return result(ApplyStatus.APPLIED, None, detail, gap)

    def _create(self, event, now, fingerprint, result) -> TransactionApplyResult:
        if event.event_type is not EventType.PAYMENT_INITIATED:
            return result(
                ApplyStatus.UNKNOWN_TRANSACTION,
                ErrorCode.UNKNOWN_TRANSACTION,
                "no PAYMENT_INITIATED has been applied for this transaction",
            )
        route = self._routes.get((event.experiment_id, event.route_id))
        if route is None and self._config.require_registered_routes:
            return result(
                ApplyStatus.IDENTITY_MISMATCH,
                ErrorCode.ROUTE_IDENTITY_MISMATCH,
                "route is not registered",
            )
        if route is not None and (
            route.remitter_bank_id != event.remitter_bank_id
            or route.beneficiary_bank_id != event.beneficiary_bank_id
        ):
            return result(
                ApplyStatus.IDENTITY_MISMATCH,
                ErrorCode.ROUTE_IDENTITY_MISMATCH,
                "banks do not match the registered route",
            )
        state = _initial_state(event)
        try:
            _validated(state, self._policy, now)
        except _ProjectionInvalid as error:
            return result(ApplyStatus.INVALID_TRANSITION, None, f"invalid projection: {error}")
        key = (event.experiment_id, event.transaction_id)
        self._records[key] = _TxnRecord(
            state=state, version_fingerprints={event.state_version: _fact_fingerprint(event)}
        )
        self._event_ids[(event.experiment_id, event.event_id)] = (event.transaction_id, fingerprint)
        return result(ApplyStatus.APPLIED, None, "projection created")

    def _quarantine(
        self,
        record: _TxnRecord,
        event: TransactionEvent,
        reason: QuarantineReason,
        now: datetime,
        detail: str,
    ) -> None:
        state = record.state
        candidate = quarantine(state, reason)
        try:
            _validated(candidate, self._policy, now)
        except _ProjectionInvalid as error:
            raise TransactionTwinInvariantError(f"cannot quarantine: {error}") from error
        record.conflicts.append(
            ConflictRecord(
                event_id=event.event_id,
                event_version=event.state_version,
                reason=reason,
                detail=detail,
                claimed_outcome=event.financial_outcome,
                claimed_ledger_state=event.ledger_state,
                evidence_reference=event.evidence_reference,
                held_outcome=state.financial_outcome,
                held_evidence_reference=state.last_evidence_reference,
            )
        )
        record.state = candidate

    # -------------------------------------------------------------- reads

    def snapshot(self, experiment_id: str, transaction_id: str) -> TransactionProjection:
        with self._lock:
            now = self._now()
            return _validated(self._record(experiment_id, transaction_id).state, self._policy, now)

    def eligibility(
        self, experiment_id: str, transaction_id: str, at_time: datetime | None = None
    ) -> CheckEligibility:
        with self._lock:
            now = self._query_time(at_time)
            projection = _validated(
                self._record(experiment_id, transaction_id).state, self._policy, now
            )
            return evaluate_eligibility(projection, self._policy, now)

    def eligible_transactions(
        self, experiment_id: str, route_id: str, at_time: datetime | None = None
    ) -> tuple[EligibleTransaction, ...]:
        """Eligible transactions on one route, ordered by eligibility time then id. No ranking."""

        with self._lock:
            now = self._query_time(at_time)
            selected: list[EligibleTransaction] = []
            for (exp, _), record in self._records.items():
                state = record.state
                if exp != experiment_id or state.route_id != route_id:
                    continue
                projection = _validated(state, self._policy, now)
                verdict = evaluate_eligibility(projection, self._policy, now)
                if verdict.eligible and verdict.eligible_at is not None:
                    selected.append(
                        EligibleTransaction(
                            transaction_id=state.transaction_id,
                            eligible_at=verdict.eligible_at,
                            version=state.version,
                            checks_used=state.checks_used,
                            deadline_at=state.deadline_at,
                            deadline_escalated=projection.deadline_escalated,
                        )
                    )
            return tuple(sorted(selected, key=lambda e: (e.eligible_at, e.transaction_id)))

    def conflicts(self, experiment_id: str, transaction_id: str) -> tuple[ConflictRecord, ...]:
        with self._lock:
            return tuple(self._record(experiment_id, transaction_id).conflicts)

    def transaction_ids(self, experiment_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(txn for exp, txn in self._records if exp == experiment_id))

    # -------------------------------------------------------------- internals

    def _record(self, experiment_id: str, transaction_id: str) -> _TxnRecord:
        record = self._records.get((experiment_id, transaction_id))
        if record is None:
            raise UnknownTransactionError(
                f"transaction ({experiment_id}, {transaction_id}) has no projection"
            )
        return record

    def _now(self) -> datetime:
        now = _require_aware(self._clock.now(), "clock time")
        if self._last_now is not None and now < self._last_now:
            raise TransactionTwinInvariantError("clock moved backwards")
        self._last_now = now
        return now

    def _query_time(self, at_time: datetime | None) -> datetime:
        if at_time is None:
            return self._now()
        return _require_aware(at_time, "at_time")
