"""Contract v1.1: JSON and Python round-trips, wire formatting and JSON Schema export."""

import json

import pytest
from test_v1_1_models import ALL_MODELS, build

from contracts.v1_1 import (
    RecoveryDecision,
    RouteTelemetry,
    StatusCheckResult,
    TransactionEvent,
)


def _ids(models):
    return [m.__name__ for m in models]


@pytest.mark.parametrize("model", ALL_MODELS, ids=_ids(ALL_MODELS))
def test_json_round_trip_preserves_every_model(model):
    original = build(model)
    restored = model.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.model_dump_json() == original.model_dump_json()


@pytest.mark.parametrize("model", ALL_MODELS, ids=_ids(ALL_MODELS))
def test_python_round_trip_preserves_every_model(model):
    original = build(model)
    assert model.model_validate(original.model_dump()) == original


@pytest.mark.parametrize("model", ALL_MODELS, ids=_ids(ALL_MODELS))
def test_json_schema_is_closed(model):
    schema = model.model_json_schema()
    assert schema["additionalProperties"] is False
    if "schema_version" in model.model_fields:
        assert schema["properties"]["schema_version"]["const"] == "1.1"
        assert "schema_version" in schema["required"]


def test_timestamps_serialise_in_utc():
    sample = build(TransactionEvent, event_timestamp="2026-01-01T05:30:00+05:30")
    wire = json.loads(sample.model_dump_json())
    assert wire["event_timestamp"] == "2026-01-01T00:00:00Z"


def test_enums_serialise_as_plain_strings():
    wire = json.loads(build(TransactionEvent).model_dump_json())
    assert wire["event_type"] == "PAYMENT_INITIATED"
    assert wire["ledger_state"] == "DEBITED"
    assert wire["financial_outcome"] == "UNKNOWN"


def test_sequences_serialise_as_json_arrays_and_restore_as_tuples():
    decision = build(RecoveryDecision)
    wire = json.loads(decision.model_dump_json())
    assert wire["selected_transaction_ids"] == ["T-1", "T-2"]
    restored = RecoveryDecision.model_validate_json(decision.model_dump_json())
    assert restored.selected_transaction_ids == ("T-1", "T-2")


def test_missing_measurements_serialise_as_null_not_zero():
    sample = build(
        RouteTelemetry,
        p50_latency_ms=None,
        p95_latency_ms=None,
        latency_sample_count=0,
        queue_depth=None,
    )
    wire = json.loads(sample.model_dump_json())
    assert wire["p95_latency_ms"] is None
    assert wire["queue_depth"] is None
    assert wire["latency_sample_count"] == 0


def test_derived_properties_are_not_serialised():
    wire = json.loads(build(StatusCheckResult).model_dump_json())
    assert "is_authoritative" not in wire
    # Serialised output must remain valid input under extra="forbid".
    StatusCheckResult.model_validate(wire)


def test_defaults_are_explicit_on_the_wire():
    wire = json.loads(build(TransactionEvent).model_dump_json())
    assert wire["currency"] == "INR"
    assert wire["evidence_reference"] is None
