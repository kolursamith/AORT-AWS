"""Track B's team-contract (contracts/v1) surface and its shared-contract controller adapter.

The controller side is a STUB defined here from the shared contract only (docs/API_CONTRACTS.md,
contracts/v1); no other owner's package is imported or run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from backend.aorts.adapters.controller_client import HttpController, ResilientController
from backend.aorts.adapters.twin_state import LocalVersionedStore
from backend.aorts.banking.dispatcher.local_adapter import LocalQueue
from backend.aorts.banking.dispatcher.service import Dispatcher
from backend.aorts.contracts.requests import DefenseConfig
from contracts.v1.models import RecoveryDecision, StatusCheckResult, TransactionEvent
from tests.trackb.conftest import count, pay


def v1_pay(st, *, key=None, bank="A1", route="A1-B", amount=2500, **extra):
    body = {
        "bank_id": bank,
        "route_id": route,
        "amount_minor": amount,
        "currency": "INR",
        "idempotency_key": key or f"v1-{uuid.uuid4().hex}",
        **extra,
    }
    return st.client.post("remitter", "/v1/payments", body, as_identity="CHANNEL")


def test_v1_payment_returns_contract_transaction_event_and_is_idempotent(stack):
    r = v1_pay(stack, key="v1-idem-000001", experiment_id="exp-v1")
    assert r.status_code == 202, r.text
    ev = TransactionEvent.model_validate(r.json())  # strict shared-contract validation
    assert (
        ev.financial_outcome == "credited"
        and ev.workflow_state == "resolved"
        and ev.experiment_id == "exp-v1"
    )
    again = v1_pay(stack, key="v1-idem-000001", experiment_id="exp-v1")
    assert again.json()["transaction_id"] == ev.transaction_id
    assert (
        count(stack.db, "SELECT count(*) FROM financial_operations WHERE operation_type='DEBIT'")
        == 1
    )
    got = stack.client.get("remitter", f"/v1/payments/{ev.transaction_id}", as_identity="CHANNEL")
    assert TransactionEvent.model_validate(got.json()).version == ev.version


def test_v1_payment_validation_and_auth(stack):
    assert v1_pay(stack, bank="A2", route="A1-B").status_code == 422  # route must start with bank
    body = {"bank_id": "A1", "route_id": "A1-B", "amount_minor": 1, "currency": "INR"}
    assert (
        stack.client.post("remitter", "/v1/payments", body, as_identity="CHANNEL").status_code
        == 422
    )
    assert stack.remitter.post("/v1/payments", json=body).status_code == 401  # unsigned


def test_v1_lost_response_stays_unknown_then_status_check_is_authoritative(stack):
    t = pay(stack, simulate="lost-response").json()["transaction"]
    ev = stack.client.get(
        "remitter", f"/v1/payments/{t['transaction_id']}", as_identity="CHANNEL"
    ).json()
    assert (
        ev["financial_outcome"] == "unknown" and ev["workflow_state"] == "ambiguous"
    )  # never "failed"
    body = {"transaction_id": t["transaction_id"], "route_id": t["route_id"], "check_id": "chk-1"}
    r = stack.client.post(
        "switch", "/v1/status-checks", body, as_identity="A1", headers={"x-bank-id": "A1"}
    )
    res = StatusCheckResult.model_validate(r.json())
    assert res.outcome == "credited" and res.authoritative and res.evidence_source_id == "SWITCH"


def test_v1_status_check_binds_bank_identity_and_is_rate_limited(stack_factory):
    st = stack_factory(bank_check_rate_per_sec=1, bank_check_burst=2)
    t = pay(st, simulate="lost-response").json()["transaction"]
    body = {"transaction_id": t["transaction_id"], "route_id": t["route_id"], "check_id": "chk-x"}
    spoof = st.client.post(
        "switch", "/v1/status-checks", body, as_identity="A3", headers={"x-bank-id": "A1"}
    )
    assert spoof.status_code == 403
    codes = [
        st.client.post(
            "switch", "/v1/status-checks", body, as_identity="A1", headers={"x-bank-id": "A1"}
        ).status_code
        for _ in range(5)
    ]
    assert codes.count(200) == 2 and codes.count(429) == 3


def test_v1_unknown_outcome_is_never_authoritative(stack_factory):
    st = stack_factory()
    st.set_defenses(DefenseConfig(rate_limiting=False, tier2_credits=False))
    body = {"transaction_id": "TXN-NEVER-SEEN", "route_id": "A1-B", "check_id": "chk-2"}
    r = st.client.get(
        "beneficiary:B", "/v1/mock/status/TXN-NEVER-SEEN?route_id=A1-B", as_identity="SWITCH"
    )
    res = StatusCheckResult.model_validate(r.json())
    assert (
        res.outcome == "unknown" and res.authoritative is False and res.evidence_source_id is None
    )
    del body


def test_v1_beneficiary_evidence_is_authoritative_after_credit(stack):
    t = pay(stack).json()["transaction"]
    r = stack.client.get(
        "beneficiary:B",
        f"/v1/mock/status/{t['transaction_id']}?route_id=A1-B",
        as_identity="SWITCH",
    )
    res = StatusCheckResult.model_validate(r.json())
    assert (
        res.outcome == "credited"
        and res.authoritative
        and res.evidence_source_id == "beneficiary-B"
    )
    assert (
        stack.client.get(
            "beneficiary:B", f"/v1/mock/status/{t['transaction_id']}", as_identity="A1"
        ).status_code
        == 403
    )


def test_v1_events_and_telemetry_on_control(stack_factory):
    st = stack_factory(with_control=True)
    pay(st)
    pay(st, simulate="lost-response")
    r = st.client.get("control", "/v1/events?after_id=0", as_identity="CONTROLLER")
    events = [TransactionEvent.model_validate(e) for e in r.json()]
    assert len(events) >= 4 and int(r.headers["X-Next-After-Id"]) > 0
    assert {"payment_initiated", "payment_timeout", "status_resolved"} <= {
        e.event_type for e in events
    }
    obs = {
        "experiment_id": "exp-v1",
        "route_id": "A1-B",
        "timestamp": datetime.now(UTC).isoformat(),
        "payment_rps": 2.0,
        "check_rps": 0.5,
        "observed_throughput_rps": 900.0,
        "p95_latency_ms": 5.0,
        "timeout_rate": 0.0,
        "queue_depth": 0,
        "source_id": "twin-probe-1",
        "sequence": 1,
    }
    assert st.client.post("control", "/v1/telemetry", obs, as_identity="A3").status_code == 403
    verdict = st.client.post("control", "/v1/telemetry", obs, as_identity="twin-probe-1").json()
    assert verdict["verdict"] != "TRUSTED"  # uncorroborated optimistic claim is not believed


# ---------------------------------------------------------------- shared-contract controller adapter
class _DecisionRequest(BaseModel):
    experiment_id: str
    route_id: str
    estimated_capacity_rps: float
    confidence: float
    pending_transaction_ids: list[str]


def stub_controller(mode: str = "ok") -> TestClient:
    """A minimal controller built only from the shared contract (not another owner's code)."""
    app = FastAPI()
    seen: list[dict] = []

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "stub-controller"}

    @app.post("/v1/recovery/decisions")
    def decide(req: _DecisionRequest):
        seen.append(req.model_dump())
        ids = list(req.pending_transaction_ids)
        if mode == "foreign":
            ids = [*ids, "TXN-NOT-OFFERED"]
        expires = datetime.now(UTC) + (
            timedelta(seconds=-1) if mode == "expired" else timedelta(seconds=5)
        )
        return RecoveryDecision(
            experiment_id=req.experiment_id,
            route_id=req.route_id,
            decision_id=f"d-{uuid.uuid4().hex}",
            budget_rps=4.0,
            selected_transaction_ids=ids,
            predicted_delay_ms=10.0,
            predicted_new_ambiguous=0,
            decision_reason="stub",
            expires_at=expires,
        ).model_dump(mode="json")

    client = TestClient(app)
    client.seen = seen
    return client


def dispatcher_with(st, controller):
    return Dispatcher(
        st.contexts["remitter"],
        st.remitter_service,
        controller=ResilientController(controller, 2),
        twin=LocalVersionedStore(),
        queue=LocalQueue(2),
    )


def test_shared_contract_controller_drives_recovery(stack):
    for i in range(3):
        pay(stack, sender=f"A1-CUST-000{i + 1}", simulate="lost-response")
    stub = stub_controller()
    ctrl = HttpController("http://stub", client=stub, tick_s=1.0, experiment_id="exp-v1")
    assert ctrl.health()["status"] == "ok"
    d = dispatcher_with(stack, ctrl)
    rec = d.tick(inline=True)
    assert rec["fallback"] is False and rec["policy"].startswith("external-controller")
    sent = stub.seen[0]
    assert (
        sent["route_id"] == "A1-B"
        and len(sent["pending_transaction_ids"]) == 3
        and 0 <= sent["confidence"] <= 1
    )
    for _ in range(5):
        if count(stack.db, "SELECT count(*) FROM transactions WHERE state='CLOSED'") == 3:
            break
        d.tick(inline=True)
    assert count(stack.db, "SELECT count(*) FROM transactions WHERE state='CLOSED'") == 3
    assert stack.ledger.verify_ledger()["ok"]


def test_expired_or_foreign_decisions_fall_back(stack):
    for i, (mode, why) in enumerate((("expired", "ValueError"), ("foreign", "ValueError"))):
        # each mode gets its own ambiguous payment: the fallback budget still recovers the previous one
        pay(stack, sender=f"A1-CUST-000{i + 1}", simulate="lost-response")
        ctrl = HttpController("http://stub", client=stub_controller(mode), tick_s=1.0)
        rec = dispatcher_with(stack, ctrl).tick(inline=True)
        assert rec["fallback"] is True and why in rec["fallback_reason"], (mode, rec)
