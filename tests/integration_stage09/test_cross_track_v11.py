"""Stage 09 cross-track evidence (integration branch only; not DB-backed).

Real Track B code (``TransactionView``, the legacy ``HttpController``, the additive
``V11ControllerClient``) talks to the real Track A controller app in process. Track B rows are
constructed ``TransactionView`` objects, not PostgreSQL rows, so nothing here proves ledger
behaviour; the DB-backed Track B suite needs a PostgreSQL server that was not available.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.aorts.adapters.controller_client import HttpController
from backend.aorts.adapters.controller_client_v11 import V11ControllerClient
from backend.aorts.contracts.requests import RecoveryDecisionRequest
from backend.aorts.contracts.responses import TransactionView
from contracts.v1_1 import SourceSequenceMark
from contracts.v1_1.adapters import Incompatible, adapt_outbox_event, adapt_validated_telemetry
from contracts.v1_1.protocol import ControllerOutcome
from intelligence.controller.api import create_app
from tests.intelligence.test_controller_service import (
    EXP,
    POLICY,
    TOKEN,
    Harness,
    ambiguous_events,
    probe_history,
    status_result,
)
from tests.intelligence.test_transaction_twin import make_policy

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "backend/aorts/contracts/fixtures"
CONTROLLER = "aorts-controller"


@pytest.fixture(scope="module")
def harness():
    h = Harness()
    h.run_until(420, ambiguous_events() + probe_history(h))
    for i in range(10):
        assert (
            h.post(
                "status-check-results", status_result(f"P{i:03d}", f"CK-P{i}", t=111.0 + i)
            ).status_code
            == 204
        )
    return h


def views(h, ids, **override):
    out = []
    for txn in ids:
        p = h.core._txn_twin.snapshot(EXP, txn)
        data = dict(
            transaction_id=txn,
            sender_account_id="ACC-S",
            beneficiary_account_id="ACC-B",
            remitter_bank_id=p.remitter_bank_id,
            beneficiary_bank_id=p.beneficiary_bank_id,
            route_id=p.route_id,
            amount_paise=p.amount_minor,
            currency="INR",
            state="AMBIGUOUS",
            financial_outcome="UNKNOWN",
            workflow_state="AMBIGUOUS",
            version=p.version,
            idempotency_key=f"idem-{txn}",
            checks_used=p.checks_used,
            created_at=p.initiated_at,
            updated_at=p.ambiguity_created_at,
            deadline_at=p.deadline_at,
            next_eligible_at=p.initiated_at + timedelta(seconds=POLICY.first_check_delay_s),
        )
        data.update(override.get(txn, {}))
        out.append(TransactionView(**data))
    return out


def client_for(h, policy=POLICY, transport=None):
    http = (
        TestClient(create_app(h.core, TOKEN))
        if transport is None
        else httpx.Client(transport=transport, base_url="http://controller")
    )
    return V11ControllerClient(
        http, token=TOKEN, controller_id=CONTROLLER, experiment_id=EXP, timing_policy=policy
    )


ALL = [f"T{i:03d}" for i in range(40)]


# ------------------------------------------------------------- what Track B emits today


def test_trackb_telemetry_cannot_feed_the_route_twin_without_v11_metadata():
    observation = json.loads((FIXTURES / "telemetry_event.json").read_text())
    assessment = {
        "event_id": observation["event_id"],
        "route_id": observation["route_id"],
        "verdict": "TRUSTED",
    }
    result = adapt_validated_telemetry(observation, assessment, None)
    assert isinstance(result, Incompatible)
    assert any(f.startswith("boundary.") for f in result.missing_fields)


def test_trackb_events_cannot_feed_the_transaction_twin_without_v11_metadata():
    event = json.loads((FIXTURES / "ambiguous_transaction.json").read_text())
    result = adapt_outbox_event(event, None)
    assert isinstance(result, Incompatible)


# ------------------------------------------------------------- v1.1 client <-> controller


def test_v11_client_admits_the_controllers_eligible_selection(harness):
    h = harness
    exchange = client_for(h).decide("A1-B", views(h, ALL), now=h.clock.now(), rate_allowance=100)
    plan = exchange.plan
    assert exchange.http_status == 200 and plan.outcome is ControllerOutcome.DECISION
    assert plan.admitted and set(plan.admitted) <= set(ALL)
    assert not exchange.backend_fallback_permitted


def test_gate_rejects_stale_version_and_terminal_rows(harness):
    h = harness
    first = client_for(h).decide("A1-B", views(h, ALL), now=h.clock.now(), rate_allowance=100)
    a, b = json.loads(first.raw)["selected_transaction_ids"][:2]
    override = {a: {"version": 3}, b: {"state": "CONFIRMED_SUCCESS"}}
    exchange = client_for(h).decide(
        "A1-B", views(h, ALL, **override), now=h.clock.now(), rate_allowance=100
    )
    refused = dict(exchange.plan.refused)
    assert refused.get(a) == "STALE_VERSION" and refused.get(b) == "TERMINAL"


def test_explicit_fail_closed_is_honoured_with_no_fallback(harness):
    h = harness
    lag = (SourceSequenceMark(source_id="src-A1-B", sequence=10**6),)
    exchange = client_for(h).decide(
        "A1-B", views(h, ALL), now=h.clock.now(), rate_allowance=100, watermark=lag
    )
    assert exchange.plan.outcome is ControllerOutcome.EXPLICIT_FAIL_CLOSED
    assert exchange.plan.admitted == () and not exchange.backend_fallback_permitted


def test_expired_decision_is_refused(harness):
    h = harness
    later = h.clock.now() + timedelta(seconds=30)
    exchange = client_for(h).decide(
        "A1-B", views(h, ALL), now=h.clock.now(), rate_allowance=100, gate_time=later
    )
    assert exchange.plan.outcome is ControllerOutcome.DECISION_EXPIRED
    assert exchange.plan.admitted == ()


def test_wrong_route_rows_and_policy_mismatch_are_refused(harness):
    h = harness
    moved = {t: {"route_id": "A1-C"} for t in ALL}
    exchange = client_for(h).decide(
        "A1-B", views(h, ALL, **moved), now=h.clock.now(), rate_allowance=100
    )
    assert exchange.plan.admitted == ()
    assert {r for _, r in exchange.plan.refused} == {"WRONG_ROUTE"}
    other = client_for(h, policy=make_policy(policy_id="trackb-other-policy"))
    mismatch = other.decide("A1-B", views(h, ALL), now=h.clock.now(), rate_allowance=100)
    assert mismatch.plan.outcome is ControllerOutcome.TIMING_POLICY_MISMATCH


def test_controller_unavailable_is_distinct_from_refusal(harness):
    def down(_request):
        raise httpx.ConnectError("connection refused")

    exchange = client_for(harness, transport=httpx.MockTransport(down)).decide(
        "A1-B", [], now=harness.clock.now(), rate_allowance=100
    )
    assert exchange.plan.outcome is ControllerOutcome.CONTROLLER_UNAVAILABLE
    assert exchange.backend_fallback_permitted  # Track B's own labelled fallback may run


def test_duplicate_status_result_has_no_second_effect_in_the_controller(harness):
    h = harness
    again = h.post("status-check-results", status_result("P000", "CK-P0", t=111.0))
    assert again.status_code == 200 and again.json()["error_code"] == "DUPLICATE_EVENT"


# ------------------------------------------------------------- legacy v1 path (labelled)


def test_trackb_legacy_http_controller_works_against_the_legacy_endpoint(harness):
    app = create_app(harness.core, TOKEN)
    legacy = HttpController("http://controller", client=TestClient(app), experiment_id=EXP)
    now = datetime.now(UTC)
    request = RecoveryDecisionRequest(
        decision_request_id="req-1",
        timestamp=now,
        routes=[
            {
                "route_id": "A1-B",
                "measured": {"workers": 10, "latency_p50_ms": 100.0},
                "integrity": {"verdict": "TRUSTED", "confidence": 0.9},
            }
        ],
        candidates=[
            {"route_id": "A1-B", "transaction_id": "T000", "ambiguous_since": now.isoformat()}
        ],
    )
    decision = legacy.decide(request)
    assert decision.policy == "external-controller (contracts/v1)"
    reason = decision.predicted["A1-B"]["decision_reason"]
    assert "fallback" in reason  # the starter path, never reported as the AORTS optimizer
