"""A. API tests + B. ledger tests for the remitter -> switch -> beneficiary workflow."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

from backend.aorts.contracts.requests import FaultConfig
from tests.trackb.conftest import OPENING, balance, count, pay


def test_successful_payment_debits_once_and_credits_beneficiary(stack):
    r = pay(stack, amount=12_345)
    assert r.status_code == 200, r.text
    txn = r.json()["transaction"]
    assert txn["state"] == "CLOSED"
    assert txn["financial_outcome"] == "SUCCESS"
    assert balance(stack.db, "A1-CUST-0001") == OPENING - 12_345
    assert balance(stack.db, "B-CUST-0001") == 12_345
    assert balance(stack.db, "A1-SUSPENSE") == 0
    assert balance(stack.db, "A1-SETTLEMENT") == 12_345
    assert stack.ledger.verify_ledger()["ok"]


def test_payment_to_unknown_beneficiary_account_fails_and_is_reversed(stack):
    r = pay(stack, beneficiary="B-CUST-9999", amount=5_000)
    txn = r.json()["transaction"]
    assert txn["state"] == "REVERSED" and txn["financial_outcome"] == "REVERSED"
    assert balance(stack.db, "A1-CUST-0001") == OPENING
    assert (
        count(stack.db, "SELECT count(*) FROM financial_operations WHERE operation_type='REVERSAL'")
        == 1
    )
    assert stack.ledger.verify_ledger()["ok"]


def test_invalid_payment_rejected_with_structured_error(stack):
    body = {
        "sender_account_id": "A1-CUST-0001",
        "beneficiary_account_id": "B-CUST-0001",
        "beneficiary_bank_id": "B",
        "amount_paise": 10.5,
        "idempotency_key": "idem-float-amount",
    }
    r = stack.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL")
    assert r.status_code == 422
    assert r.json()["error_code"] == "VALIDATION_ERROR"
    body.update(amount_paise=-5)
    assert (
        stack.client.post("remitter", "/api/v1/payments", body, as_identity="CHANNEL").status_code
        == 422
    )


def test_insufficient_funds_rejected_without_ledger_effect(stack):
    r = pay(stack, amount=OPENING + 1)
    assert r.status_code == 422 and r.json()["error_code"] == "INSUFFICIENT_FUNDS"
    assert count(stack.db, "SELECT count(*) FROM transactions") == 0
    assert balance(stack.db, "A1-CUST-0001") == OPENING


def test_invalid_route_rejected(stack):
    r = pay(stack, bank="Z")
    # Unknown beneficiary bank: debit happens, switch refuses route -> no evidence -> AMBIGUOUS, never "failed".
    assert r.status_code == 200
    assert r.json()["transaction"]["state"] == "AMBIGUOUS"


def test_duplicate_idempotency_key_does_not_debit_twice(stack):
    key = f"idem-{uuid.uuid4().hex}"
    first = pay(stack, key=key, amount=7_000)
    second = pay(stack, key=key, amount=7_000)
    assert (
        first.json()["transaction"]["transaction_id"]
        == second.json()["transaction"]["transaction_id"]
    )
    assert second.json()["replayed"] is True
    assert balance(stack.db, "A1-CUST-0001") == OPENING - 7_000
    assert (
        count(stack.db, "SELECT count(*) FROM financial_operations WHERE operation_type='DEBIT'")
        == 1
    )


def test_idempotency_key_with_different_body_is_conflict(stack):
    key = f"idem-{uuid.uuid4().hex}"
    pay(stack, key=key, amount=7_000)
    r = pay(stack, key=key, amount=8_000)
    assert r.status_code == 409 and r.json()["error_code"] == "IDEMPOTENCY_CONFLICT"


def test_concurrent_identical_payment_requests_debit_once(stack):
    key = f"idem-{uuid.uuid4().hex}"
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: pay(stack, key=key, amount=3_000), range(8)))
    ids = {r.json()["transaction"]["transaction_id"] for r in results}
    assert len(ids) == 1, [r.text for r in results]
    assert balance(stack.db, "A1-CUST-0001") == OPENING - 3_000
    assert (
        count(stack.db, "SELECT count(*) FROM financial_operations WHERE operation_type='DEBIT'")
        == 1
    )


def test_lost_response_makes_payment_ambiguous_not_failed(stack):
    r = pay(stack, amount=4_000, simulate="lost-response")
    txn = r.json()["transaction"]
    assert txn["state"] == "AMBIGUOUS"
    assert txn["financial_outcome"] == "UNKNOWN"  # rule C: a lost response is not a failure
    # The beneficiary WAS credited even though the remitter never heard back.
    assert balance(stack.db, "B-CUST-0001") == 4_000
    assert (
        count(stack.db, "SELECT count(*) FROM financial_operations WHERE operation_type='REVERSAL'")
        == 0
    )


def test_switch_unavailable_leaves_transaction_ambiguous(stack):
    stack.set_fault(FaultConfig(target="switch", unavailable=True))
    r = pay(stack, amount=1_000)
    assert r.json()["transaction"]["state"] == "AMBIGUOUS"
    assert balance(stack.db, "A1-CUST-0001") == OPENING - 1_000  # debit held, not reversed blindly


def test_unsigned_payment_request_rejected(stack):
    r = stack.remitter.post("/api/v1/payments", json={"sender_account_id": "A1-CUST-0001"})
    assert r.status_code == 401 and r.json()["error_code"] == "AUTH_MISSING"


def test_simulation_header_requires_admin(stack):
    r = pay(stack, simulate="lost-response", admin=False)
    assert r.status_code == 403


def test_history_is_paginated(stack):
    txn_id = pay(stack).json()["transaction"]["transaction_id"]
    r = stack.client.get(
        "remitter", f"/api/v1/payments/{txn_id}/history?limit=2", as_identity="CHANNEL"
    )
    body = r.json()
    assert len(body["items"]) == 2 and body["next_after_id"] is not None
    states = [i["new_state"] for i in body["items"]]
    assert states[0] == "DEBITED"


def test_audit_chain_verifies(stack):
    from backend.aorts.security.audit import verify_chain

    pay(stack)
    pay(stack, beneficiary="B-CUST-9999")
    stack.flush_audit()
    with stack.db.session() as s:
        result = verify_chain(s)
    assert result["ok"] and result["checked"] > 5


def test_concurrent_different_payments_from_same_sender_do_not_deadlock(stack):
    """Regression: FK KEY SHARE (txn insert) + FOR UPDATE (debit) on the same sender used to deadlock."""
    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(lambda i: pay(stack, amount=1_000 + i), range(24)))
    assert all(r.status_code == 200 for r in results), [
        r.text[:200] for r in results if r.status_code != 200
    ]
    total = sum(1_000 + i for i in range(24))
    assert balance(stack.db, "A1-CUST-0001") == OPENING - total
    assert stack.ledger.verify_ledger()["ok"]


def test_mixed_concurrent_debits_settlements_reversals_same_senders(stack):
    """Stress: debit, settle and reversal paths contending on the same sender/suspense accounts."""
    from backend.aorts.adapters.twin_state import LocalVersionedStore
    from backend.aorts.banking.dispatcher.local_adapter import LocalQueue
    from backend.aorts.banking.dispatcher.service import Dispatcher

    def job(i):
        sender = f"A1-CUST-000{(i % 3) + 1}"
        if i % 3 == 0:
            return pay(
                stack, sender=sender, beneficiary="B-CUST-9999", amount=500
            )  # declined -> reversal
        if i % 3 == 1:
            return pay(stack, sender=sender, amount=700, simulate="lost-response")  # ambiguous
        return pay(stack, sender=sender, amount=900)  # success -> settlement

    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(job, range(36)))
    assert all(r.status_code == 200 for r in results), [
        r.text[:200] for r in results if r.status_code != 200
    ]
    d = Dispatcher(
        stack.contexts["remitter"],
        stack.remitter_service,
        twin=LocalVersionedStore(),
        queue=LocalQueue(4),
    )
    pending = "SELECT count(*) FROM transactions WHERE state NOT IN ('CLOSED','REVERSED')"
    for _ in range(12):  # no telemetry -> conservative fallback budget of 2 checks/route/tick
        if count(stack.db, pending) == 0:
            break
        d.tick(inline=True)
    assert count(stack.db, pending) == 0
    assert stack.ledger.verify_ledger()["ok"]
