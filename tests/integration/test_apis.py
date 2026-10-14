from fastapi.testclient import TestClient

from backend.remitter.api import app as remitter_app
from backend.switch.api import app as switch_app
from intelligence.controller.api import app as controller_app


def test_health_endpoints() -> None:
    for app in (remitter_app, switch_app, controller_app):
        assert TestClient(app).get("/health").status_code == 200


def test_payment_is_accepted_but_outcome_remains_unknown() -> None:
    response = TestClient(remitter_app).post(
        "/v1/payments",
        json={"bank_id": "bank-1", "route_id": "route-1", "amount_minor": 500, "currency": "INR"},
    )
    assert response.status_code == 202
    assert response.json()["financial_outcome"] == "unknown"


def test_switch_without_evidence_returns_non_authoritative_unknown() -> None:
    response = TestClient(switch_app).post(
        "/v1/status-checks",
        headers={"x-bank-id": "integration-bank"},
        json={"transaction_id": "txn-1", "route_id": "route-1", "check_id": "check-1"},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "unknown"
    assert response.json()["authoritative"] is False
