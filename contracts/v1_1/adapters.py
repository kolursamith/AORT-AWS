"""Pure adapters from Track B representations (feature/backend @ 8b863da) to v1.1 messages.

Inputs are the JSON-shaped mappings Track B already produces:

* outbox ``DomainEvent`` dumps (``backend/aorts/contracts/events.py``, written by
  ``banking/ledger/service.py``);
* telemetry observations and ``TelemetryAssessment.as_dict()``
  (``backend/aorts/contracts/requests.py``, ``security/telemetry_validator.py``);
* remitter ``check_status`` outcomes and ``StatusEvidence`` dumps
  (``banking/remitter/service.py``).

Each adapter also takes ``boundary``: the additional metadata v1.1 requires that Track B does
not emit yet. Keeping it separate shows exactly which data are additional; the README lists the
future ``/v1.1`` export that must produce them. Adapters never invent identifiers, timestamps,
windows, versions, evidence, verification verdicts or outcomes. A message that cannot be
represented faithfully comes back as ``Incompatible`` with the missing fields and reasons.
``UNKNOWN`` is never mapped to ``FAILURE``. No adapter imports Track B code or verifies a
signature; verification verdicts are Track B's trust-boundary output.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from contracts.v1_1.errors import ErrorCode
from contracts.v1_1.models import (
    SCHEMA_VERSION,
    CheckResultKind,
    DeadlineRule,
    DeadlineRuleKind,
    EvidenceKind,
    FinancialOutcome,
    FirstCheckReference,
    LedgerState,
    StatusCheckResult,
    TelemetryTrustStatus,
    TimingPolicy,
    TimingRegime,
    TransactionEvent,
    TransactionEventType,
    ValidatedRouteTelemetry,
    VerificationStatus,
)
from contracts.v1_1.protocol import error_response

T = TypeVar("T", bound=BaseModel)

TRACKB_REFERENCE_COMMIT = "8b863da"


@dataclass(frozen=True)
class Adapted(Generic[T]):
    """A faithful v1.1 message. ``dropped_fields`` had no v1.1 counterpart and were not sent."""

    value: T
    dropped_fields: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True)
class Incompatible:
    """The input cannot become a valid v1.1 message without inventing data."""

    error_code: ErrorCode
    missing_fields: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return False


def _missing(data: Mapping[str, Any] | None, keys: tuple[str, ...], prefix: str) -> list[str]:
    data = data or {}
    return [f"{prefix}{key}" for key in keys if data.get(key) is None]


def _build(model: type[T], fields: dict[str, Any], dropped: tuple[str, ...] = ()):
    try:
        return Adapted(model.model_validate(fields), dropped)
    except ValidationError as exc:
        reasons = tuple(
            f"{'.'.join(str(p) for p in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        )
        return Incompatible(_code_from_reasons(reasons), (), reasons)


def _code_from_reasons(reasons: tuple[str, ...]) -> ErrorCode:
    """Surface the contract's own semantic codes (they prefix the validator messages)."""

    for code in (
        ErrorCode.TIMEOUT_CANNOT_IMPLY_OUTCOME,
        ErrorCode.EVIDENCE_REQUIRED,
        ErrorCode.EXPERIMENT_MISMATCH,
        ErrorCode.ROUTE_IDENTITY_MISMATCH,
    ):
        if any(str(code) in reason for reason in reasons):
            return code
    return ErrorCode.SCHEMA_INVALID


# ------------------------------------------------------------------------- telemetry

# v1.1 fields Track B's telemetry path does not produce (requests.RouteTelemetry has a single
# ``timestamp`` and no window, scope, rate split or sample count; the validator output has a
# verdict and free-text reasons, not a trust status, structured flags or validator identity).
TELEMETRY_BOUNDARY_FIELDS: tuple[str, ...] = (
    "experiment_id",
    "observation_scope",
    "observation_window_start",
    "observation_window_end",
    "payment_rps",
    "check_rps",
    "latency_sample_count",
    "trust_status",
    "cross_check_status",
    "validator_id",
    "validator_version",
    "validated_at",
)
TELEMETRY_OPTIONAL_BOUNDARY_FIELDS: tuple[str, ...] = (
    "validation_flags",
    "completed_payment_rps",
    "completed_check_rps",
    "timeout_count",
    "timeout_denominator_count",
)
# Track B observation fields with no v1.1 counterpart.
TELEMETRY_DROPPED_FIELDS: tuple[str, ...] = ("health_score", "event_id", "correlation_id")


def adapt_validated_telemetry(
    observation: Mapping[str, Any],
    assessment: Mapping[str, Any],
    boundary: Mapping[str, Any] | None,
) -> "Adapted[ValidatedRouteTelemetry] | Incompatible":
    """Track B telemetry observation + validator assessment -> ``ValidatedRouteTelemetry``.

    The measurements are the source's reported values; ``observation_timestamp`` is Track B's
    own ``timestamp`` field. Windows, trust status and flags must come from ``boundary``.
    """

    for key in ("event_id", "route_id"):
        if observation.get(key) != assessment.get(key):
            return Incompatible(
                ErrorCode.CONFLICTING_SEQUENCE,
                (),
                (f"observation and assessment disagree on {key}",),
            )
    verdict = assessment.get("verdict")
    if verdict == "REJECTED":
        return Incompatible(
            ErrorCode.UNTRUSTED_SOURCE,
            (),
            (
                "Track B validator verdict REJECTED; rejected samples are not forwarded",
                *map(str, assessment.get("reasons") or ()),
            ),
        )
    if verdict not in ("TRUSTED", "SUSPICIOUS"):
        return Incompatible(ErrorCode.SCHEMA_INVALID, ("assessment.verdict",), ())

    missing = _missing(observation, ("source_id", "route_id", "timestamp"), "observation.")
    if observation.get("sequence") is None:
        missing.append("observation.sequence")
    missing += _missing(boundary, TELEMETRY_BOUNDARY_FIELDS, "boundary.")
    if missing:
        return Incompatible(
            ErrorCode.SCHEMA_INVALID,
            tuple(missing),
            ("Track B does not emit these v1.1 telemetry fields; nothing is inferred",),
        )
    assert boundary is not None  # for type checkers: _missing reported it otherwise

    flags = tuple(boundary.get("validation_flags") or ())
    trust = boundary["trust_status"]
    if verdict == "TRUSTED" and trust != TelemetryTrustStatus.AUTHENTICATED:
        return Incompatible(
            ErrorCode.UNTRUSTED_SOURCE,
            (),
            (f"verdict TRUSTED conflicts with trust_status {trust}",),
        )
    if verdict == "TRUSTED" and flags:
        return Incompatible(
            ErrorCode.SCHEMA_INVALID, (), ("verdict TRUSTED conflicts with validation flags",)
        )
    if verdict == "SUSPICIOUS" and not flags:
        return Incompatible(
            ErrorCode.SCHEMA_INVALID,
            ("boundary.validation_flags",),
            ("a SUSPICIOUS sample needs at least one structured flag; it is never sent as clean",),
        )

    telemetry = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": boundary["experiment_id"],
        "route_id": observation["route_id"],
        "source_id": observation["source_id"],
        "observation_scope": boundary["observation_scope"],
        "sequence": observation["sequence"],
        "observation_window_start": boundary["observation_window_start"],
        "observation_window_end": boundary["observation_window_end"],
        "observation_timestamp": observation["timestamp"],
        "payment_rps": boundary["payment_rps"],
        "check_rps": boundary["check_rps"],
        "observed_throughput_rps": observation.get("throughput_tps"),
        "p50_latency_ms": observation.get("latency_p50_ms"),
        "p95_latency_ms": observation.get("latency_p95_ms"),
        "latency_sample_count": boundary["latency_sample_count"],
        "timeout_rate": observation.get("timeout_rate"),
        "queue_depth": observation.get("backlog"),
    }
    for key in TELEMETRY_OPTIONAL_BOUNDARY_FIELDS[1:]:
        if boundary.get(key) is not None:
            telemetry[key] = boundary[key]
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": boundary["experiment_id"],
        "route_id": observation["route_id"],
        "telemetry": telemetry,
        "trust_status": trust,
        "validator_id": boundary["validator_id"],
        "validator_version": boundary["validator_version"],
        "validated_at": boundary["validated_at"],
        "validation_flags": flags,
        "cross_check_status": boundary["cross_check_status"],
    }
    dropped = tuple(key for key in TELEMETRY_DROPPED_FIELDS if observation.get(key) is not None)
    return _build(ValidatedRouteTelemetry, envelope, dropped)


# ----------------------------------------------------------------- transaction events


class MappingStatus(StrEnum):
    DIRECT = "DIRECT"  # Track B's transition determines the v1.1 event type
    EVIDENCE_DETERMINED = "EVIDENCE_DETERMINED"  # the evidence kind decides the type
    NEEDS_DISAMBIGUATION = "NEEDS_DISAMBIGUATION"  # Track B overloads its event type
    NOT_REPRESENTABLE = "NOT_REPRESENTABLE"  # no faithful v1.1 event exists


@dataclass(frozen=True)
class OutboxRule:
    trackb_event_type: str
    previous_states: frozenset[str | None]
    new_state: str
    status: MappingStatus
    v11_event_type: TransactionEventType | None
    note: str


_UNRESOLVED = frozenset({"AMBIGUOUS", "CHECK_IN_FLIGHT", "DEBITED"})

OUTBOX_RULES: tuple[OutboxRule, ...] = (
    OutboxRule(
        "PAYMENT_DEBITED",
        frozenset({None}),
        "DEBITED",
        MappingStatus.DIRECT,
        TransactionEventType.PAYMENT_INITIATED,
        "payment accepted and debited (ledger/service.py debit path)",
    ),
    OutboxRule(
        "PAYMENT_AMBIGUOUS",
        frozenset({"DEBITED"}),
        "AMBIGUOUS",
        MappingStatus.NEEDS_DISAMBIGUATION,
        None,
        "emitted for a client timeout (mark_ambiguous) and for an unusable or PENDING original "
        "response (_preserve_uncertainty); boundary.ambiguity_cause must say which",
    ),
    OutboxRule(
        "CHECK_STARTED",
        frozenset({"AMBIGUOUS"}),
        "CHECK_IN_FLIGHT",
        MappingStatus.DIRECT,
        TransactionEventType.STATUS_CHECK_DISPATCHED,
        "start_check; Track B has no check identifier yet (boundary.check_id)",
    ),
    OutboxRule(
        "CHECK_RESULT",
        frozenset({"CHECK_IN_FLIGHT"}),
        "AMBIGUOUS",
        MappingStatus.NEEDS_DISAMBIGUATION,
        None,
        "overloaded: inconclusive answer, transport failure or switch refusal "
        "(apply_evidence/check_not_admitted); boundary.check_fact must state which",
    ),
    OutboxRule(
        "CHECK_RESULT",
        frozenset({"CONFIRMED_FAILED"}),
        "REVERSAL_INITIATED",
        MappingStatus.NOT_REPRESENTABLE,
        None,
        "intermediate state committed atomically with REVERSED; v1.1 decision 6 has no "
        "REVERSAL_INITIATED. The next REVERSAL_COMPLETED carries the fact (version gap of 1)",
    ),
    OutboxRule(
        "CHECK_RESULT",
        frozenset({"QUARANTINED"}),
        "AMBIGUOUS",
        MappingStatus.NOT_REPRESENTABLE,
        None,
        "manual release of a backend quarantine; v1.1 has no backend quarantine state",
    ),
    OutboxRule(
        "PAYMENT_SUCCEEDED",
        _UNRESOLVED,
        "CONFIRMED_SUCCESS",
        MappingStatus.EVIDENCE_DETERMINED,
        None,
        "ORIGINAL_PAYMENT_RESPONSE evidence -> PAYMENT_RESPONSE_RECEIVED; "
        "STATUS_CHECK_RESPONSE evidence -> STATUS_CHECK_ANSWERED",
    ),
    OutboxRule(
        "PAYMENT_FAILED",
        _UNRESOLVED,
        "CONFIRMED_FAILED",
        MappingStatus.EVIDENCE_DETERMINED,
        None,
        "as PAYMENT_SUCCEEDED, with FAILURE",
    ),
    OutboxRule(
        "REVERSAL_COMPLETED",
        frozenset({"REVERSAL_INITIATED"}),
        "REVERSED",
        MappingStatus.DIRECT,
        TransactionEventType.REVERSAL_COMPLETED,
        "committed reversal posting; payload.operation_id identifies it",
    ),
    OutboxRule(
        "PAYMENT_CLOSED",
        frozenset({"CONFIRMED_SUCCESS"}),
        "CLOSED",
        MappingStatus.DIRECT,
        TransactionEventType.TRANSACTION_CLOSED,
        "settlement posted and closed",
    ),
    OutboxRule(
        "PAYMENT_CLOSED",
        frozenset({"HANDOFF_UDIR"}),
        "RESOLVED_BY_RECONCILIATION",
        MappingStatus.NOT_REPRESENTABLE,
        None,
        "Track B combines reconciliation result, settlement or reversal and closure in one "
        "terminal state; v1.1 RECONCILIATION_RESOLVED needs a CONFIRMED_*/REVERSED ledger "
        "state followed by TRANSACTION_CLOSED",
    ),
    OutboxRule(
        "ESCALATED",
        frozenset({"AMBIGUOUS", "CHECK_IN_FLIGHT", "QUARANTINED"}),
        "HANDOFF_UDIR",
        MappingStatus.DIRECT,
        TransactionEventType.RECONCILIATION_HANDOFF,
        "check allowance exhausted, deadline, or manual escalation",
    ),
    OutboxRule(
        "QUARANTINED",
        frozenset(_UNRESOLVED | {"HANDOFF_UDIR"}),
        "QUARANTINED",
        MappingStatus.NOT_REPRESENTABLE,
        None,
        "backend quarantine is a ledger state in Track B; in v1.1 quarantine is a Track A "
        "projection state only",
    ),
)

# Track B financial outcome -> v1.1. REVERSED is a ledger fact, so its outcome is FAILURE.
TRACKB_OUTCOME: dict[str, FinancialOutcome] = {
    "UNKNOWN": FinancialOutcome.UNKNOWN,
    "SUCCESS": FinancialOutcome.SUCCESS,
    "FAILURE": FinancialOutcome.FAILURE,
    "REVERSED": FinancialOutcome.FAILURE,
}

# Fields every v1.1 event needs that Track B's DomainEvent does not carry.
EVENT_BOUNDARY_FIELDS: tuple[str, ...] = ("experiment_id", "source_id", "source_sequence")
# Track B DomainEvent fields with no v1.1 counterpart (kept in Track B's audit trail).
EVENT_DROPPED_FIELDS: tuple[str, ...] = (
    "payload.previous_state",
    "payload.reason",
    "payload.ambiguous_since",
    "payload.created_at",
    "payload.evidence_source",
    "payload.why",
    "payload.operation_id",
)

_CHECK_EVENTS = frozenset(
    {
        TransactionEventType.STATUS_CHECK_DISPATCHED,
        TransactionEventType.STATUS_CHECK_ANSWERED,
        TransactionEventType.STATUS_CHECK_REJECTED,
        TransactionEventType.STATUS_CHECK_TIMED_OUT,
    }
)


def find_outbox_rule(event: Mapping[str, Any]) -> OutboxRule | None:
    payload = event.get("payload") or {}
    for rule in OUTBOX_RULES:
        if (
            rule.trackb_event_type == event.get("event_type")
            and payload.get("previous_state") in rule.previous_states
            and payload.get("last_known_state") == rule.new_state
        ):
            return rule
    return None


def _event_kind(
    rule: OutboxRule, event: Mapping[str, Any], boundary: Mapping[str, Any]
) -> tuple[TransactionEventType, dict[str, Any]] | Incompatible:
    """Resolve the v1.1 type and any type-specific fields, or explain why it cannot be done."""

    if rule.status is MappingStatus.NOT_REPRESENTABLE:
        return Incompatible(ErrorCode.NOT_IMPLEMENTED, (), (rule.note,))
    if rule.status is MappingStatus.DIRECT:
        assert rule.v11_event_type is not None
        return rule.v11_event_type, {}

    if rule.status is MappingStatus.EVIDENCE_DETERMINED:
        evidence = boundary.get("evidence") or {}
        kind = evidence.get("evidence_kind")
        if kind == EvidenceKind.ORIGINAL_PAYMENT_RESPONSE:
            return TransactionEventType.PAYMENT_RESPONSE_RECEIVED, {}
        if kind == EvidenceKind.STATUS_CHECK_RESPONSE:
            success = event.get("event_type") == "PAYMENT_SUCCEEDED"
            result = (
                CheckResultKind.AUTHORITATIVE_SUCCESS
                if success
                else CheckResultKind.AUTHORITATIVE_FAILURE
            )
            return TransactionEventType.STATUS_CHECK_ANSWERED, {"check_result": result}
        return Incompatible(
            ErrorCode.EVIDENCE_REQUIRED,
            ("boundary.evidence.evidence_kind",),
            ("a confirmed outcome needs its evidence reference; Track B stores only a source",),
        )

    # NEEDS_DISAMBIGUATION
    if event.get("event_type") == "PAYMENT_AMBIGUOUS":
        cause = boundary.get("ambiguity_cause")
        if cause == "PAYMENT_TIMEOUT":
            return TransactionEventType.PAYMENT_TIMED_OUT, {}
        if cause == "PENDING_RESPONSE":
            return TransactionEventType.PAYMENT_RESPONSE_RECEIVED, {"outcome": "PENDING"}
        return Incompatible(
            ErrorCode.SCHEMA_INVALID,
            ("boundary.ambiguity_cause",),
            (rule.note,),
        )
    fact = boundary.get("check_fact") or {}
    kind = fact.get("kind")
    if kind == "ANSWERED" and fact.get("check_result") in (
        CheckResultKind.PENDING,
        CheckResultKind.UNKNOWN,
    ):
        extra = {"check_result": CheckResultKind(fact["check_result"])}
        if extra["check_result"] is CheckResultKind.PENDING:
            extra["outcome"] = "PENDING"
        return TransactionEventType.STATUS_CHECK_ANSWERED, extra
    if kind == "TIMED_OUT" and fact.get("result_code"):
        return TransactionEventType.STATUS_CHECK_TIMED_OUT, {"result_code": fact["result_code"]}
    if kind == "REJECTED" and fact.get("result_code"):
        return TransactionEventType.STATUS_CHECK_REJECTED, {"result_code": fact["result_code"]}
    return Incompatible(ErrorCode.SCHEMA_INVALID, ("boundary.check_fact",), (rule.note,))


_LEDGER_FOR_KIND: dict[TransactionEventType, LedgerState] = {
    TransactionEventType.PAYMENT_INITIATED: LedgerState.DEBITED,
    TransactionEventType.PAYMENT_TIMED_OUT: LedgerState.DEBITED,
    TransactionEventType.STATUS_CHECK_DISPATCHED: LedgerState.DEBITED,
    TransactionEventType.STATUS_CHECK_REJECTED: LedgerState.DEBITED,
    TransactionEventType.STATUS_CHECK_TIMED_OUT: LedgerState.DEBITED,
    TransactionEventType.REVERSAL_COMPLETED: LedgerState.REVERSED,
    TransactionEventType.RECONCILIATION_HANDOFF: LedgerState.RECONCILIATION_PENDING,
    TransactionEventType.TRANSACTION_CLOSED: LedgerState.CLOSED,
}
_TRACKB_LEDGER: dict[str, LedgerState] = {
    "DEBITED": LedgerState.DEBITED,
    "AMBIGUOUS": LedgerState.DEBITED,
    "CHECK_IN_FLIGHT": LedgerState.DEBITED,
    "CONFIRMED_SUCCESS": LedgerState.CONFIRMED_SUCCESS,
    "CONFIRMED_FAILED": LedgerState.CONFIRMED_FAILED,
    "REVERSED": LedgerState.REVERSED,
    "CLOSED": LedgerState.CLOSED,
    "HANDOFF_UDIR": LedgerState.RECONCILIATION_PENDING,
}


def adapt_outbox_event(
    event: Mapping[str, Any], boundary: Mapping[str, Any] | None
) -> "Adapted[TransactionEvent] | Incompatible":
    """Track B outbox ``DomainEvent`` dump -> v1.1 ``TransactionEvent`` (or why not)."""

    boundary = boundary or {}
    payload = event.get("payload") or {}
    rule = find_outbox_rule(event)
    if rule is None:
        return Incompatible(
            ErrorCode.NOT_IMPLEMENTED,
            (),
            (
                f"no mapping for Track B {event.get('event_type')} "
                f"{payload.get('previous_state')} -> {payload.get('last_known_state')}",
            ),
        )
    resolved = _event_kind(rule, event, boundary)
    if isinstance(resolved, Incompatible):
        return resolved
    kind, extra = resolved

    # The debit payload carries no outcome: a just-debited payment is UNKNOWN by definition.
    trackb_outcome = payload.get("financial_outcome")
    if trackb_outcome is None and kind is TransactionEventType.PAYMENT_INITIATED:
        trackb_outcome = "UNKNOWN"
    if trackb_outcome not in TRACKB_OUTCOME:
        return Incompatible(
            ErrorCode.SCHEMA_INVALID, ("payload.financial_outcome",), ("unknown Track B outcome",)
        )
    outcome = TRACKB_OUTCOME[trackb_outcome]
    timeouts = (TransactionEventType.PAYMENT_TIMED_OUT, TransactionEventType.STATUS_CHECK_TIMED_OUT)
    if kind in timeouts and outcome is not FinancialOutcome.UNKNOWN:
        return Incompatible(
            ErrorCode.TIMEOUT_CANNOT_IMPLY_OUTCOME,
            (),
            (
                f"Track B reports {trackb_outcome} on a timeout; "
                "a timeout leaves the outcome UNKNOWN",
            ),
        )
    if extra.get("outcome") == "PENDING":
        if outcome is not FinancialOutcome.UNKNOWN:
            return Incompatible(
                ErrorCode.CONFLICTING_EVENT,
                (),
                (f"PENDING evidence conflicts with Track B outcome {trackb_outcome}",),
            )
        outcome = FinancialOutcome.PENDING  # an evidenced refinement of UNKNOWN, never FAILURE

    missing = _missing(
        event,
        ("event_id", "transaction_id", "route_id", "bank_id", "amount_paise", "timestamp"),
        "event.",
    )
    if event.get("transaction_version") is None:
        missing.append("event.transaction_version")
    if payload.get("checks_used") is None:
        missing.append("event.payload.checks_used")
    missing += _missing(boundary, EVENT_BOUNDARY_FIELDS, "boundary.")
    beneficiary = payload.get("beneficiary_bank_id") or boundary.get("beneficiary_bank_id")
    if beneficiary is None:
        missing.append("boundary.beneficiary_bank_id")
    elif (
        payload.get("beneficiary_bank_id")
        and boundary.get("beneficiary_bank_id")
        and payload["beneficiary_bank_id"] != boundary["beneficiary_bank_id"]
    ):
        return Incompatible(
            ErrorCode.CONFLICTING_EVENT, (), ("beneficiary_bank_id differs from the payload",)
        )
    correlation = event.get("correlation_id") or boundary.get("correlation_id")
    if correlation is None:
        missing.append("event.correlation_id")
    if kind in _CHECK_EVENTS and boundary.get("check_id") is None:
        missing.append("boundary.check_id")
    deadline_at = payload.get("deadline_at")
    if deadline_at is not None and boundary.get("deadline_policy_id") is None:
        missing.append("boundary.deadline_policy_id")
    if kind is TransactionEventType.REVERSAL_COMPLETED and not payload.get("operation_id"):
        missing.append("event.payload.operation_id")
    needs_evidence = outcome in (
        FinancialOutcome.SUCCESS,
        FinancialOutcome.FAILURE,
        FinancialOutcome.PENDING,
    )
    evidence = boundary.get("evidence")
    if needs_evidence and evidence is None:
        missing.append("boundary.evidence")
    if missing:
        return Incompatible(
            ErrorCode.EVIDENCE_REQUIRED
            if missing == ["boundary.evidence"]
            else ErrorCode.SCHEMA_INVALID,
            tuple(missing),
            ("Track B does not emit these v1.1 event fields; nothing is inferred",),
        )

    source_issuer = payload.get("evidence_source")
    if evidence is not None and source_issuer and evidence.get("issuer_id") != source_issuer:
        return Incompatible(
            ErrorCode.EVIDENCE_CONFLICT,
            (),
            (f"evidence issuer {evidence.get('issuer_id')!r} is not {source_issuer!r}",),
        )

    ledger = _LEDGER_FOR_KIND.get(kind) or _TRACKB_LEDGER.get(payload["last_known_state"])
    if ledger is None:
        return Incompatible(
            ErrorCode.NOT_IMPLEMENTED, (), (f"no ledger state for {payload['last_known_state']}",)
        )
    fields: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": boundary["experiment_id"],
        "event_id": event["event_id"],
        "transaction_id": event["transaction_id"],
        "route_id": event["route_id"],
        "remitter_bank_id": event["bank_id"],
        "beneficiary_bank_id": beneficiary,
        "event_type": kind,
        "ledger_state": ledger,
        "financial_outcome": outcome,
        "state_version": event["transaction_version"],
        "amount_minor": event["amount_paise"],
        "currency": "INR",
        "event_timestamp": event["timestamp"],
        "source_id": boundary["source_id"],
        "source_sequence": boundary["source_sequence"],
        "correlation_id": correlation,
        "counted_check_attempts": payload["checks_used"],
    }
    if kind in _CHECK_EVENTS:
        fields["check_id"] = boundary["check_id"]
    if "check_result" in extra:
        fields["check_result"] = extra["check_result"]
    if "result_code" in extra:
        fields["result_code"] = extra["result_code"]
    if evidence is not None:
        fields["evidence_reference"] = evidence
    if deadline_at is not None:
        fields["deadline_at"] = deadline_at
        fields["deadline_policy_id"] = boundary["deadline_policy_id"]
    dropped = tuple(
        name
        for name in EVENT_DROPPED_FIELDS
        if payload.get(name.removeprefix("payload.")) is not None
    )
    return _build(TransactionEvent, fields, dropped)


# --------------------------------------------------------------- status-check outcomes

# Track B error codes on a refused (not admitted, HTTP 429/403) check -> v1.1. Never counted.
TRACKB_REJECTION_CODES: dict[str, ErrorCode] = {
    "RATE_LIMITED": ErrorCode.RATE_LIMITED,
    "ROUTE_RATE_LIMITED": ErrorCode.RATE_LIMITED,
    "GLOBAL_CAPACITY_EXCEEDED": ErrorCode.RATE_LIMITED,
    "SWITCH_OVERLOADED": ErrorCode.RATE_LIMITED,
    "CREDIT_EXHAUSTED": ErrorCode.CREDIT_EXHAUSTED,
    "AUTH_MISSING": ErrorCode.AUTHENTICATION_FAILED,
    "AUTH_INVALID_SIGNATURE": ErrorCode.AUTHENTICATION_FAILED,
    "AUTH_UNKNOWN_KEY": ErrorCode.AUTHENTICATION_FAILED,
    "AUTH_EXPIRED_TIMESTAMP": ErrorCode.AUTHENTICATION_FAILED,
    "AUTH_REPLAYED_NONCE": ErrorCode.AUTHENTICATION_FAILED,
    "AUTH_FORBIDDEN": ErrorCode.AUTHORIZATION_DENIED,
    "IDENTITY_MISMATCH": ErrorCode.AUTHORIZATION_DENIED,
}
# httpx exception class names Track B reports in ``outcome["error"]`` that mean a timeout.
TIMEOUT_ERROR_NAMES = frozenset(
    {"TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}
)
# Switch status -> check result. NOT_FOUND is UNKNOWN: absence of a record is not a failure.
TRACKB_CHECK_RESULT: dict[str, CheckResultKind] = {
    "SUCCESS": CheckResultKind.AUTHORITATIVE_SUCCESS,
    "FAILED": CheckResultKind.AUTHORITATIVE_FAILURE,
    "PENDING": CheckResultKind.PENDING,
    "NOT_FOUND": CheckResultKind.UNKNOWN,
}
STATUS_BOUNDARY_FIELDS: tuple[str, ...] = (
    "experiment_id",
    "check_id",
    "route_id",
    "counted_check_attempts",
)


def adapt_status_check(
    outcome: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
    boundary: Mapping[str, Any] | None,
):
    """Track B ``check_status`` outcome -> ``StatusCheckResult`` or ``ErrorResponse``.

    * refused before dispatch -> ``ErrorResponse`` that does not count;
    * transport failure -> ``CHECK_TIMEOUT``/``UPSTREAM_UNAVAILABLE`` that counts (Track B counts
      every transport failure, ``remitter/service.py`` ``count_check=True``);
    * answer whose signature failed -> ``EVIDENCE_SIGNATURE_INVALID`` (counts);
    * authenticated answer -> ``StatusCheckResult``. An authoritative result needs a complete
      verification record from Track B's trust boundary; a bare boolean is not enough.
    """

    boundary = boundary or {}
    experiment_id = boundary.get("experiment_id")
    correlation = boundary.get("correlation_id")
    if outcome.get("admitted") is False:
        reason = outcome.get("rejection_reason")
        code = TRACKB_REJECTION_CODES.get(str(reason))
        if code is None:
            return Incompatible(
                ErrorCode.SCHEMA_INVALID,
                (),
                (f"unmapped Track B rejection {reason!r}; refusing to guess whether it counted",),
            )
        return Adapted(
            error_response(
                code,
                f"Track B refused the check before dispatch: {reason}",
                experiment_id=experiment_id,
                correlation_id=correlation,
            )
        )
    if outcome.get("admitted") is not True:
        return Incompatible(ErrorCode.SCHEMA_INVALID, ("outcome.admitted",), ())

    if outcome.get("error"):
        timeout = outcome["error"] in TIMEOUT_ERROR_NAMES
        code = ErrorCode.CHECK_TIMEOUT if timeout else ErrorCode.UPSTREAM_UNAVAILABLE
        return Adapted(
            error_response(
                code,
                f"no answer from the switch ({outcome['error']}); outcome unchanged",
                counts_against_limit=None if timeout else True,
                experiment_id=experiment_id,
                correlation_id=correlation,
            )
        )

    if evidence is None:
        return Incompatible(
            ErrorCode.EVIDENCE_REQUIRED,
            ("evidence",),
            ("an admitted check without an error must carry the switch evidence",),
        )
    if not evidence.get("authenticated"):
        return Adapted(
            error_response(
                ErrorCode.EVIDENCE_SIGNATURE_INVALID,
                "switch response failed signature verification; treated as no answer",
                experiment_id=experiment_id,
                correlation_id=correlation,
            )
        )
    if outcome.get("status") is not None and outcome["status"] != evidence.get("status"):
        return Incompatible(
            ErrorCode.EVIDENCE_CONFLICT, (), ("outcome.status differs from evidence.status",)
        )
    result = TRACKB_CHECK_RESULT.get(str(evidence.get("status")))
    if result is None:
        return Incompatible(ErrorCode.SCHEMA_INVALID, ("evidence.status",), ())

    missing = _missing(boundary, STATUS_BOUNDARY_FIELDS, "boundary.")
    missing += _missing(evidence, ("transaction_id", "observed_at"), "evidence.")
    if outcome.get("latency_ms") is None:
        missing.append("outcome.latency_ms")
    evidenced = result is not CheckResultKind.UNKNOWN
    reference = boundary.get("evidence_reference")
    signature = boundary.get("evidence_signature")
    if evidenced and reference is None:
        missing.append("boundary.evidence_reference")
    if evidenced and signature is None:
        missing.append("boundary.evidence_signature")
    verification = boundary.get("verification") or {}
    verified = (
        verification.get("status") == "VERIFIED"
        and bool(verification.get("verifier_id"))
        and verification.get("verified_at") is not None
        and signature is not None
        and verification.get("key_id") == signature.get("key_id")
    )
    authoritative = result in (
        CheckResultKind.AUTHORITATIVE_SUCCESS,
        CheckResultKind.AUTHORITATIVE_FAILURE,
    )
    if authoritative and not verified:
        missing.append("boundary.verification")
    if missing:
        return Incompatible(
            ErrorCode.EVIDENCE_REQUIRED
            if any("evidence" in m or "verification" in m for m in missing)
            else ErrorCode.SCHEMA_INVALID,
            tuple(missing),
            ("authoritative and pending answers need Track B's evidence and verification record",),
        )

    fields: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": boundary["experiment_id"],
        "check_id": boundary["check_id"],
        "transaction_id": evidence["transaction_id"],
        "route_id": boundary["route_id"],
        "check_result": result,
        "evidence_verified": verified,
        "counts_against_limit": True,
        "counted_check_attempts": boundary["counted_check_attempts"],
        "latency_ms": outcome["latency_ms"],
        "responded_at": evidence["observed_at"],
    }
    if reference is not None:
        fields["evidence_reference"] = reference
    if signature is not None:
        fields["evidence_signature"] = signature
    dropped = tuple(k for k in ("switch_reference", "reason") if evidence.get(k) is not None)
    return _build(StatusCheckResult, fields, dropped)


# ----------------------------------------------------------------------- timing policy


def express_trackb_timing_policy(
    settings: Mapping[str, Any], *, policy_id: str
) -> "Adapted[TimingPolicy] | Incompatible":
    """Express Track B's dispatcher timing settings (``app/config.py``) as a ``TimingPolicy``.

    Track B waits ``min(max, base * 2**(n-1))`` seconds after the n-th counted check
    (``ledger/service.py`` ``_backoff``). A v1.1 policy has one fixed spacing, so the schedule is
    representable only when it never grows (``base == max``). Track B has no check window; the
    deadline doubles as one. The result is labelled RESEARCH_SCALED, never compliant.
    """

    needed = (
        "max_checks_per_transaction",
        "check_backoff_base_seconds",
        "check_backoff_max_seconds",
        "deadline_seconds",
        "remitter_switch_timeout_s",
        "status_check_timeout_s",
    )
    missing = _missing(settings, needed, "settings.")
    if missing:
        return Incompatible(ErrorCode.SCHEMA_INVALID, tuple(missing), ())
    base = settings["check_backoff_base_seconds"]
    if base != settings["check_backoff_max_seconds"]:
        return Incompatible(
            ErrorCode.NOT_IMPLEMENTED,
            (),
            (
                f"exponential backoff {base}..{settings['check_backoff_max_seconds']} s has no "
                "fixed min_check_spacing_s; set base == max or extend TimingPolicy jointly",
            ),
        )
    deadline = float(settings["deadline_seconds"])
    fields = {
        "schema_version": SCHEMA_VERSION,
        "policy_id": policy_id,
        "regime": TimingRegime.RESEARCH_SCALED,
        "verification_status": VerificationStatus.RESEARCH_PARAMETER,
        "first_check_delay_s": float(base),
        "first_check_reference": FirstCheckReference.TIMED_OUT,
        "min_check_spacing_s": float(base),
        "max_counted_checks": settings["max_checks_per_transaction"],
        "check_window_s": deadline,
        "client_timeout_s": float(settings["remitter_switch_timeout_s"]),
        "check_timeout_s": float(settings["status_check_timeout_s"]),
        "deadline_rule": DeadlineRule(kind=DeadlineRuleKind.FIXED_DURATION, duration_s=deadline),
        "escalation_window_s": 0.0,
        "escalation_extra_checks": 0,
    }
    return _build(TimingPolicy, fields)
