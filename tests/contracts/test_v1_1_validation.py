"""Contract v1.1: field and cross-field validation, including every negative case."""

import math
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from test_v1_1_models import EXP, PROBE, ROUTE, build, payload, ts

from contracts.v1_1 import (
    CreditConsumption,
    CreditLease,
    DeadlineRule,
    DecisionRequest,
    ErrorResponse,
    EvidenceSignature,
    FinancialOutcome,
    RecoveryDecision,
    RouteDescriptor,
    RouteTelemetry,
    RouteTwinSnapshot,
    StatusCheckResult,
    TimingPolicy,
    TransactionEvent,
    TransactionProjection,
    ValidatedRouteTelemetry,
)


def evidence(kind: str = "STATUS_CHECK_RESPONSE") -> dict:
    return {"evidence_id": "EV-9", "evidence_kind": kind, "issuer_id": "SW", "response_version": 2}


def event(**overrides) -> TransactionEvent:
    return build(TransactionEvent, **{"state_version": 2, **overrides})


def telemetry(**overrides) -> dict:
    return payload(RouteTelemetry, **overrides)


def validated(**overrides) -> dict:
    return payload(ValidatedRouteTelemetry, **overrides)


# ------------------------------------------------------------------ timestamps


TIMESTAMP_FIELDS = [
    (TransactionEvent, "event_timestamp"),
    (RouteTelemetry, "observation_timestamp"),
    (RecoveryDecision, "created_at"),
    (CreditLease, "issued_at"),
    (DecisionRequest, "requested_at"),
]


@pytest.mark.parametrize(("model", "field"), TIMESTAMP_FIELDS)
@pytest.mark.parametrize("naive", [datetime(2026, 1, 1, 0, 0, 5), "2026-01-01T00:00:05"])
def test_naive_timestamps_are_rejected(model, field, naive):
    with pytest.raises(ValidationError, match="timezone"):
        build(model, **{field: naive})


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 1, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        "2026-01-01T05:30:00+05:30",
        "2026-01-01T00:00:00Z",
        "2025-12-31T19:00:00-05:00",
    ],
)
def test_timestamps_are_normalised_to_utc(value):
    instance = event(event_type="PAYMENT_TIMED_OUT", event_timestamp=value)
    assert instance.event_timestamp == ts(0)
    assert instance.event_timestamp.utcoffset() == timedelta(0)


# ------------------------------------------------------------------ numbers


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (RouteTelemetry, "payment_rps"),
        (RouteTelemetry, "check_rps"),
        (RouteTelemetry, "p50_latency_ms"),
        (RouteTelemetry, "queue_depth"),
        (StatusCheckResult, "latency_ms"),
        (RecoveryDecision, "budget_rps"),
        (CreditLease, "credits_granted"),
        (TimingPolicy, "first_check_delay_s"),
    ],
)
def test_negative_quantities_are_rejected(model, field):
    with pytest.raises(ValidationError, match=field):
        build(model, **{field: -1})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize(
    ("model", "field"),
    [
        (RouteTelemetry, "payment_rps"),
        (RouteTelemetry, "p95_latency_ms"),
        (RouteTelemetry, "timeout_rate"),
        (StatusCheckResult, "latency_ms"),
        (TimingPolicy, "check_window_s"),
    ],
)
def test_nan_and_infinity_are_rejected(model, field, value):
    with pytest.raises(ValidationError, match=field):
        build(model, **{field: value})


def test_nan_and_infinity_are_rejected_in_json_input():
    for literal in ("NaN", "Infinity"):
        raw = RouteTelemetry.model_validate(telemetry()).model_dump_json()
        raw = raw.replace('"payment_rps":700.0', f'"payment_rps":{literal}')
        with pytest.raises(ValidationError, match="payment_rps"):
            RouteTelemetry.model_validate_json(raw)


@pytest.mark.parametrize("value", [True, "700", "seven"])
def test_numbers_are_strict(value):
    with pytest.raises(ValidationError, match="payment_rps"):
        RouteTelemetry.model_validate(telemetry(payment_rps=value))


def test_integer_fields_reject_fractional_values():
    with pytest.raises(ValidationError, match="queue_depth"):
        RouteTelemetry.model_validate(telemetry(queue_depth=1.5))


def test_timeout_rate_is_a_probability():
    with pytest.raises(ValidationError, match="timeout_rate"):
        RouteTelemetry.model_validate(
            telemetry(timeout_rate=1.5, timeout_count=None, timeout_denominator_count=None)
        )


# ------------------------------------------------------------------ telemetry


def test_missing_observations_stay_null():
    sample = RouteTelemetry.model_validate(
        telemetry(
            observed_throughput_rps=None,
            completed_payment_rps=None,
            completed_check_rps=None,
            p50_latency_ms=None,
            p95_latency_ms=None,
            latency_sample_count=0,
            timeout_rate=None,
            timeout_count=None,
            timeout_denominator_count=None,
            queue_depth=None,
        )
    )
    for field in ("observed_throughput_rps", "p95_latency_ms", "timeout_rate", "queue_depth"):
        assert getattr(sample, field) is None


@pytest.mark.parametrize(
    "field", ["observed_throughput_rps", "p50_latency_ms", "p95_latency_ms", "queue_depth"]
)
def test_missing_observation_must_be_explicit_null(field):
    data = telemetry()
    del data[field]
    with pytest.raises(ValidationError, match=field):
        RouteTelemetry.model_validate(data)


def test_measured_zero_is_kept_distinct_from_missing():
    sample = RouteTelemetry.model_validate(
        telemetry(
            payment_rps=0.0,
            check_rps=0.0,
            observed_throughput_rps=0.0,
            completed_payment_rps=0.0,
            completed_check_rps=0.0,
            p50_latency_ms=None,
            p95_latency_ms=None,
            latency_sample_count=0,
            timeout_rate=0.0,
            timeout_count=0,
            timeout_denominator_count=10,
            queue_depth=0,
        )
    )
    assert sample.observed_throughput_rps == 0.0
    assert sample.timeout_rate == 0.0
    assert sample.queue_depth == 0
    assert sample.p95_latency_ms is None


@pytest.mark.parametrize(
    ("start", "end", "emitted", "message"),
    [
        (ts(1), ts(1), ts(1), "later than observation_window_start"),
        (ts(2), ts(1), ts(2), "later than observation_window_start"),
        (ts(0), ts(1), ts(0.5), "cannot precede observation_window_end"),
    ],
)
def test_invalid_observation_window_is_rejected(start, end, emitted, message):
    with pytest.raises(ValidationError, match=message):
        RouteTelemetry.model_validate(
            telemetry(
                observation_window_start=start,
                observation_window_end=end,
                observation_timestamp=emitted,
            )
        )


def test_emission_after_window_end_is_accepted():
    sample = RouteTelemetry.model_validate(telemetry(observation_timestamp=ts(3)))
    assert sample.observation_timestamp == ts(3)


def test_p95_below_p50_is_rejected():
    with pytest.raises(ValidationError, match="p95_latency_ms cannot be below p50_latency_ms"):
        RouteTelemetry.model_validate(telemetry(p50_latency_ms=500.0, p95_latency_ms=100.0))


@pytest.mark.parametrize(("p50", "p95"), [(200.0, 200.0), (None, 300.0), (150.0, None)])
def test_percentiles_may_be_equal_or_partially_present(p50, p95):
    RouteTelemetry.model_validate(telemetry(p50_latency_ms=p50, p95_latency_ms=p95))


@pytest.mark.parametrize(("p50", "p95"), [(0.0, None), (None, 0.0), (0.0, 0.0), (None, 5.0)])
def test_latency_without_samples_is_rejected(p50, p95):
    with pytest.raises(ValidationError, match="latency_sample_count is 0"):
        RouteTelemetry.model_validate(
            telemetry(latency_sample_count=0, p50_latency_ms=p50, p95_latency_ms=p95)
        )


def test_throughput_split_must_sum_to_total():
    with pytest.raises(ValidationError, match="must equal completed_payment_rps"):
        RouteTelemetry.model_validate(telemetry(observed_throughput_rps=800.0))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"timeout_count": 8, "timeout_denominator_count": 7}, "cannot exceed"),
        ({"timeout_rate": 0.5}, "must equal timeout_count"),
        ({"timeout_rate": None}, "must equal timeout_count"),
        ({"timeout_count": 0, "timeout_denominator_count": 0}, "denominator is 0"),
        ({"timeout_denominator_count": None}, "given together"),
    ],
)
def test_timeout_counts_must_match_the_rate(overrides, message):
    with pytest.raises(ValidationError, match=message):
        RouteTelemetry.model_validate(telemetry(**overrides))


def test_validated_envelope_rejects_inconsistent_experiment():
    with pytest.raises(ValidationError, match="EXPERIMENT_MISMATCH"):
        ValidatedRouteTelemetry.model_validate(validated(experiment_id="EXP-2"))


def test_validated_envelope_rejects_inconsistent_route():
    with pytest.raises(ValidationError, match="ROUTE_IDENTITY_MISMATCH"):
        ValidatedRouteTelemetry.model_validate(validated(route_id="A1-C"))


def test_validation_cannot_precede_observation():
    with pytest.raises(ValidationError, match="validated_at cannot precede"):
        ValidatedRouteTelemetry.model_validate(validated(validated_at=ts(0.5)))


@pytest.mark.parametrize(
    ("flags", "status"),
    [(["CROSS_CHECK_DEVIATION"], "CONSISTENT"), ([], "INCONSISTENT")],
)
def test_cross_check_flag_and_status_must_agree(flags, status):
    with pytest.raises(ValidationError, match="CROSS_CHECK_DEVIATION"):
        ValidatedRouteTelemetry.model_validate(
            validated(validation_flags=flags, cross_check_status=status)
        )


def test_duplicate_validation_flags_are_rejected():
    with pytest.raises(ValidationError, match="duplicates"):
        ValidatedRouteTelemetry.model_validate(
            validated(validation_flags=["SEQUENCE_GAP", "SEQUENCE_GAP"])
        )


def test_untrusted_telemetry_is_representable_so_the_consumer_can_reject_it():
    envelope = ValidatedRouteTelemetry.model_validate(validated(trust_status="UNAUTHENTICATED"))
    assert envelope.trust_status == "UNAUTHENTICATED"


# ------------------------------------------------------------------ route twin snapshot


def snapshot(**overrides) -> RouteTwinSnapshot:
    return build(RouteTwinSnapshot, **overrides)


def flagged_envelope(sequence: int = 2, emitted: float = 2.0) -> dict:
    sample = telemetry(
        sequence=sequence,
        observation_window_start=ts(emitted - 1),
        observation_window_end=ts(emitted),
        observation_timestamp=ts(emitted),
    )
    return validated(
        telemetry=sample,
        validated_at=ts(emitted + 0.1),
        validation_flags=["STATISTICAL_ANOMALY"],
    )


def test_no_data_snapshot_is_valid_and_has_no_estimates():
    empty = snapshot(
        freshness_status="NO_DATA",
        last_observed_at=None,
        latest_sequence_per_source=[],
        latest_trusted_observation=None,
    )
    assert empty.estimated_capacity_rps is None
    assert empty.capacity_lower_bound_rps is None


def test_flagged_observation_is_kept_apart_from_the_trusted_one():
    snap = snapshot(
        latest_sequence_per_source=[{"source_id": PROBE, "sequence": 2}],
        latest_flagged_observation=flagged_envelope(),
        last_observed_at=ts(2),
    )
    assert snap.latest_trusted_observation.sequence == 1
    assert snap.latest_flagged_observation.telemetry.sequence == 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"freshness_status": "NO_DATA"}, "NO_DATA snapshots cannot contain observations"),
        (
            {"latest_trusted_observation": None, "last_observed_at": None},
            "FRESH snapshots require an observation",
        ),
        ({"last_observed_at": ts(5)}, "last_observed_at must equal"),
        ({"latest_sequence_per_source": []}, "beyond latest_sequence_per_source"),
        (
            {
                "latest_sequence_per_source": [
                    {"source_id": PROBE, "sequence": 1},
                    {"source_id": PROBE, "sequence": 3},
                ]
            },
            "more than once",
        ),
        ({"estimated_capacity_rps": 650.0}, "estimator_id is required"),
        ({"estimator_id": "ewma-v1"}, "estimator_id is required"),
        (
            {
                "estimated_capacity_rps": 600.0,
                "capacity_lower_bound_rps": 640.0,
                "estimator_id": "ewma-v1",
            },
            "cannot exceed estimated_capacity_rps",
        ),
    ],
)
def test_invalid_snapshots_are_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        snapshot(**overrides)


def test_snapshot_rejects_observation_from_another_experiment():
    foreign = telemetry(experiment_id="EXP-2")
    with pytest.raises(ValidationError, match="EXPERIMENT_MISMATCH"):
        snapshot(latest_trusted_observation=foreign)


def test_flagged_slot_requires_authenticated_and_flagged_telemetry():
    unflagged = flagged_envelope()
    unflagged["validation_flags"] = []
    with pytest.raises(ValidationError, match="at least one flag"):
        snapshot(
            latest_sequence_per_source=[{"source_id": PROBE, "sequence": 2}],
            latest_flagged_observation=unflagged,
            last_observed_at=ts(2),
        )
    untrusted = flagged_envelope()
    untrusted["trust_status"] = "UNAUTHENTICATED"
    with pytest.raises(ValidationError, match="AUTHENTICATED"):
        snapshot(
            latest_sequence_per_source=[{"source_id": PROBE, "sequence": 2}],
            latest_flagged_observation=untrusted,
            last_observed_at=ts(2),
        )


def test_estimates_are_accepted_with_an_estimator():
    snap = snapshot(
        estimated_capacity_rps=650.0, capacity_lower_bound_rps=640.0, estimator_id="ewma-v1"
    )
    assert snap.capacity_lower_bound_rps == 640.0


# ------------------------------------------------------------------ transaction events


VALID_EVENTS = {
    "payment-timed-out": {"event_type": "PAYMENT_TIMED_OUT"},
    "timed-out-with-deadline": {
        "event_type": "PAYMENT_TIMED_OUT",
        "deadline_at": ts(3600),
        "deadline_policy_id": "research-scaled-60min",
    },
    "check-dispatched": {"event_type": "STATUS_CHECK_DISPATCHED", "check_id": "CHK-1"},
    "pending-answer": {
        "event_type": "STATUS_CHECK_ANSWERED",
        "check_id": "CHK-1",
        "check_result": "PENDING",
        "financial_outcome": "PENDING",
        "evidence_reference": evidence(),
        "counted_check_attempts": 1,
    },
    "unknown-answer": {
        "event_type": "STATUS_CHECK_ANSWERED",
        "check_id": "CHK-1",
        "check_result": "UNKNOWN",
        "counted_check_attempts": 1,
    },
    "confirmed-success": {
        "event_type": "STATUS_CHECK_ANSWERED",
        "check_id": "CHK-1",
        "check_result": "AUTHORITATIVE_SUCCESS",
        "ledger_state": "CONFIRMED_SUCCESS",
        "financial_outcome": "SUCCESS",
        "evidence_reference": evidence(),
        "counted_check_attempts": 1,
    },
    "confirmed-failure": {
        "event_type": "STATUS_CHECK_ANSWERED",
        "check_id": "CHK-1",
        "check_result": "AUTHORITATIVE_FAILURE",
        "ledger_state": "CONFIRMED_FAILED",
        "financial_outcome": "FAILURE",
        "evidence_reference": evidence(),
        "counted_check_attempts": 1,
    },
    "reversal-completed": {
        "event_type": "REVERSAL_COMPLETED",
        "ledger_state": "REVERSED",
        "financial_outcome": "FAILURE",
        "evidence_reference": evidence(),
    },
    "check-timed-out": {
        "event_type": "STATUS_CHECK_TIMED_OUT",
        "check_id": "CHK-1",
        "result_code": "CHECK_TIMEOUT",
        "counted_check_attempts": 1,
    },
    "credit-exhausted": {
        "event_type": "STATUS_CHECK_REJECTED",
        "check_id": "CHK-2",
        "result_code": "CREDIT_EXHAUSTED",
    },
    "reconciliation-handoff": {
        "event_type": "RECONCILIATION_HANDOFF",
        "ledger_state": "RECONCILIATION_PENDING",
        "counted_check_attempts": 3,
    },
    "reconciliation-resolved": {
        "event_type": "RECONCILIATION_RESOLVED",
        "ledger_state": "CONFIRMED_SUCCESS",
        "financial_outcome": "SUCCESS",
        "evidence_reference": evidence("RECONCILIATION_RECORD"),
    },
    "late-original-response": {
        "event_type": "PAYMENT_RESPONSE_RECEIVED",
        "ledger_state": "CONFIRMED_SUCCESS",
        "financial_outcome": "SUCCESS",
        "evidence_reference": evidence("ORIGINAL_PAYMENT_RESPONSE"),
    },
    "evidence-conflict-recorded": {
        "event_type": "EVIDENCE_CONFLICT_RECORDED",
        "ledger_state": "CONFIRMED_SUCCESS",
        "financial_outcome": "SUCCESS",
        "evidence_reference": evidence(),
    },
    "transaction-closed": {
        "event_type": "TRANSACTION_CLOSED",
        "ledger_state": "CLOSED",
        "financial_outcome": "SUCCESS",
        "evidence_reference": evidence(),
    },
}


@pytest.mark.parametrize("name", list(VALID_EVENTS))
def test_valid_event_scenarios(name):
    instance = event(**VALID_EVENTS[name])
    assert instance.event_type == VALID_EVENTS[name]["event_type"]


def test_a_timeout_leaves_the_outcome_unknown():
    timed_out = event(event_type="PAYMENT_TIMED_OUT")
    assert timed_out.financial_outcome is FinancialOutcome.UNKNOWN
    assert timed_out.ledger_state == "DEBITED"


INVALID_EVENTS = {
    "payment-timeout-claims-failure": (
        {
            "event_type": "PAYMENT_TIMED_OUT",
            "ledger_state": "CONFIRMED_FAILED",
            "financial_outcome": "FAILURE",
            "evidence_reference": evidence(),
        },
        "TIMEOUT_CANNOT_IMPLY_OUTCOME",
    ),
    "payment-timeout-claims-pending": (
        {
            "event_type": "PAYMENT_TIMED_OUT",
            "financial_outcome": "PENDING",
            "evidence_reference": evidence(),
        },
        "TIMEOUT_CANNOT_IMPLY_OUTCOME",
    ),
    "check-timeout-claims-success": (
        {
            "event_type": "STATUS_CHECK_TIMED_OUT",
            "check_id": "CHK-1",
            "result_code": "CHECK_TIMEOUT",
            "ledger_state": "CONFIRMED_SUCCESS",
            "financial_outcome": "SUCCESS",
            "evidence_reference": evidence(),
        },
        "TIMEOUT_CANNOT_IMPLY_OUTCOME",
    ),
    "success-without-evidence": (
        {
            "event_type": "STATUS_CHECK_ANSWERED",
            "check_id": "CHK-1",
            "check_result": "AUTHORITATIVE_SUCCESS",
            "ledger_state": "CONFIRMED_SUCCESS",
            "financial_outcome": "SUCCESS",
        },
        "EVIDENCE_REQUIRED",
    ),
    "pending-without-evidence": (
        {
            "event_type": "STATUS_CHECK_ANSWERED",
            "check_id": "CHK-1",
            "check_result": "PENDING",
            "financial_outcome": "PENDING",
        },
        "EVIDENCE_REQUIRED",
    ),
    "reversal-without-evidence": (
        {
            "event_type": "REVERSAL_COMPLETED",
            "ledger_state": "REVERSED",
            "financial_outcome": "FAILURE",
        },
        "EVIDENCE_REQUIRED",
    ),
    "success-while-ledger-debited": (
        {
            "event_type": "EVIDENCE_CONFLICT_RECORDED",
            "financial_outcome": "SUCCESS",
            "evidence_reference": evidence(),
        },
        "inconsistent with ledger state",
    ),
    "reversed-with-success": (
        {
            "event_type": "REVERSAL_COMPLETED",
            "ledger_state": "REVERSED",
            "financial_outcome": "SUCCESS",
            "evidence_reference": evidence(),
        },
        "inconsistent with ledger state",
    ),
    "check-result-contradicts-outcome": (
        {
            "event_type": "STATUS_CHECK_ANSWERED",
            "check_id": "CHK-1",
            "check_result": "AUTHORITATIVE_FAILURE",
            "ledger_state": "CONFIRMED_SUCCESS",
            "financial_outcome": "SUCCESS",
            "evidence_reference": evidence(),
        },
        "inconsistent with outcome",
    ),
    "check-event-without-check-id": (
        {"event_type": "STATUS_CHECK_DISPATCHED"},
        "check_id is required",
    ),
    "payment-event-with-check-id": (
        {"event_type": "PAYMENT_TIMED_OUT", "check_id": "CHK-1"},
        "check_id is required",
    ),
    "check-result-on-dispatch": (
        {"event_type": "STATUS_CHECK_DISPATCHED", "check_id": "CHK-1", "check_result": "UNKNOWN"},
        "check_result is required",
    ),
    "rejection-without-code": (
        {"event_type": "STATUS_CHECK_REJECTED", "check_id": "CHK-1"},
        "result_code is required",
    ),
    "rejection-with-timeout-code": (
        {
            "event_type": "STATUS_CHECK_REJECTED",
            "check_id": "CHK-1",
            "result_code": "CHECK_TIMEOUT",
        },
        "pre-dispatch",
    ),
    "timeout-with-credit-code": (
        {
            "event_type": "STATUS_CHECK_TIMED_OUT",
            "check_id": "CHK-1",
            "result_code": "CREDIT_EXHAUSTED",
        },
        "STATUS_CHECK_TIMED_OUT requires result_code",
    ),
    "initiated-at-version-2": (
        {"event_type": "PAYMENT_INITIATED"},
        "state_version 1",
    ),
    "answer-with-wrong-evidence-kind": (
        {
            "event_type": "STATUS_CHECK_ANSWERED",
            "check_id": "CHK-1",
            "check_result": "PENDING",
            "financial_outcome": "PENDING",
            "evidence_reference": evidence("ORIGINAL_PAYMENT_RESPONSE"),
        },
        "evidence of kind",
    ),
    "handoff-while-debited": (
        {"event_type": "RECONCILIATION_HANDOFF"},
        "requires ledger_state",
    ),
    "deadline-without-policy": (
        {"event_type": "PAYMENT_TIMED_OUT", "deadline_at": ts(3600)},
        "deadline_at and deadline_policy_id",
    ),
    "same-bank-both-ends": (
        {"event_type": "PAYMENT_TIMED_OUT", "beneficiary_bank_id": "A1"},
        "must differ",
    ),
    "zero-amount": ({"event_type": "PAYMENT_TIMED_OUT", "amount_minor": 0}, "amount_minor"),
    "lowercase-currency": ({"event_type": "PAYMENT_TIMED_OUT", "currency": "inr"}, "currency"),
    "negative-counted-attempts": (
        {"event_type": "PAYMENT_TIMED_OUT", "counted_check_attempts": -1},
        "counted_check_attempts",
    ),
    "zero-state-version": (
        {"event_type": "PAYMENT_TIMED_OUT", "state_version": 0},
        "state_version",
    ),
}


@pytest.mark.parametrize("name", list(INVALID_EVENTS))
def test_invalid_event_scenarios(name):
    overrides, message = INVALID_EVENTS[name]
    with pytest.raises(ValidationError, match=message):
        event(**overrides)


def test_route_descriptor_rejects_identical_endpoints():
    with pytest.raises(ValidationError, match="must differ"):
        build(RouteDescriptor, beneficiary_bank_id="A1")


# ------------------------------------------------------------------ transaction projection


def projection(**overrides) -> TransactionProjection:
    return build(TransactionProjection, **overrides)


CLOSED_SUCCESS = {
    "workflow_state": "CLOSED",
    "terminal": True,
    "financial_outcome": "SUCCESS",
    "ledger_state_observed": "CONFIRMED_SUCCESS",
    "last_evidence_reference": evidence(),
    "last_authoritative_evidence_at": ts(200),
    "next_eligible_at": None,
}


@pytest.mark.parametrize(
    "overrides",
    [
        CLOSED_SUCCESS,
        {"workflow_state": "CHECK_IN_FLIGHT", "check_in_flight_id": "CHK-1"},
        {"workflow_state": "ESCALATED", "deadline_escalated": True},
        {"workflow_state": "QUARANTINED", "quarantine_reason": "EVIDENCE_CONFLICT"},
        {"workflow_state": "ACTIVE", "ambiguity_created_at": None},
        {"last_check_at": ts(91), "last_check_id": "CHK-1", "checks_used": 1},
    ],
    ids=["closed", "in-flight", "escalated", "quarantined", "active", "after-check"],
)
def test_valid_projections(overrides):
    projection(**overrides)


INVALID_PROJECTIONS = {
    "closed-but-not-terminal": ({**CLOSED_SUCCESS, "terminal": False}, "terminal must be true"),
    "terminal-but-ambiguous": ({"terminal": True}, "terminal must be true"),
    "closed-with-unknown-outcome": (
        {"workflow_state": "CLOSED", "terminal": True, "next_eligible_at": None},
        "SUCCESS or FAILURE",
    ),
    "closed-still-eligible": ({**CLOSED_SUCCESS, "next_eligible_at": ts(300)}, "next_eligible_at"),
    "success-without-evidence": (
        {**CLOSED_SUCCESS, "last_evidence_reference": None},
        "EVIDENCE_REQUIRED",
    ),
    "success-without-evidence-time": (
        {**CLOSED_SUCCESS, "last_authoritative_evidence_at": None},
        "last_authoritative_evidence_at",
    ),
    "outcome-contradicts-ledger": (
        {"financial_outcome": "FAILURE", "last_evidence_reference": evidence()},
        "inconsistent with ledger state",
    ),
    "quarantined-without-reason": ({"workflow_state": "QUARANTINED"}, "quarantine_reason"),
    "reason-without-quarantine": ({"quarantine_reason": "EVIDENCE_CONFLICT"}, "quarantine_reason"),
    "in-flight-without-check": ({"workflow_state": "CHECK_IN_FLIGHT"}, "check_in_flight_id"),
    "escalated-without-flag": ({"workflow_state": "ESCALATED"}, "deadline_escalated"),
    "active-but-ambiguous": ({"workflow_state": "ACTIVE"}, "ACTIVE projection"),
    "ambiguous-without-time": ({"ambiguity_created_at": None}, "requires ambiguity_created_at"),
    "ambiguity-before-initiation": ({"initiated_at": ts(20)}, "cannot precede initiated_at"),
    "check-time-without-id": ({"last_check_at": ts(91)}, "last_check_at and last_check_id"),
    "negative-checks-used": ({"checks_used": -1}, "checks_used"),
    "deadline-without-policy": ({"deadline_policy_id": None}, "deadline_at and deadline_policy_id"),
}


@pytest.mark.parametrize("name", list(INVALID_PROJECTIONS))
def test_invalid_projections(name):
    overrides, message = INVALID_PROJECTIONS[name]
    with pytest.raises(ValidationError, match=message):
        projection(**overrides)


# ------------------------------------------------------------------ decisions


def decision(**overrides) -> RecoveryDecision:
    return build(RecoveryDecision, **overrides)


FAIL_CLOSED = {
    "decision_mode": "FAIL_CLOSED",
    "budget_rps": 0,
    "selected_transaction_ids": [],
    "state_versions": [],
    "predicted_delay_ms": None,
    "predicted_new_ambiguous": None,
    "prediction_horizon_s": None,
}


@pytest.mark.parametrize(
    "reason",
    ["NO_CAPACITY_ESTIMATE", "TELEMETRY_STALE", "TELEMETRY_UNTRUSTED", "BOOTSTRAP_FAIL_CLOSED"],
)
def test_fail_closed_decisions_have_zero_budget(reason):
    instance = decision(**FAIL_CLOSED, reason_code=reason)
    assert instance.budget_rps == 0
    assert instance.selected_transaction_ids == ()


def test_warming_up_decision_is_valid_with_zero_budget():
    decision(**{**FAIL_CLOSED, "decision_mode": "WARMING_UP"}, reason_code="CONTROLLER_WARMING_UP")


@pytest.mark.parametrize("expires", [ts(100), ts(99)])
def test_decision_must_expire_after_creation(expires):
    with pytest.raises(ValidationError, match="expires_at must be later than created_at"):
        decision(expires_at=expires)


def test_duplicate_selected_transactions_are_rejected():
    with pytest.raises(ValidationError, match="contains duplicates"):
        decision(
            selected_transaction_ids=["T-1", "T-1"],
            state_versions=[
                {"transaction_id": "T-1", "version": 2},
                {"transaction_id": "T-1", "version": 2},
            ],
        )


@pytest.mark.parametrize(
    "versions",
    [
        [{"transaction_id": "T-1", "version": 2}],
        [
            {"transaction_id": "T-2", "version": 3},
            {"transaction_id": "T-1", "version": 2},
        ],
        [
            {"transaction_id": "T-1", "version": 2},
            {"transaction_id": "T-2", "version": 3},
            {"transaction_id": "T-3", "version": 1},
        ],
    ],
    ids=["missing-version", "wrong-order", "extra-version"],
)
def test_state_versions_must_match_the_selection(versions):
    with pytest.raises(ValidationError, match="state_versions must list exactly"):
        decision(state_versions=versions)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({**FAIL_CLOSED, "budget_rps": 5, "reason_code": "TELEMETRY_STALE"}, "zero budget"),
        ({"decision_mode": "FAIL_CLOSED"}, "is not valid for FAIL_CLOSED"),
        ({"reason_code": "TELEMETRY_STALE"}, "is not valid for NORMAL"),
        ({"reason_code": "NO_ELIGIBLE_TRANSACTIONS"}, "cannot select transactions"),
        ({"budget_rps": 1}, "exceeds budget_rps"),
        ({"prediction_horizon_s": None}, "prediction_horizon_s"),
        (
            {
                "telemetry_watermark": [
                    {"source_id": PROBE, "sequence": 1},
                    {"source_id": PROBE, "sequence": 2},
                ]
            },
            "more than once",
        ),
    ],
)
def test_invalid_decisions_are_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        decision(**overrides)


def test_decision_request_rejects_duplicate_watermark_sources():
    with pytest.raises(ValidationError, match="more than once"):
        build(
            DecisionRequest,
            min_telemetry_watermark=[
                {"source_id": PROBE, "sequence": 1},
                {"source_id": PROBE, "sequence": 1},
            ],
        )


# ------------------------------------------------------------------ status checks and errors


def result(**overrides) -> StatusCheckResult:
    return build(StatusCheckResult, **overrides)


def test_unknown_answer_needs_no_evidence_and_is_not_authoritative():
    unknown = result(
        check_result="UNKNOWN",
        evidence_reference=None,
        evidence_signature=None,
        evidence_verified=False,
    )
    assert unknown.is_authoritative is False


def test_authoritative_only_when_evidence_is_verified():
    assert result().is_authoritative is True
    assert result(evidence_verified=False).is_authoritative is False
    assert result(check_result="PENDING").is_authoritative is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"evidence_reference": None, "evidence_signature": None, "evidence_verified": False},
            "EVIDENCE_REQUIRED",
        ),
        (
            {
                "check_result": "PENDING",
                "evidence_reference": None,
                "evidence_signature": None,
                "evidence_verified": False,
            },
            "EVIDENCE_REQUIRED",
        ),
        (
            {"check_result": "UNKNOWN", "evidence_signature": None, "evidence_verified": False},
            "given together",
        ),
        (
            {
                "check_result": "UNKNOWN",
                "evidence_reference": None,
                "evidence_signature": None,
                "evidence_verified": True,
            },
            "evidence_verified requires evidence",
        ),
        ({"evidence_reference": evidence("RECONCILIATION_RECORD")}, "STATUS_CHECK_RESPONSE"),
        ({"counts_against_limit": False}, "counts against the limit"),
        ({"counted_check_attempts": 0}, "counted_check_attempts"),
    ],
)
def test_invalid_status_check_results(overrides, message):
    with pytest.raises(ValidationError, match=message):
        result(**overrides)


@pytest.mark.parametrize(
    "fields", [[], ["transaction_id", "transaction_id"]], ids=["empty", "duplicate"]
)
def test_signed_fields_must_be_non_empty_and_unique(fields):
    with pytest.raises(ValidationError, match="signed_fields"):
        build(EvidenceSignature, signed_fields=fields)


@pytest.mark.parametrize(
    ("code", "counts", "valid"),
    [
        ("CREDIT_EXHAUSTED", False, True),
        ("CREDIT_EXHAUSTED", True, False),
        ("RATE_LIMITED", None, False),
        ("CHECK_TIMEOUT", True, True),
        ("CHECK_TIMEOUT", False, False),
        ("UPSTREAM_UNAVAILABLE", True, True),
        ("UPSTREAM_UNAVAILABLE", False, True),
        ("UPSTREAM_UNAVAILABLE", None, False),
        ("DUPLICATE_EVENT", None, True),
        ("DUPLICATE_EVENT", False, False),
    ],
)
def test_error_response_follows_check_counting_rules(code, counts, valid):
    data = payload(ErrorResponse, error_code=code, counts_against_limit=counts)
    if valid:
        ErrorResponse.model_validate(data)
    else:
        with pytest.raises(ValidationError, match="counts_against_limit|counting"):
            ErrorResponse.model_validate(data)


def test_retry_after_requires_a_retryable_error():
    with pytest.raises(ValidationError, match="retry_after_ms"):
        build(ErrorResponse, retryable=False)


# ------------------------------------------------------------------ experimental credits


@pytest.mark.parametrize("expires", [ts(100), ts(99)])
def test_lease_must_expire_after_issue(expires):
    with pytest.raises(ValidationError, match="expires_at must be later than issued_at"):
        build(CreditLease, expires_at=expires)


def test_credit_overconsumption_is_rejected():
    with pytest.raises(ValidationError, match="cannot exceed credits_granted"):
        build(CreditLease, credits_consumed=28)


def test_fully_consumed_lease_is_valid():
    assert build(CreditLease, credits_consumed=27).credits_consumed == 27


@pytest.mark.parametrize("regime", ["NPCI_PRODUCTION", "TIER1_COMPLIANT", ""])
def test_lease_must_declare_the_experimental_mock_switch_regime(regime):
    with pytest.raises(ValidationError, match="regime"):
        build(CreditLease, regime=regime)


def test_lease_regime_is_required():
    data = payload(CreditLease)
    del data["regime"]
    with pytest.raises(ValidationError, match="regime"):
        CreditLease.model_validate(data)


@pytest.mark.parametrize(
    ("accepted", "code", "valid"),
    [
        (True, None, True),
        (False, "CREDIT_EXHAUSTED", True),
        (False, "LEASE_EXPIRED", True),
        (True, "CREDIT_EXHAUSTED", False),
        (False, None, False),
        (False, "RATE_LIMITED", False),
    ],
)
def test_credit_consumption_outcomes(accepted, code, valid):
    data = payload(CreditConsumption, accepted=accepted, error_code=code)
    if valid:
        CreditConsumption.model_validate(data)
    else:
        with pytest.raises(ValidationError, match="error_code"):
            CreditConsumption.model_validate(data)


# ------------------------------------------------------------------ timing policy


def policy(**overrides) -> TimingPolicy:
    return build(TimingPolicy, **overrides)


@pytest.mark.parametrize("status", ["RESEARCH_PARAMETER", "SOURCE_CLAIMED_UNVERIFIED"])
def test_tier1_label_requires_verified_limits(status):
    with pytest.raises(ValidationError, match="TIER1_COMPLIANT requires verification_status"):
        policy(regime="TIER1_COMPLIANT", verification_status=status)


def test_verified_tier1_policy_is_accepted():
    assert policy(regime="TIER1_COMPLIANT", verification_status="VERIFIED").regime == (
        "TIER1_COMPLIANT"
    )


@pytest.mark.parametrize("regime", ["TIER3", "tier1_compliant", "", "COMPLIANT"])
def test_unknown_timing_regime_is_rejected(regime):
    with pytest.raises(ValidationError, match="regime"):
        policy(regime=regime)


@pytest.mark.parametrize(
    ("regime", "status"),
    [("RESEARCH_SCALED", "RESEARCH_PARAMETER"), ("TIER1_COMPLIANT", "VERIFIED")],
)
def test_experimental_extra_checks_cannot_be_enabled_outside_tier2(regime, status):
    with pytest.raises(ValidationError, match="only allowed in TIER2_EXPERIMENTAL"):
        policy(regime=regime, verification_status=status, escalation_extra_checks=1)


def test_tier2_policy_may_grant_extra_checks():
    tier2 = policy(regime="TIER2_EXPERIMENTAL", escalation_extra_checks=1, first_check_delay_s=30.0)
    assert tier2.escalation_extra_checks == 1


def test_tier2_limits_cannot_be_labelled_verified():
    with pytest.raises(ValidationError, match="cannot be VERIFIED"):
        policy(regime="TIER2_EXPERIMENTAL", verification_status="VERIFIED")


def test_regulatory_calendar_deadline_requires_verification():
    calendar = {"kind": "REGULATORY_CALENDAR", "calendar_id": "rbi-tat-upi"}
    with pytest.raises(ValidationError, match="requires a VERIFIED policy"):
        policy(deadline_rule=calendar)
    accepted = policy(
        regime="TIER1_COMPLIANT", verification_status="VERIFIED", deadline_rule=calendar
    )
    assert accepted.deadline_rule.calendar_id == "rbi-tat-upi"


@pytest.mark.parametrize(
    "rule",
    [
        {"kind": "FIXED_DURATION"},
        {"kind": "FIXED_DURATION", "duration_s": 3600.0, "calendar_id": "x"},
        {"kind": "REGULATORY_CALENDAR"},
        {"kind": "REGULATORY_CALENDAR", "calendar_id": "x", "duration_s": 60.0},
        {"kind": "FIXED_DURATION", "duration_s": 0.0},
    ],
)
def test_invalid_deadline_rules(rule):
    with pytest.raises(ValidationError):
        DeadlineRule.model_validate(rule)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"stop_error_codes": ["U16", "U16"]}, "duplicates"),
        ({"check_window_s": 100.0}, "cannot fit within check_window_s"),
        ({"first_check_reference": "SETTLED"}, "first_check_reference"),
        ({"client_timeout_s": 0.0}, "client_timeout_s"),
        ({"check_timeout_s": -1.0}, "check_timeout_s"),
    ],
)
def test_invalid_timing_policies(overrides, message):
    with pytest.raises(ValidationError, match=message):
        policy(**overrides)


def test_no_check_control_policy_is_expressible():
    assert policy(max_counted_checks=0).max_counted_checks == 0


@pytest.mark.parametrize("reference", ["INITIATED", "TIMED_OUT"])
def test_first_check_reference_is_explicit_configuration(reference):
    assert policy(first_check_reference=reference).first_check_reference == reference


def test_inconsistent_experiment_identifiers_are_detected_across_models():
    # Every message carries its own experiment_id; nested copies must agree.
    with pytest.raises(ValidationError, match="EXPERIMENT_MISMATCH"):
        ValidatedRouteTelemetry.model_validate(
            validated(experiment_id=EXP, telemetry=telemetry(experiment_id="EXP-OTHER"))
        )
    with pytest.raises(ValidationError, match="ROUTE_IDENTITY_MISMATCH"):
        snapshot(route_id="A1-C", latest_trusted_observation=telemetry(route_id=ROUTE))
