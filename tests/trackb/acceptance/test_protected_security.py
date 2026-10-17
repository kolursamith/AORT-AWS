"""PROTECTED SECURITY ACCEPTANCE TESTS (Track B requirements, section 6).

Do not delete, skip, loosen or rewrite these assertions to make an implementation pass. If one fails,
the implementation is wrong (or the requirement changed - then change it explicitly, with review).
They run against the real in-process stack and PostgreSQL (``AORTS_REQUIRE_DB=1`` makes a missing
database a failure, not a skip).
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from backend.aorts.adapters.twin_state import LocalVersionedStore
from backend.aorts.app.http_client import dumps
from backend.aorts.contracts.events import new_id
from backend.aorts.contracts.requests import (
    DefenseConfig,
    FaultConfig,
    RecoveryDecisionRequest,
    RouteTelemetry,
)
from backend.aorts.control.telemetry_service import TelemetryService
from backend.aorts.recovery.budget import BudgetConfig
from backend.aorts.recovery.controller import AortsController
from backend.aorts.security.budget_guard import BudgetGuard
from backend.aorts.security.flooding_simulator import FloodConfig, run_flood
from backend.aorts.security.request_auth import sign_request
from backend.aorts.twin.engine import TwinEngine
from tests.trackb.conftest import ADMIN_TOKEN, count, pay

MEAS = {
    "workers": 8,
    "latency_p50_ms": 100.0,
    "latency_p95_ms": 150.0,
    "throughput_tps": 20.0,
    "timeout_rate": 0.0,
    "in_flight": 1,
    "backlog": 0,
    "samples": 50,
}


def _decision_budget(st, measured):
    ctrl = AortsController(TwinEngine(st.db), budget_cfg=BudgetConfig(tick_s=1.0))
    cands = [
        {
            "transaction_id": f"T{i}",
            "route_id": "A1-B",
            "checks_used": 0,
            "ambiguous_since": datetime.now(UTC).isoformat(),
            "deadline_at": datetime.now(UTC).isoformat(),
        }
        for i in range(30)
    ]
    d = ctrl.decide(
        RecoveryDecisionRequest(
            decision_request_id="acc",
            timestamp=datetime.now(UTC),
            routes=[{"route_id": "A1-B", "backlog": 30, "measured": measured}],
            candidates=cands,
        )
    )
    return d.route_budgets["A1-B"], d.predicted["routes"]["A1-B"]


def test_P1_uncorroborated_false_high_capacity_telemetry_cannot_raise_budget(stack):
    clean_budget, _ = _decision_budget(stack, MEAS)
    ts = TelemetryService(
        stack.contexts["remitter"], LocalVersionedStore(), measurement_source=lambda: {}
    )
    for i in range(
        10
    ):  # registered probe, wildly optimistic, nothing independent to corroborate it
        ts.ingest(
            RouteTelemetry(
                event_id=new_id("tel"),
                source_id="twin-probe-2",
                route_id="A1-B",
                sequence=i + 1,
                timestamp=datetime.now(UTC),
                throughput_tps=5000.0,
                latency_p50_ms=1.0,
                latency_p95_ms=2.0,
                timeout_rate=0.0,
                backlog=0,
            ),
            authenticated_source="twin-probe-2",
        )
    assert all(x["verdict"] != "TRUSTED" for x in ts.recent[-10:])
    poisoned_budget, detail = _decision_budget(stack, MEAS)
    assert poisoned_budget <= clean_budget
    assert (
        detail["estimate"]["point_rps"] == 80.0
    )  # capacity still from the switch measurement only
    guard = BudgetGuard(hard_cap=50, fallback_budget=2)
    assert guard.apply("A1-B", 50, telemetry={"verdict": "SUSPICIOUS"}, measured=MEAS).applied <= 2


def test_P2_replayed_or_duplicated_requests_cause_no_duplicate_financial_operation(stack_factory):
    st = stack_factory(auto_reverse_on_confirmed_failure=False)
    # (a) the same payment request replayed byte-for-byte (same nonce) and re-sent with the same key
    body = {
        "sender_account_id": "A1-CUST-0003",
        "beneficiary_account_id": "B-CUST-0003",
        "beneficiary_bank_id": "B",
        "amount_paise": 4321,
        "idempotency_key": f"acc-{uuid.uuid4().hex}",
    }
    raw = dumps(body)
    hdr = {
        "Content-Type": "application/json",
        **sign_request(
            st.client.registry.signing_key_for("CHANNEL"), "POST", "/api/v1/payments", raw
        ),
    }
    assert st.remitter.post("/api/v1/payments", content=raw, headers=hdr).status_code == 200
    assert (
        st.remitter.post("/api/v1/payments", content=raw, headers=hdr).status_code == 401
    )  # replayed nonce
    for _ in range(3):
        st.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL")
    assert (
        count(
            st.db,
            "SELECT count(*) FROM financial_operations WHERE operation_type='DEBIT' "
            "AND transaction_id IN (SELECT transaction_id FROM transactions WHERE idempotency_key=:k)",
            k=body["idempotency_key"],
        )
        == 1
    )
    # (b) a FAILED status event delivered 10x concurrently, then 8 concurrent reversals
    txn = pay(st, sender="A1-CUST-0004", simulate="timeout").json()["transaction"]
    ev = {
        "event_id": new_id("evt"),
        "schema_version": "1.0",
        "timestamp": datetime.now(UTC).isoformat(),
        "transaction_id": txn["transaction_id"],
        "route_id": txn["route_id"],
        "bank_id": "A1",
        "amount_paise": txn["amount_paise"],
        "event_type": "STATUS_RESPONSE",
        "transaction_version": txn["version"],
        "payload": {"status": "FAILED"},
    }
    with ThreadPoolExecutor(10) as pool:
        codes = list(
            pool.map(
                lambda _: st.client.post(
                    "remitter", "/api/v1/events/transaction-state", ev, as_identity="SWITCH"
                ).status_code,
                range(10),
            )
        )
    assert codes.count(200) == 1
    v = st.ledger.eligibility(txn["transaction_id"], "REVERSAL").current_version

    def rev(i):
        return st.client.post(
            "remitter",
            f"/api/v1/payments/{txn['transaction_id']}/reversal",
            {"operation_id": f"OP-{i % 2}", "expected_version": v, "reason": "acceptance"},
            as_identity="CONTROLLER",
        )

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(rev, range(8)))
    assert (
        count(
            st.db,
            "SELECT count(*) FROM financial_operations WHERE transaction_id=:t AND "
            "operation_type='REVERSAL'",
            t=txn["transaction_id"],
        )
        == 1
    )
    assert st.ledger.verify_ledger()["ok"]


def test_P3_status_check_flooding_is_rate_limited(stack_factory):
    st = stack_factory(bank_check_rate_per_sec=5, bank_check_burst=5)
    pool = {}
    for bank in ("A1", "A3"):
        t = pay(st, sender=f"{bank}-CUST-0006", simulate="lost-response").json()["transaction"]
        pool[bank] = [(t["transaction_id"], t["route_id"])]
    s = run_flood(
        st.client,
        pool,
        FloodConfig(
            attack_rps=60, duration_s=1.5, legit_banks=("A1",), legit_rps_per_bank=2, tail_s=0
        ),
    ).summary(bank_limit=5)
    a3 = s["per_bank"]["A3"]
    assert a3["sent"] >= 80
    assert a3["admitted_ok"] <= 5 + 5 * 1.5 + 3  # burst + rate x duration (+ timing slack)
    assert a3["outcomes"].get("RATE_LIMITED", 0) >= a3["sent"] - a3["admitted_ok"] - 3
    assert s["per_bank"]["A1"]["admitted_ratio"] == 1.0


def test_P4_recovery_traffic_cannot_consume_capacity_reserved_for_primary_payments(stack_factory):
    st = stack_factory(
        switch_workers_per_destination=4,
        switch_primary_reserve_fraction=0.5,
        switch_queue_wait_s=0.3,
        switch_beneficiary_timeout_s=2.0,
    )
    st.set_defenses(
        DefenseConfig(rate_limiting=False, tier2_credits=False)
    )  # recovery pressure unthrottled
    st.set_fault(FaultConfig(target="beneficiary:B", drop_response_rate=1.0))
    pending = [
        pay(st, sender=f"A1-CUST-000{(i % 9) + 1}", amount=100 + i).json()["transaction"]
        for i in range(12)
    ]
    st.reset_faults()
    st.set_fault(
        FaultConfig(target="beneficiary:B", latency_ms=250)
    )  # slow downstream: checks hold slots
    stop = threading.Event()
    pays = []

    def primary():
        while not stop.is_set():
            pays.append(
                pay(st, sender="A2-CUST-0005", beneficiary="B-CUST-0008", amount=200).json()[
                    "transaction"
                ]
            )

    th = threading.Thread(target=primary)
    th.start()

    def check(t):
        return st.client.post(
            "switch",
            "/api/v1/check-status",
            {"transaction_id": t["transaction_id"], "bank_id": "A1", "route_id": "A1-B"},
            as_identity="A1",
        )

    with ThreadPoolExecutor(16) as pool:
        list(pool.map(check, pending * 2))
    stop.set()
    th.join()
    dest = st.switch_service.capacity.pool("B")
    assert dest.max_recovery_in_flight >= 2  # the test really pushed recovery up to its cap...
    assert (
        dest.max_recovery_in_flight <= dest.limit_for_recovery(0.5) == 2
    )  # ...but never into the reserved half
    assert pays and all(p["state"] == "CLOSED" for p in pays)  # primary payments kept flowing
    assert st.ledger.verify_ledger()["ok"]


def test_P5_low_confidence_telemetry_activates_conservative_behaviour(stack):
    trusted_budget, _ = _decision_budget(stack, MEAS)
    ts = TelemetryService(
        stack.contexts["remitter"], LocalVersionedStore(), measurement_source=lambda: {}
    )
    for i in range(8):  # rejected samples -> route telemetry reliability collapses
        ts.ingest(
            RouteTelemetry(
                event_id=new_id("tel"),
                source_id="twin-probe-1",
                route_id="A1-B",
                sequence=i + 1,
                timestamp=datetime.now(UTC),
                latency_p50_ms=-1.0,
            ),
            authenticated_source="twin-probe-1",
        )
    budget, detail = _decision_budget(stack, MEAS)
    assert detail["decision"]["mode"] == "fallback" and budget <= 2 and budget <= trusted_budget
    assert detail["estimate"]["confidence"] < 0.6
    assert any("conservative fallback" in r for r in detail["decision"]["reasons"])
    assert ADMIN_TOKEN  # (fixture sanity)
