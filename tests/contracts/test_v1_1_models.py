"""Contract v1.1: valid examples, enum vocabularies, common rules and v1 coexistence.

The example payloads defined here are reused by the validation and serialization tests.
"""

import copy
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import pytest
from pydantic import BaseModel, ValidationError

import contracts.v1_1 as v11
from contracts.v1 import models as v1
from contracts.v1_1 import (
    CHECK_COUNTING,
    HTTP_STATUS,
    CreditConsumption,
    CreditLease,
    DeadlineRule,
    DecisionRequest,
    ErrorCode,
    ErrorResponse,
    EvidenceReference,
    EvidenceSignature,
    ExperimentalCheckLimits,
    RecoveryDecision,
    RouteDescriptor,
    RouteTelemetry,
    RouteTwinSnapshot,
    SourceSequenceMark,
    StatusCheckRequest,
    StatusCheckResult,
    TimingPolicy,
    TransactionEvent,
    TransactionProjection,
    TransactionVersionReference,
    ValidatedRouteTelemetry,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
EXP = "EXP-1"
ROUTE = "A1-B"
PROBE = "remitter-A1-probe"


def ts(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _evidence_reference(kind: str = "STATUS_CHECK_RESPONSE") -> dict:
    return {"evidence_id": "EV-1", "evidence_kind": kind, "issuer_id": "SW", "response_version": 1}


def _evidence_signature() -> dict:
    return {
        "signature_alg": "HMAC-SHA256",
        "key_id": "key-1",
        "signature": "c2lnbmF0dXJl",
        "signed_fields": ["transaction_id", "check_result", "response_version"],
    }


def _telemetry() -> dict:
    return {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "route_id": ROUTE,
        "source_id": PROBE,
        "observation_scope": "REMITTER_OWN_TRAFFIC",
        "sequence": 1,
        "observation_window_start": ts(0),
        "observation_window_end": ts(1),
        "observation_timestamp": ts(1),
        "payment_rps": 700.0,
        "check_rps": 10.0,
        "observed_throughput_rps": 705.0,
        "completed_payment_rps": 700.0,
        "completed_check_rps": 5.0,
        "p50_latency_ms": 120.0,
        "p95_latency_ms": 900.0,
        "latency_sample_count": 705,
        "timeout_rate": 0.01,
        "timeout_count": 7,
        "timeout_denominator_count": 700,
        "queue_depth": 40,
    }


def _validated() -> dict:
    return {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "route_id": ROUTE,
        "telemetry": _telemetry(),
        "trust_status": "AUTHENTICATED",
        "validator_id": "validator-1",
        "validator_version": "0.1.0",
        "validated_at": ts(1.2),
        "validation_flags": [],
        "cross_check_status": "CONSISTENT",
    }


def _deadline_rule() -> dict:
    return {"kind": "FIXED_DURATION", "duration_s": 3600.0}


EXAMPLES = {
    RouteDescriptor: lambda: {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "route_id": ROUTE,
        "remitter_bank_id": "A1",
        "switch_id": "SW",
        "beneficiary_bank_id": "B",
    },
    EvidenceReference: _evidence_reference,
    EvidenceSignature: _evidence_signature,
    TransactionEvent: lambda: {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "event_id": "EV-0001",
        "transaction_id": "T-1",
        "route_id": ROUTE,
        "remitter_bank_id": "A1",
        "beneficiary_bank_id": "B",
        "event_type": "PAYMENT_INITIATED",
        "ledger_state": "DEBITED",
        "financial_outcome": "UNKNOWN",
        "state_version": 1,
        "amount_minor": 50000,
        "event_timestamp": ts(0),
        "source_id": "remitter-A1",
        "source_sequence": 0,
        "correlation_id": "PAY-1",
        "counted_check_attempts": 0,
    },
    RouteTelemetry: _telemetry,
    ValidatedRouteTelemetry: _validated,
    SourceSequenceMark: lambda: {"source_id": PROBE, "sequence": 1},
    RouteTwinSnapshot: lambda: {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "route_id": ROUTE,
        "snapshot_version": 1,
        "model_version": "route-twin-0.1",
        "last_updated_at": ts(2),
        "last_observed_at": ts(1),
        "latest_sequence_per_source": [{"source_id": PROBE, "sequence": 1}],
        "freshness_status": "FRESH",
        "latest_trusted_observation": _telemetry(),
    },
    TransactionProjection: lambda: {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "transaction_id": "T-1",
        "route_id": ROUTE,
        "remitter_bank_id": "A1",
        "beneficiary_bank_id": "B",
        "amount_minor": 50000,
        "financial_outcome": "UNKNOWN",
        "ledger_state_observed": "DEBITED",
        "workflow_state": "AMBIGUOUS",
        "version": 2,
        "last_applied_event_id": "EV-0002",
        "initiated_at": ts(0),
        "ambiguity_created_at": ts(15),
        "checks_used": 0,
        "next_eligible_at": ts(90),
        "deadline_at": ts(3600),
        "deadline_policy_id": "research-scaled-60min",
        "deadline_escalated": False,
        "terminal": False,
    },
    TransactionVersionReference: lambda: {"transaction_id": "T-1", "version": 2},
    DecisionRequest: lambda: {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "route_id": ROUTE,
        "requested_at": ts(100),
        "min_telemetry_watermark": [{"source_id": PROBE, "sequence": 1}],
    },
    RecoveryDecision: lambda: {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "route_id": ROUTE,
        "decision_id": "D-1",
        "policy_version": "aorts-0.1",
        "model_version": "route-twin-0.1",
        "timing_policy_id": "research-scaled-60min",
        "created_at": ts(100),
        "expires_at": ts(101),
        "decision_mode": "NORMAL",
        "budget_rps": 40,
        "selected_transaction_ids": ["T-1", "T-2"],
        "state_versions": [
            {"transaction_id": "T-1", "version": 2},
            {"transaction_id": "T-2", "version": 3},
        ],
        "predicted_delay_ms": 1600.0,
        "predicted_new_ambiguous": 0.0,
        "prediction_horizon_s": 30.0,
        "reason_code": "SAFE_BUDGET_SELECTED",
        "reason_detail": "largest candidate budget meeting the delay target",
        "telemetry_watermark": [{"source_id": PROBE, "sequence": 1}],
        "latest_observation_at": ts(99),
    },
    StatusCheckRequest: lambda: {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "check_id": "CHK-1",
        "transaction_id": "T-1",
        "route_id": ROUTE,
        "remitter_bank_id": "A1",
        "decision_id": "D-1",
        "requested_at": ts(100.5),
    },
    StatusCheckResult: lambda: {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "check_id": "CHK-1",
        "transaction_id": "T-1",
        "route_id": ROUTE,
        "check_result": "AUTHORITATIVE_FAILURE",
        "evidence_reference": _evidence_reference(),
        "evidence_signature": _evidence_signature(),
        "evidence_verified": True,
        "counts_against_limit": True,
        "counted_check_attempts": 1,
        "latency_ms": 1400.0,
        "responded_at": ts(102),
    },
    ErrorResponse: lambda: {
        "schema_version": "1.1",
        "error_code": "CREDIT_EXHAUSTED",
        "message": "no Tier 2 credit available for this route",
        "retryable": True,
        "counts_against_limit": False,
        "retry_after_ms": 1000,
        "correlation_id": "CHK-2",
        "experiment_id": EXP,
    },
    ExperimentalCheckLimits: lambda: {
        "first_check_delay_s": 180.0,
        "max_counted_checks": 2,
        "limit_class": "STORM",
    },
    CreditLease: lambda: {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "lease_id": "L-1",
        "regime": "EXPERIMENTAL_MOCK_SWITCH",
        "route_id": ROUTE,
        "bank_id": "A1",
        "credits_granted": 27,
        "credits_consumed": 3,
        "issued_at": ts(100),
        "expires_at": ts(101),
        "coordinator_version": "coord-0.1",
        "limits": {"first_check_delay_s": 180.0, "max_counted_checks": 2, "limit_class": "STORM"},
    },
    CreditConsumption: lambda: {
        "schema_version": "1.1",
        "experiment_id": EXP,
        "lease_id": "L-1",
        "check_id": "CHK-1",
        "consumed_at": ts(100.5),
        "accepted": True,
    },
    DeadlineRule: _deadline_rule,
    TimingPolicy: lambda: {
        "schema_version": "1.1",
        "policy_id": "research-scaled-60min",
        "regime": "RESEARCH_SCALED",
        "verification_status": "RESEARCH_PARAMETER",
        "first_check_delay_s": 90.0,
        "first_check_reference": "INITIATED",
        "min_check_spacing_s": 90.0,
        "max_counted_checks": 3,
        "check_window_s": 7200.0,
        "stop_error_codes": [],
        "client_timeout_s": 15.0,
        "check_timeout_s": 10.0,
        "deadline_rule": _deadline_rule(),
        "escalation_window_s": 600.0,
        "escalation_extra_checks": 0,
    },
}

ALL_MODELS = list(EXAMPLES)
VERSIONED_MODELS = [m for m in ALL_MODELS if "schema_version" in m.model_fields]


def payload(model: type[BaseModel], **overrides) -> dict:
    """A fresh valid payload for model with top-level fields overridden."""

    data = copy.deepcopy(EXAMPLES[model]())
    data.update(overrides)
    return data


def build(model: type[BaseModel], **overrides) -> BaseModel:
    return model.model_validate(payload(model, **overrides))


def _ids(models: list[type[BaseModel]]) -> list[str]:
    return [m.__name__ for m in models]


def test_every_exported_model_has_an_example():
    exported = {
        obj
        for name in v11.__all__
        if isinstance(obj := getattr(v11, name), type) and issubclass(obj, BaseModel)
    }
    assert exported == set(ALL_MODELS)


@pytest.mark.parametrize("model", ALL_MODELS, ids=_ids(ALL_MODELS))
def test_example_is_valid(model):
    instance = build(model)
    assert isinstance(instance, model)


@pytest.mark.parametrize("model", ALL_MODELS, ids=_ids(ALL_MODELS))
def test_instances_are_frozen(model):
    instance = build(model)
    field = next(iter(model.model_fields))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(instance, field, getattr(instance, field))


def test_sequences_are_tuples_so_instances_are_deeply_immutable():
    decision = build(RecoveryDecision)
    assert isinstance(decision.selected_transaction_ids, tuple)
    assert isinstance(decision.state_versions, tuple)
    assert isinstance(build(EvidenceSignature).signed_fields, tuple)


EXPECTED_ENUMS = {
    v11.FinancialOutcome: ["UNKNOWN", "PENDING", "SUCCESS", "FAILURE"],
    v11.LedgerState: [
        "DEBITED",
        "CONFIRMED_SUCCESS",
        "CONFIRMED_FAILED",
        "RECONCILIATION_PENDING",
        "REVERSED",
        "CLOSED",
    ],
    v11.RecoveryWorkflowState: [
        "ACTIVE",
        "AMBIGUOUS",
        "CHECK_IN_FLIGHT",
        "ESCALATED",
        "HANDOFF_RECONCILIATION",
        "QUARANTINED",
        "CLOSED",
    ],
    v11.TransactionEventType: [
        "PAYMENT_INITIATED",
        "PAYMENT_RESPONSE_RECEIVED",
        "PAYMENT_TIMED_OUT",
        "STATUS_CHECK_DISPATCHED",
        "STATUS_CHECK_ANSWERED",
        "STATUS_CHECK_REJECTED",
        "STATUS_CHECK_TIMED_OUT",
        "REVERSAL_COMPLETED",
        "RECONCILIATION_HANDOFF",
        "RECONCILIATION_RESOLVED",
        "EVIDENCE_CONFLICT_RECORDED",
        "TRANSACTION_CLOSED",
    ],
    v11.EvidenceKind: [
        "ORIGINAL_PAYMENT_RESPONSE",
        "STATUS_CHECK_RESPONSE",
        "RECONCILIATION_RECORD",
    ],
    v11.CheckResultKind: ["AUTHORITATIVE_SUCCESS", "AUTHORITATIVE_FAILURE", "PENDING", "UNKNOWN"],
    v11.ObservationScope: ["REMITTER_OWN_TRAFFIC", "SWITCH_ROUTE", "SWITCH_GLOBAL"],
    v11.TelemetryTrustStatus: ["AUTHENTICATED", "UNAUTHENTICATED", "UNKNOWN_SOURCE"],
    v11.TelemetryValidationFlag: [
        "RANGE_VIOLATION",
        "CROSS_CHECK_DEVIATION",
        "STATISTICAL_ANOMALY",
        "SEQUENCE_GAP",
        "CLOCK_SKEW",
    ],
    v11.CrossCheckStatus: ["CONSISTENT", "INCONSISTENT", "UNAVAILABLE"],
    v11.FreshnessStatus: ["FRESH", "STALE", "NO_DATA"],
    v11.QuarantineReason: [
        "EVIDENCE_CONFLICT",
        "CONFLICTING_EVENT",
        "IDENTITY_MISMATCH",
        "INVARIANT_VIOLATION",
    ],
    v11.DecisionMode: ["NORMAL", "FAIL_CLOSED", "WARMING_UP"],
    v11.DecisionReason: [
        "SAFE_BUDGET_SELECTED",
        "NO_SAFE_BUDGET",
        "NO_ELIGIBLE_TRANSACTIONS",
        "NO_CAPACITY_ESTIMATE",
        "TELEMETRY_STALE",
        "TELEMETRY_UNTRUSTED",
        "CONTROLLER_WARMING_UP",
        "BOOTSTRAP_FAIL_CLOSED",
    ],
    v11.Tier2LimitClass: ["CALM", "NORMAL", "STORM"],
    v11.TimingRegime: ["TIER1_COMPLIANT", "RESEARCH_SCALED", "TIER2_EXPERIMENTAL"],
    v11.VerificationStatus: ["VERIFIED", "SOURCE_CLAIMED_UNVERIFIED", "RESEARCH_PARAMETER"],
    v11.FirstCheckReference: ["INITIATED", "TIMED_OUT"],
    v11.DeadlineRuleKind: ["FIXED_DURATION", "REGULATORY_CALENDAR"],
    v11.CheckCounting: ["COUNTED", "NOT_COUNTED", "DECLARED_BY_DISPATCHER", "NOT_APPLICABLE"],
}


@pytest.mark.parametrize("enum", list(EXPECTED_ENUMS), ids=lambda e: e.__name__)
def test_enum_wire_values_are_pinned(enum):
    assert [member.value for member in enum] == EXPECTED_ENUMS[enum]
    for member in enum:
        assert enum(member.value) is member
        assert member.value == member.name


def test_every_exported_enum_is_pinned():
    exported = {
        obj
        for name in v11.__all__
        if isinstance(obj := getattr(v11, name), type) and issubclass(obj, StrEnum)
    }
    assert exported - {ErrorCode} == set(EXPECTED_ENUMS)


def test_every_error_code_has_http_status_and_counting_rule():
    assert set(HTTP_STATUS) == set(ErrorCode)
    assert set(CHECK_COUNTING) == set(ErrorCode)
    assert all(100 <= status <= 599 for status in HTTP_STATUS.values())


@pytest.mark.parametrize("model", VERSIONED_MODELS, ids=_ids(VERSIONED_MODELS))
@pytest.mark.parametrize("version", ["1.0", "1.2", "1.10", "v1.1", ""])
def test_wrong_schema_version_is_rejected(model, version):
    with pytest.raises(ValidationError, match="schema_version"):
        build(model, schema_version=version)


@pytest.mark.parametrize("model", VERSIONED_MODELS, ids=_ids(VERSIONED_MODELS))
def test_schema_version_is_required(model):
    data = payload(model)
    del data["schema_version"]
    with pytest.raises(ValidationError, match="schema_version"):
        model.model_validate(data)


@pytest.mark.parametrize("model", ALL_MODELS, ids=_ids(ALL_MODELS))
def test_unknown_field_is_rejected(model):
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        build(model, unexpected_field=1)


IDENTIFIER_FIELDS = [
    (RouteDescriptor, "route_id"),
    (TransactionEvent, "event_id"),
    (TransactionEvent, "transaction_id"),
    (RouteTelemetry, "source_id"),
    (StatusCheckRequest, "check_id"),
    (CreditLease, "lease_id"),
    (TimingPolicy, "policy_id"),
    (RecoveryDecision, "decision_id"),
]


@pytest.mark.parametrize(("model", "field"), IDENTIFIER_FIELDS)
def test_empty_identifier_is_rejected(model, field):
    with pytest.raises(ValidationError, match=field):
        build(model, **{field: ""})


@pytest.mark.parametrize(
    "bad_value",
    ["has space", "slash/char", "ünicode", "-leading-dash", ".leading-dot", "x" * 129, "tab\t"],
)
@pytest.mark.parametrize(("model", "field"), IDENTIFIER_FIELDS[:4])
def test_invalid_identifier_characters_are_rejected(model, field, bad_value):
    with pytest.raises(ValidationError, match=field):
        build(model, **{field: bad_value})


@pytest.mark.parametrize("good_value", ["A", "a1", "A1-B.c:d_e", "x" * 128])
def test_valid_identifier_forms_are_accepted(good_value):
    assert build(RouteDescriptor, route_id=good_value).route_id == good_value


def test_identifiers_must_be_strings():
    with pytest.raises(ValidationError, match="route_id"):
        build(RouteDescriptor, route_id=123)


# ------------------------------------------------------------------ v1 compatibility
# The team contract ``contracts/v1`` (served by Track B) stays frozen; v1.1 sits beside it.

V1_FIELDS = {
    v1.TransactionEvent: [
        "schema_version",
        "event_id",
        "experiment_id",
        "transaction_id",
        "bank_id",
        "route_id",
        "amount_minor",
        "currency",
        "event_type",
        "financial_outcome",
        "workflow_state",
        "version",
        "occurred_at",
    ],
    v1.RouteTelemetry: [
        "schema_version",
        "experiment_id",
        "route_id",
        "timestamp",
        "payment_rps",
        "check_rps",
        "observed_throughput_rps",
        "p95_latency_ms",
        "timeout_rate",
        "queue_depth",
        "source_id",
        "sequence",
    ],
    v1.RecoveryDecision: [
        "schema_version",
        "experiment_id",
        "route_id",
        "decision_id",
        "budget_rps",
        "selected_transaction_ids",
        "predicted_delay_ms",
        "predicted_new_ambiguous",
        "decision_reason",
        "expires_at",
    ],
    v1.StatusCheckResult: [
        "transaction_id",
        "route_id",
        "check_id",
        "outcome",
        "authoritative",
        "response_version",
        "latency_ms",
        "evidence_source_id",
    ],
}


@pytest.mark.parametrize("model", list(V1_FIELDS), ids=lambda m: m.__name__)
def test_v1_models_are_unchanged(model):
    assert list(model.model_fields) == V1_FIELDS[model]


def test_v1_enums_are_unchanged():
    assert [e.value for e in v1.EventType] == [
        "payment_initiated",
        "payment_timeout",
        "status_requested",
        "status_resolved",
        "escalated",
    ]
    assert [o.value for o in v1.FinancialOutcome] == ["unknown", "credited", "not_credited"]
    assert [s.value for s in v1.WorkflowState] == [
        "initiated",
        "ambiguous",
        "check_pending",
        "resolved",
        "escalated",
    ]
    assert [o.value for o in v1.StatusOutcome] == ["unknown", "credited", "not_credited"]


def _v1_telemetry_payload() -> dict:
    return {
        "experiment_id": EXP,
        "route_id": ROUTE,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "payment_rps": 10,
        "check_rps": 1,
        "observed_throughput_rps": 9,
        "p95_latency_ms": 100,
        "timeout_rate": 0.1,
        "queue_depth": 2,
        "source_id": "test",
        "sequence": 1,
    }


def test_v1_still_validates_its_own_payloads():
    legacy = v1.RouteTelemetry.model_validate(_v1_telemetry_payload())
    assert legacy.schema_version == "1.0"


def test_v1_payloads_are_not_silently_accepted_as_v1_1():
    # v1 telemetry has no observation window, scope or sample count; v1.1 must not invent them.
    with pytest.raises(ValidationError):
        RouteTelemetry.model_validate({**_v1_telemetry_payload(), "schema_version": "1.0"})
    with pytest.raises(ValidationError):
        RouteTelemetry.model_validate({**_v1_telemetry_payload(), "schema_version": "1.1"})


def test_v1_and_v1_1_are_distinct_types():
    assert v1.RouteTelemetry is not RouteTelemetry
    assert v1.TransactionEvent is not TransactionEvent
    assert v1.FinancialOutcome is not v11.FinancialOutcome
    assert {o.value for o in v1.FinancialOutcome}.isdisjoint(
        {o.value for o in v11.FinancialOutcome}
    )
