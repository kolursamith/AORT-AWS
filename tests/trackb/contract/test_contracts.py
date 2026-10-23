"""Contract tests: frozen fixtures must keep validating against the schema-1.0 models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.aorts.banking.ledger.invariants import PROJECTION, TRANSITIONS
from backend.aorts.contracts.enums import SCHEMA_VERSION, TxnState
from backend.aorts.contracts.events import DomainEvent
from backend.aorts.contracts.requests import PaymentRequest, RouteTelemetry, TransactionStateEvent

FIX = Path(__file__).resolve().parents[3] / "backend" / "aorts" / "contracts" / "fixtures"


@pytest.mark.parametrize(
    "name", ["ambiguous_transaction", "successful_transaction", "failed_transaction"]
)
def test_domain_event_fixtures(name):
    ev = DomainEvent.model_validate(json.loads((FIX / f"{name}.json").read_text()))
    assert ev.schema_version == SCHEMA_VERSION and isinstance(ev.amount_paise, int)


def test_duplicate_response_fixture_is_a_valid_state_event():
    ev = TransactionStateEvent.model_validate(
        json.loads((FIX / "duplicate_response.json").read_text())
    )
    assert ev.event_type == "STATUS_RESPONSE" and ev.payload["status"] == "FAILED"


def test_telemetry_fixture():
    t = RouteTelemetry.model_validate(json.loads((FIX / "telemetry_event.json").read_text()))
    assert t.timestamp.tzinfo is not None


def test_float_amounts_and_unknown_fields_rejected():
    base = {
        "sender_account_id": "A1-CUST-0001",
        "beneficiary_account_id": "B-CUST-0001",
        "beneficiary_bank_id": "B",
        "amount_paise": 100,
        "idempotency_key": "idem-12345678",
    }
    PaymentRequest.model_validate(base)
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate({**base, "amount_paise": 100.0})
    with pytest.raises(ValidationError):
        PaymentRequest.model_validate({**base, "unexpected": 1})


def test_state_machine_is_complete():
    assert set(TRANSITIONS) == set(TxnState) == set(PROJECTION)
