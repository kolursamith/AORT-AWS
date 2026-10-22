"""AORTS wire protocol v1.1: endpoints, codecs and pure decision-gating rules.

This module defines how the v1.1 models travel between Track B (banking backend) and Track A
(intelligence controller). It is transport-neutral Python: nothing here opens a socket, reads a
clock, touches a database or verifies a signature. Callers pass ``now`` explicitly.

The proposed HTTP surface lives under ``/v1.1``. None of these endpoints exists in Track B yet;
the existing ``/v1`` surface (``contracts/v1``) is unchanged. See ``contracts/v1_1/README.md``
("Wire protocol v1.1" and "Track B integration handoff").
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from math import floor
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, StrictBool, StringConstraints, ValidationError, model_validator

from contracts.v1_1.errors import CHECK_COUNTING, HTTP_STATUS, CheckCounting, ErrorCode
from contracts.v1_1.models import (
    SCHEMA_VERSION,
    ContractModel,
    DecisionMode,
    DecisionReason,
    DecisionRequest,
    ErrorResponse,
    Identifier,
    RecoveryDecision,
    SchemaVersion,
    SourceSequenceMark,
    StatusCheckResult,
    Text,
    TimingPolicy,
    TransactionEvent,
    UtcDatetime,
    ValidatedRouteTelemetry,
    VersionLabel,
)

PROTOCOL_VERSION = "1.1"
PATH_PREFIX = "/v1.1"
MEDIA_TYPE = "application/json"

Model = TypeVar("Model", bound=BaseModel)
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


# ------------------------------------------------------------------------ endpoints


class Party(StrEnum):
    TRACK_B_BACKEND = "TRACK_B_BACKEND"
    TRACK_A_CONTROLLER = "TRACK_A_CONTROLLER"


class TimingPolicyReference(ContractModel):
    """Stable identity of one timing-policy configuration: its ID plus a content digest."""

    policy_id: Identifier
    digest: Sha256Hex


class ControllerStatus(ContractModel):
    """Body of ``GET /v1.1/health`` and ``GET /v1.1/ready``."""

    schema_version: SchemaVersion
    protocol_version: Literal["1.1"]
    service_id: Identifier
    experiment_id: Identifier
    ready: StrictBool
    policy_version: VersionLabel
    model_version: VersionLabel
    timing_policy: TimingPolicyReference
    not_ready_reasons: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def _semantics(self) -> "ControllerStatus":
        if self.ready == bool(self.not_ready_reasons):
            raise ValueError("not_ready_reasons must be given exactly when ready is false")
        return self


@dataclass(frozen=True)
class Endpoint:
    """One proposed v1.1 endpoint: who sends what to whom, and what can go wrong."""

    name: str
    method: Literal["GET", "POST"]
    path: str
    producer: Party
    consumer: Party
    request_model: type[BaseModel] | None
    response_model: type[BaseModel] | None
    success_status: int
    idempotency: str
    error_codes: tuple[ErrorCode, ...]


_E = ErrorCode
_DECODE_ERRORS = (_E.SCHEMA_INVALID, _E.UNSUPPORTED_SCHEMA_VERSION)
_AUTH_ERRORS = (_E.AUTHENTICATION_FAILED, _E.AUTHORIZATION_DENIED)
_IDENTITY_ERRORS = (_E.EXPERIMENT_MISMATCH, _E.ROUTE_IDENTITY_MISMATCH)

ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        name="telemetry_intake",
        method="POST",
        path=f"{PATH_PREFIX}/telemetry",
        producer=Party.TRACK_B_BACKEND,
        consumer=Party.TRACK_A_CONTROLLER,
        request_model=ValidatedRouteTelemetry,
        response_model=None,
        success_status=204,
        idempotency="(experiment_id, telemetry.source_id, telemetry.sequence); an identical "
        "resend answers 200 DUPLICATE_SEQUENCE, a different body with the same key 409",
        error_codes=(
            *_DECODE_ERRORS,
            *_AUTH_ERRORS,
            *_IDENTITY_ERRORS,
            _E.UNKNOWN_SOURCE,
            _E.UNTRUSTED_SOURCE,
            _E.OUT_OF_ORDER_SEQUENCE,
            _E.DUPLICATE_SEQUENCE,
            _E.CONFLICTING_SEQUENCE,
            _E.STALE_OBSERVATION,
            _E.FUTURE_TIMESTAMP,
            _E.INTERNAL_ERROR,
        ),
    ),
    Endpoint(
        name="transaction_event_intake",
        method="POST",
        path=f"{PATH_PREFIX}/transaction-events",
        producer=Party.TRACK_B_BACKEND,
        consumer=Party.TRACK_A_CONTROLLER,
        request_model=TransactionEvent,
        response_model=None,
        success_status=204,
        idempotency="event_id, and (transaction_id, state_version) by content; an identical "
        "resend answers 200 DUPLICATE_EVENT, different content 409 CONFLICTING_EVENT",
        error_codes=(
            *_DECODE_ERRORS,
            *_AUTH_ERRORS,
            *_IDENTITY_ERRORS,
            _E.UNKNOWN_SOURCE,
            _E.DUPLICATE_EVENT,
            _E.STALE_EVENT_VERSION,
            _E.CONFLICTING_EVENT,
            _E.EVIDENCE_REQUIRED,
            _E.EVIDENCE_CONFLICT,
            _E.TIMEOUT_CANNOT_IMPLY_OUTCOME,
            _E.INTERNAL_ERROR,
        ),
    ),
    Endpoint(
        name="status_check_result_intake",
        method="POST",
        path=f"{PATH_PREFIX}/status-check-results",
        producer=Party.TRACK_B_BACKEND,
        consumer=Party.TRACK_A_CONTROLLER,
        request_model=StatusCheckResult,
        response_model=None,
        success_status=204,
        idempotency="check_id; an identical resend answers 200 DUPLICATE_EVENT, different "
        "content 409 DUPLICATE_CHECK_ID. Sensing data only: the matching TransactionEvent, "
        "not this message, changes the transaction projection",
        error_codes=(
            *_DECODE_ERRORS,
            *_AUTH_ERRORS,
            *_IDENTITY_ERRORS,
            _E.UNKNOWN_TRANSACTION,
            _E.DUPLICATE_EVENT,
            _E.DUPLICATE_CHECK_ID,
            _E.EVIDENCE_REQUIRED,
            _E.INTERNAL_ERROR,
        ),
    ),
    Endpoint(
        name="recovery_decision",
        method="POST",
        path=f"{PATH_PREFIX}/recovery/decisions",
        producer=Party.TRACK_B_BACKEND,
        consumer=Party.TRACK_A_CONTROLLER,
        request_model=DecisionRequest,
        response_model=RecoveryDecision,
        success_status=200,
        idempotency="not idempotent: every call yields a new decision_id; safe to retry "
        "because a decision has no side effect until Track B's gate admits it",
        error_codes=(
            *_DECODE_ERRORS,
            *_AUTH_ERRORS,
            *_IDENTITY_ERRORS,
            _E.STALE_OBSERVATION,
            _E.FUTURE_TIMESTAMP,
            _E.INTERNAL_ERROR,
            _E.NOT_IMPLEMENTED,
        ),
    ),
    Endpoint(
        name="controller_health",
        method="GET",
        path=f"{PATH_PREFIX}/health",
        producer=Party.TRACK_A_CONTROLLER,
        consumer=Party.TRACK_B_BACKEND,
        request_model=None,
        response_model=ControllerStatus,
        success_status=200,
        idempotency="read-only",
        error_codes=(*_AUTH_ERRORS, _E.INTERNAL_ERROR),
    ),
    Endpoint(
        name="controller_readiness",
        method="GET",
        path=f"{PATH_PREFIX}/ready",
        producer=Party.TRACK_A_CONTROLLER,
        consumer=Party.TRACK_B_BACKEND,
        request_model=None,
        response_model=ControllerStatus,
        success_status=200,
        idempotency="read-only; 503 with ready=false and reasons when not ready",
        error_codes=(*_AUTH_ERRORS, _E.UPSTREAM_UNAVAILABLE, _E.INTERNAL_ERROR),
    ),
)
del _E

ENDPOINTS_BY_NAME: dict[str, Endpoint] = {endpoint.name: endpoint for endpoint in ENDPOINTS}


# --------------------------------------------------------------------------- errors

# Failures worth retrying unchanged after a delay. Everything else is terminal for that message.
RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.CHECK_TIMEOUT,
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
        ErrorCode.CREDIT_EXHAUSTED,
        ErrorCode.OUT_OF_ORDER_SEQUENCE,
        ErrorCode.INTERNAL_ERROR,
    }
)


class ProtocolViolation(Exception):
    """A message that must be refused with a structured v1.1 error. Never auto-repaired."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    def to_error_response(
        self, *, experiment_id: str | None = None, correlation_id: str | None = None
    ) -> ErrorResponse:
        return error_response(
            self.code,
            self.message,
            experiment_id=experiment_id,
            correlation_id=correlation_id,
        )


def error_response(
    code: ErrorCode,
    message: str,
    *,
    counts_against_limit: bool | None = None,
    retry_after_ms: int | None = None,
    experiment_id: str | None = None,
    correlation_id: str | None = None,
) -> ErrorResponse:
    """Build an ``ErrorResponse`` whose retryability and counting follow the shared tables.

    ``counts_against_limit`` is only taken from the caller for dispatcher-declared codes;
    every other code has a fixed counting rule that the caller cannot override.
    """

    rule = CHECK_COUNTING[code]
    fixed = {
        CheckCounting.COUNTED: True,
        CheckCounting.NOT_COUNTED: False,
        CheckCounting.NOT_APPLICABLE: None,
    }
    if rule is CheckCounting.DECLARED_BY_DISPATCHER:
        if counts_against_limit is None:
            raise ValueError(f"{code} requires the dispatcher to declare counts_against_limit")
        counted = counts_against_limit
    else:
        if counts_against_limit is not None and counts_against_limit is not fixed[rule]:
            raise ValueError(f"{code} has a fixed counting rule ({rule})")
        counted = fixed[rule]
    return ErrorResponse(
        schema_version=SCHEMA_VERSION,
        error_code=code,
        message=message[:1000] or str(code),
        retryable=code in RETRYABLE_CODES,
        counts_against_limit=counted,
        retry_after_ms=retry_after_ms if code in RETRYABLE_CODES else None,
        correlation_id=correlation_id,
        experiment_id=experiment_id,
    )


# ---------------------------------------------------------------------------- codec


def encode(message: BaseModel) -> bytes:
    """Deterministic wire bytes: compact UTF-8 JSON, sorted keys, UTC ``Z`` timestamps."""

    return json.dumps(
        message.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def decode(model: type[Model], raw: bytes | str | Mapping[str, Any]) -> Model:
    """Parse one wire message into ``model`` or raise ``ProtocolViolation``.

    A missing or different ``schema_version`` is ``UNSUPPORTED_SCHEMA_VERSION`` when the model
    has one; every other structural problem, unknown fields included, is ``SCHEMA_INVALID``.
    Nothing is coerced, defaulted or repaired beyond what the model itself defines.
    """

    if isinstance(raw, bytes | str):
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolViolation(ErrorCode.SCHEMA_INVALID, f"body is not JSON: {exc}") from exc
    else:
        data = raw
    if not isinstance(data, Mapping):
        raise ProtocolViolation(ErrorCode.SCHEMA_INVALID, "body must be a JSON object")
    if "schema_version" in model.model_fields and data.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolViolation(
            ErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            f"schema_version must be {SCHEMA_VERSION!r}, got {data.get('schema_version')!r}",
        )
    try:
        return model.model_validate(dict(data))
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "<root>"
        raise ProtocolViolation(
            ErrorCode.SCHEMA_INVALID,
            f"{model.__name__} invalid at {where}: {first['msg']} ({exc.error_count()} error(s))",
        ) from exc


# --------------------------------------------------------------------- timing policy


def timing_policy_digest(policy: TimingPolicy) -> str:
    return hashlib.sha256(encode(policy)).hexdigest()


def timing_policy_reference(policy: TimingPolicy) -> TimingPolicyReference:
    return TimingPolicyReference(policy_id=policy.policy_id, digest=timing_policy_digest(policy))


def timing_policy_mismatches(
    advertised: TimingPolicyReference, backend_policy: TimingPolicy
) -> tuple[str, ...]:
    """Why the controller's advertised policy differs from the policy Track B enforces."""

    problems = []
    if advertised.policy_id != backend_policy.policy_id:
        problems.append(
            f"policy_id differs: controller {advertised.policy_id!r}, "
            f"backend {backend_policy.policy_id!r}"
        )
    elif advertised.digest != timing_policy_digest(backend_policy):
        problems.append(f"policy {advertised.policy_id!r} has different content on each side")
    return tuple(problems)


# ---------------------------------------------------------- controller-side request rules

# Fields the starter /v1 controller request carried. In v1.1 the controller derives capacity
# from its Route Twin and candidates from its Transaction Twin, so a caller cannot supply them.
STARTER_REQUEST_FIELDS = frozenset(
    {"estimated_capacity_rps", "confidence", "pending_transaction_ids"}
)


def validate_decision_request(
    raw: bytes | str | Mapping[str, Any],
    *,
    experiment_id: str,
    known_routes: frozenset[str],
    now: datetime,
    max_request_age_s: float,
    max_future_skew_s: float,
) -> DecisionRequest:
    """Controller-side intake of ``POST /v1.1/recovery/decisions``."""

    if isinstance(raw, Mapping) and (starter := STARTER_REQUEST_FIELDS & set(raw)):
        raise ProtocolViolation(
            ErrorCode.SCHEMA_INVALID,
            f"starter fields {sorted(starter)} are not accepted: the controller derives "
            "capacity and candidates from its own twins",
        )
    request = decode(DecisionRequest, raw)
    if request.experiment_id != experiment_id:
        raise ProtocolViolation(
            ErrorCode.EXPERIMENT_MISMATCH,
            f"controller serves {experiment_id!r}, request is for {request.experiment_id!r}",
        )
    if request.route_id not in known_routes:
        raise ProtocolViolation(
            ErrorCode.ROUTE_IDENTITY_MISMATCH, f"route {request.route_id!r} is not registered"
        )
    age = (now - request.requested_at).total_seconds()
    if age < -max_future_skew_s:
        raise ProtocolViolation(ErrorCode.FUTURE_TIMESTAMP, "requested_at is in the future")
    if age > max_request_age_s:
        raise ProtocolViolation(ErrorCode.STALE_OBSERVATION, f"request is {age:.3f} s old")
    return request


def lagging_sources(
    required: tuple[SourceSequenceMark, ...], accepted: Mapping[str, int]
) -> tuple[SourceSequenceMark, ...]:
    """Watermark entries the controller has not reached yet (unknown sources count as lagging)."""

    return tuple(mark for mark in required if accepted.get(mark.source_id, -1) < mark.sequence)


def build_fail_closed_decision(
    request: DecisionRequest,
    *,
    decision_id: str,
    reason: DecisionReason,
    detail: str,
    created_at: datetime,
    validity_s: float,
    policy_version: str,
    model_version: str,
    timing_policy_id: str,
    telemetry_watermark: tuple[SourceSequenceMark, ...] = (),
    latest_observation_at: datetime | None = None,
) -> RecoveryDecision:
    """An explicit, valid zero-budget decision. Its reason must be a fail-closed reason."""

    return RecoveryDecision(
        schema_version=SCHEMA_VERSION,
        experiment_id=request.experiment_id,
        route_id=request.route_id,
        decision_id=decision_id,
        policy_version=policy_version,
        model_version=model_version,
        timing_policy_id=timing_policy_id,
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=validity_s),
        decision_mode=DecisionMode.FAIL_CLOSED,
        budget_rps=0,
        reason_code=reason,
        reason_detail=detail,
        telemetry_watermark=telemetry_watermark,
        latest_observation_at=latest_observation_at,
    )


# ---------------------------------------------------------- backend-side decision rules


class ControllerOutcome(StrEnum):
    """What one controller exchange amounted to, as seen by Track B's dispatcher."""

    DECISION = "DECISION"
    EXPLICIT_FAIL_CLOSED = "EXPLICIT_FAIL_CLOSED"
    WARMING_UP = "WARMING_UP"
    CONTROLLER_UNAVAILABLE = "CONTROLLER_UNAVAILABLE"
    CONTROLLER_TIMEOUT = "CONTROLLER_TIMEOUT"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    DECISION_EXPIRED = "DECISION_EXPIRED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    TIMING_POLICY_MISMATCH = "TIMING_POLICY_MISMATCH"
    UNAUTHORIZED_CONTROLLER = "UNAUTHORIZED_CONTROLLER"


@dataclass(frozen=True)
class FallbackRule:
    """Provisional handling of one outcome (decision register M1–M7 in the README).

    ``controller_directed_checks`` is the number of checks the controller authorises: the
    decision's own bound for ``DECISION`` (``None``), otherwise zero. ``backend_fallback`` says
    whether Track B's own, separately configured and labelled fallback policy may run instead.
    No rule ever authorises a reversal or any other financial operation.
    """

    controller_directed_checks: int | None
    backend_fallback: bool
    retry: str


FALLBACK_RULES: dict[ControllerOutcome, FallbackRule] = {
    ControllerOutcome.DECISION: FallbackRule(None, False, "none"),
    ControllerOutcome.EXPLICIT_FAIL_CLOSED: FallbackRule(0, False, "next tick"),
    ControllerOutcome.WARMING_UP: FallbackRule(0, False, "next tick"),
    ControllerOutcome.CONTROLLER_UNAVAILABLE: FallbackRule(0, True, "next tick, with backoff"),
    ControllerOutcome.CONTROLLER_TIMEOUT: FallbackRule(0, True, "next tick, with backoff"),
    ControllerOutcome.MALFORMED_RESPONSE: FallbackRule(0, True, "next tick; alert, do not repair"),
    ControllerOutcome.DECISION_EXPIRED: FallbackRule(0, True, "request a new decision"),
    ControllerOutcome.IDENTITY_MISMATCH: FallbackRule(0, True, "next tick; alert"),
    ControllerOutcome.TIMING_POLICY_MISMATCH: FallbackRule(0, True, "after operators align"),
    ControllerOutcome.UNAUTHORIZED_CONTROLLER: FallbackRule(0, True, "after operators fix keys"),
}


class TransportFailure(StrEnum):
    CONNECT_ERROR = "CONNECT_ERROR"
    TIMEOUT = "TIMEOUT"
    HTTP_5XX = "HTTP_5XX"


def outcome_for_transport_failure(failure: TransportFailure) -> ControllerOutcome:
    if failure is TransportFailure.TIMEOUT:
        return ControllerOutcome.CONTROLLER_TIMEOUT
    return ControllerOutcome.CONTROLLER_UNAVAILABLE


class GateRefusal(StrEnum):
    """Why Track B's execution gate refused a whole decision or one selected transaction."""

    # Whole decision.
    MALFORMED = "MALFORMED"
    EXPERIMENT_MISMATCH = "EXPERIMENT_MISMATCH"
    ROUTE_MISMATCH = "ROUTE_MISMATCH"
    NOT_YET_VALID = "NOT_YET_VALID"
    EXPIRED = "EXPIRED"
    UNAUTHORIZED_CONTROLLER = "UNAUTHORIZED_CONTROLLER"
    TIMING_POLICY_MISMATCH = "TIMING_POLICY_MISMATCH"
    # One selected transaction.
    UNKNOWN_TRANSACTION = "UNKNOWN_TRANSACTION"
    WRONG_ROUTE = "WRONG_ROUTE"
    WRONG_EXPERIMENT = "WRONG_EXPERIMENT"
    TERMINAL = "TERMINAL"
    STALE_VERSION = "STALE_VERSION"
    CHECK_IN_FLIGHT = "CHECK_IN_FLIGHT"
    NOT_YET_ELIGIBLE = "NOT_YET_ELIGIBLE"
    WINDOW_CLOSED = "WINDOW_CLOSED"
    CHECK_LIMIT_REACHED = "CHECK_LIMIT_REACHED"
    OVER_BUDGET = "OVER_BUDGET"
    RATE_LIMITED = "RATE_LIMITED"
    CREDIT_EXHAUSTED = "CREDIT_EXHAUSTED"


# Exact v1.1 error codes for refusals that have one. The others are backend-local outcomes.
ERROR_CODE_FOR_REFUSAL: dict[GateRefusal, ErrorCode] = {
    GateRefusal.MALFORMED: ErrorCode.SCHEMA_INVALID,
    GateRefusal.EXPERIMENT_MISMATCH: ErrorCode.EXPERIMENT_MISMATCH,
    GateRefusal.ROUTE_MISMATCH: ErrorCode.ROUTE_IDENTITY_MISMATCH,
    GateRefusal.EXPIRED: ErrorCode.DECISION_EXPIRED,
    GateRefusal.UNAUTHORIZED_CONTROLLER: ErrorCode.AUTHORIZATION_DENIED,
    GateRefusal.UNKNOWN_TRANSACTION: ErrorCode.UNKNOWN_TRANSACTION,
    GateRefusal.WRONG_ROUTE: ErrorCode.ROUTE_IDENTITY_MISMATCH,
    GateRefusal.WRONG_EXPERIMENT: ErrorCode.EXPERIMENT_MISMATCH,
    GateRefusal.STALE_VERSION: ErrorCode.STALE_VERSION,
    GateRefusal.NOT_YET_ELIGIBLE: ErrorCode.NOT_YET_ELIGIBLE,
    GateRefusal.CHECK_LIMIT_REACHED: ErrorCode.CHECK_LIMIT_REACHED,
    GateRefusal.RATE_LIMITED: ErrorCode.RATE_LIMITED,
    GateRefusal.CREDIT_EXHAUSTED: ErrorCode.CREDIT_EXHAUSTED,
}

_DECISION_REFUSAL_OUTCOME: dict[GateRefusal, ControllerOutcome] = {
    GateRefusal.MALFORMED: ControllerOutcome.MALFORMED_RESPONSE,
    GateRefusal.EXPERIMENT_MISMATCH: ControllerOutcome.IDENTITY_MISMATCH,
    GateRefusal.ROUTE_MISMATCH: ControllerOutcome.IDENTITY_MISMATCH,
    GateRefusal.NOT_YET_VALID: ControllerOutcome.MALFORMED_RESPONSE,
    GateRefusal.EXPIRED: ControllerOutcome.DECISION_EXPIRED,
    GateRefusal.UNAUTHORIZED_CONTROLLER: ControllerOutcome.UNAUTHORIZED_CONTROLLER,
    GateRefusal.TIMING_POLICY_MISMATCH: ControllerOutcome.TIMING_POLICY_MISMATCH,
}


@dataclass(frozen=True)
class DispatchCandidate:
    """Track B's authoritative view of one transaction at execution time (read under lock)."""

    transaction_id: str
    experiment_id: str
    route_id: str
    state_version: int
    terminal: bool
    check_in_flight: bool
    counted_check_attempts: int
    eligible_from: datetime | None
    eligible_until: datetime | None


@dataclass(frozen=True)
class DispatchLimits:
    """Remaining allowances for this route at execution time. ``None`` means not enforced."""

    rate_allowance: int
    credit_allowance: int | None = None


@dataclass(frozen=True)
class DispatchPlan:
    """Result of gating one decision. ``admitted`` may be dispatched as status checks only."""

    outcome: ControllerOutcome
    decision_id: str | None
    decision_refusal: GateRefusal | None
    admitted: tuple[str, ...]
    refused: tuple[tuple[str, GateRefusal], ...]

    @property
    def rule(self) -> FallbackRule:
        return FALLBACK_RULES[self.outcome]

    @property
    def backend_fallback_permitted(self) -> bool:
        return self.rule.backend_fallback


def _whole_decision_refused(decision_id: str | None, refusal: GateRefusal) -> DispatchPlan:
    return DispatchPlan(_DECISION_REFUSAL_OUTCOME[refusal], decision_id, refusal, (), ())


def gate_decision(
    raw: bytes | str | Mapping[str, Any],
    *,
    request: DecisionRequest,
    controller_id: str,
    authorized_controllers: frozenset[str],
    timing_policy: TimingPolicy,
    candidates: Mapping[str, DispatchCandidate],
    limits: DispatchLimits,
    now: datetime,
    dispatch_window_s: float,
    max_clock_skew_s: float,
) -> DispatchPlan:
    """Pure execution gate for one controller response (contract requirements X1–X11).

    ``controller_id`` is the identity Track B's transport layer authenticated; it is never
    read from the response body. Nothing is repaired: an invalid decision is refused whole.
    """

    try:
        decision = decode(RecoveryDecision, raw)
    except ProtocolViolation:
        return _whole_decision_refused(None, GateRefusal.MALFORMED)
    decision_id = decision.decision_id
    if controller_id not in authorized_controllers:
        return _whole_decision_refused(decision_id, GateRefusal.UNAUTHORIZED_CONTROLLER)
    if decision.experiment_id != request.experiment_id:
        return _whole_decision_refused(decision_id, GateRefusal.EXPERIMENT_MISMATCH)
    if decision.route_id != request.route_id:
        return _whole_decision_refused(decision_id, GateRefusal.ROUTE_MISMATCH)
    if decision.created_at > now + timedelta(seconds=max_clock_skew_s):
        return _whole_decision_refused(decision_id, GateRefusal.NOT_YET_VALID)
    if now >= decision.expires_at:
        return _whole_decision_refused(decision_id, GateRefusal.EXPIRED)
    if decision.timing_policy_id != timing_policy.policy_id:
        return _whole_decision_refused(decision_id, GateRefusal.TIMING_POLICY_MISMATCH)

    if decision.decision_mode is not DecisionMode.NORMAL:
        outcome = (
            ControllerOutcome.EXPLICIT_FAIL_CLOSED
            if decision.decision_mode is DecisionMode.FAIL_CLOSED
            else ControllerOutcome.WARMING_UP
        )
        return DispatchPlan(outcome, decision_id, None, (), ())

    budget = floor(decision.budget_rps * dispatch_window_s)
    expected_versions = {ref.transaction_id: ref.version for ref in decision.state_versions}
    admitted: list[str] = []
    refused: list[tuple[str, GateRefusal]] = []
    for txn_id in decision.selected_transaction_ids:
        refusal = _transaction_refusal(
            candidates.get(txn_id), expected_versions[txn_id], decision, timing_policy, now
        )
        if refusal is None:
            if len(admitted) >= budget:
                refusal = GateRefusal.OVER_BUDGET
            elif len(admitted) >= limits.rate_allowance:
                refusal = GateRefusal.RATE_LIMITED
            elif limits.credit_allowance is not None and len(admitted) >= limits.credit_allowance:
                refusal = GateRefusal.CREDIT_EXHAUSTED
        if refusal is None:
            admitted.append(txn_id)
        else:
            refused.append((txn_id, refusal))
    return DispatchPlan(
        ControllerOutcome.DECISION, decision_id, None, tuple(admitted), tuple(refused)
    )


def _transaction_refusal(
    candidate: DispatchCandidate | None,
    expected_version: int,
    decision: RecoveryDecision,
    policy: TimingPolicy,
    now: datetime,
) -> GateRefusal | None:
    if candidate is None:
        return GateRefusal.UNKNOWN_TRANSACTION
    if candidate.experiment_id != decision.experiment_id:
        return GateRefusal.WRONG_EXPERIMENT
    if candidate.route_id != decision.route_id:
        return GateRefusal.WRONG_ROUTE
    if candidate.terminal:
        return GateRefusal.TERMINAL
    if candidate.state_version != expected_version:
        return GateRefusal.STALE_VERSION
    if candidate.check_in_flight:
        return GateRefusal.CHECK_IN_FLIGHT
    if candidate.counted_check_attempts >= policy.max_counted_checks:
        return GateRefusal.CHECK_LIMIT_REACHED
    if candidate.eligible_from is None or now < candidate.eligible_from:
        return GateRefusal.NOT_YET_ELIGIBLE
    if candidate.eligible_until is not None and now > candidate.eligible_until:
        return GateRefusal.WINDOW_CLOSED
    return None


# Re-exported for callers that only import the protocol module.
__all__ = [
    "ENDPOINTS",
    "ENDPOINTS_BY_NAME",
    "ERROR_CODE_FOR_REFUSAL",
    "FALLBACK_RULES",
    "MEDIA_TYPE",
    "PATH_PREFIX",
    "PROTOCOL_VERSION",
    "RETRYABLE_CODES",
    "STARTER_REQUEST_FIELDS",
    "ControllerOutcome",
    "ControllerStatus",
    "DispatchCandidate",
    "DispatchLimits",
    "DispatchPlan",
    "Endpoint",
    "FallbackRule",
    "GateRefusal",
    "Party",
    "ProtocolViolation",
    "TimingPolicyReference",
    "TransportFailure",
    "UtcDatetime",
    "build_fail_closed_decision",
    "decode",
    "encode",
    "error_response",
    "gate_decision",
    "lagging_sources",
    "outcome_for_transport_failure",
    "timing_policy_digest",
    "timing_policy_mismatches",
    "timing_policy_reference",
    "validate_decision_request",
]
