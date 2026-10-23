"""F. Integration: ambiguous payments resolved by the dispatcher through real signed status checks."""

from __future__ import annotations

import httpx
import pytest

from backend.aorts.adapters.controller_client import HttpController, ResilientController
from backend.aorts.adapters.twin_state import LocalVersionedStore
from backend.aorts.banking.dispatcher.local_adapter import LocalQueue
from backend.aorts.banking.dispatcher.service import Dispatcher
from backend.aorts.contracts.requests import FaultConfig
from tests.trackb.conftest import OPENING, balance, count, pay


def make_dispatcher(st, controller=None):
    ctx = st.contexts["remitter"]
    return Dispatcher(
        ctx,
        st.remitter_service,
        controller=controller,
        twin=LocalVersionedStore(),
        queue=LocalQueue(workers=1),
    )


def test_e8_lost_response_resolved_as_success_by_status_check(stack):
    txn = pay(stack, amount=6_000, simulate="lost-response").json()["transaction"]
    assert txn["state"] == "AMBIGUOUS"
    record = make_dispatcher(stack).tick(inline=True)
    assert record["dispatched"] == 1
    t = stack.client.get(
        "remitter", f"/api/v1/payments/{txn['transaction_id']}", as_identity="CHANNEL"
    ).json()
    assert t["state"] == "CLOSED" and t["financial_outcome"] == "SUCCESS" and t["checks_used"] == 1
    assert balance(stack.db, "B-CUST-0001") == 6_000  # credited exactly once
    assert (
        count(stack.db, "SELECT count(*) FROM financial_operations WHERE operation_type='REVERSAL'")
        == 0
    )
    assert stack.ledger.verify_ledger()["ok"]


def test_never_received_payment_is_fenced_then_reversed_once(stack):
    txn = pay(stack, amount=8_000, simulate="timeout").json()["transaction"]
    make_dispatcher(stack).tick(inline=True)
    t = stack.ledger.eligibility(txn["transaction_id"], "STATUS_CHECK")
    assert t.current_state == "REVERSED"
    assert balance(stack.db, "A1-CUST-0001") == OPENING
    # A late /pay for the fenced transaction cannot be processed by the switch.
    late = stack.client.post(
        "switch",
        "/api/v1/pay",
        {
            "transaction_id": txn["transaction_id"],
            "remitter_bank_id": "A1",
            "beneficiary_bank_id": "B",
            "beneficiary_account_id": "B-CUST-0001",
            "route_id": "A1-B",
            "amount_paise": 8_000,
        },
        as_identity="A1",
    )
    assert late.json()["status"] == "FAILED"
    assert balance(stack.db, "B-CUST-0001") == 0


def test_beneficiary_response_lost_after_credit_resolves_as_success(stack):
    st = stack
    st.set_fault(FaultConfig(target="beneficiary:B", drop_response_rate=1.0))
    txn2 = pay(st, amount=7_000, sender="A1-CUST-0004", beneficiary="B-CUST-0004").json()[
        "transaction"
    ]
    assert (
        txn2["state"] == "AMBIGUOUS"
    )  # credit committed, response lost -> switch PENDING -> remitter AMBIGUOUS
    st.reset_faults()
    make_dispatcher(st).tick(inline=True)
    assert st.ledger.eligibility(txn2["transaction_id"], "STATUS_CHECK").current_state == "CLOSED"
    assert (
        balance(st.db, "B-CUST-0004") == 7_000 and balance(st.db, "A1-CUST-0004") == OPENING - 7_000
    )
    assert st.ledger.verify_ledger()["ok"]


def test_route_isolation_bank_b_degraded_bank_c_healthy(stack):
    stack.set_fault(FaultConfig(target="beneficiary:B", unavailable=True))
    b = pay(stack, beneficiary="B-CUST-0005").json()["transaction"]
    c = pay(stack, beneficiary="C-CUST-0005", bank="C").json()["transaction"]
    assert b["state"] == "AMBIGUOUS"
    assert c["state"] == "CLOSED" and balance(stack.db, "C-CUST-0005") == 10_000


def test_deadline_escalates_instead_of_inventing_outcome(stack_factory):
    st = stack_factory(deadline_seconds=1)
    st.set_fault(FaultConfig(target="beneficiary:B", unavailable=True))
    txn = pay(st).json()["transaction"]
    import time

    time.sleep(1.2)
    rec = make_dispatcher(st).tick(inline=True)
    assert rec["escalated"] == 1
    e = st.ledger.eligibility(txn["transaction_id"], "STATUS_CHECK")
    assert e.current_state == "HANDOFF_UDIR"
    assert balance(st.db, "A1-CUST-0001") == OPENING - 10_000  # funds held, not blindly reversed


def test_rate_limited_check_does_not_consume_allowance(stack_factory):
    st = stack_factory(tier2_credit_cap=1, tier2_credit_refill_per_sec=0.001)
    t1 = pay(st, simulate="lost-response").json()["transaction"]
    t2 = pay(st, simulate="lost-response", sender="A1-CUST-0002").json()["transaction"]
    make_dispatcher(st).tick(inline=True)
    states = {
        t: st.ledger.eligibility(t, "STATUS_CHECK")
        for t in (t1["transaction_id"], t2["transaction_id"])
    }
    closed = [t for t, e in states.items() if e.current_state == "CLOSED"]
    waiting = [t for t, e in states.items() if e.current_state == "AMBIGUOUS"]
    assert len(closed) == 1 and len(waiting) == 1
    view = st.client.get("remitter", f"/api/v1/payments/{waiting[0]}", as_identity="CHANNEL").json()
    assert view["checks_used"] == 0  # CREDIT_EXHAUSTED rejection did not count as a completed check


class _BrokenTransport(httpx.BaseTransport):
    def handle_request(self, request):
        raise httpx.ConnectError("controller down")


def test_controller_unavailable_uses_conservative_fallback(stack):
    for i in range(6):
        pay(stack, simulate="lost-response", sender=f"A1-CUST-000{i + 1}")
    broken = HttpController(
        "http://controller:8001",
        client=httpx.Client(base_url="http://controller:8001", transport=_BrokenTransport()),
    )
    rec = make_dispatcher(stack, ResilientController(broken, fallback_budget=2)).tick(inline=True)
    assert rec["fallback"] is True and "ConnectError" in rec["fallback_reason"]
    assert rec["dispatched"] == 2  # fallback budget, not "unlimited"
    assert stack.ledger.verify_ledger()["ok"]


@pytest.mark.slow
def test_duplicate_dispatch_job_is_harmless(stack):
    txn = pay(stack, simulate="lost-response").json()["transaction"]
    d = make_dispatcher(stack)
    d.tick()  # enqueue only
    job = d.queue._q.queue[0]
    d.queue.drain(d.handle_job)
    d.handle_job(job)  # SQS-style redelivery of the same job
    assert stack.ledger.eligibility(txn["transaction_id"], "STATUS_CHECK").current_state == "CLOSED"
    assert d.ctx.metrics.counters.get("dispatch_stale_jobs{reason=STALE_VERSION}", 0) == 1
