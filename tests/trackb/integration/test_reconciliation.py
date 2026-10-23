"""Escalated (HANDOFF_UDIR) transactions resolved by reconciliation; quarantine release."""

from __future__ import annotations

import time

import pytest

from backend.aorts.adapters.twin_state import LocalVersionedStore
from backend.aorts.banking.dispatcher.local_adapter import LocalQueue
from backend.aorts.banking.dispatcher.service import Dispatcher
from backend.aorts.contracts.requests import FaultConfig
from tests.trackb.conftest import ADMIN_TOKEN, OPENING, balance, count, pay


def escalate(st, **fault):
    """Make a payment ambiguous, let its simulated deadline pass, escalate it."""
    st.set_fault(FaultConfig(target="beneficiary:B", **fault))
    txn = pay(st, amount=6_000).json()["transaction"]
    assert txn["state"] == "AMBIGUOUS"
    time.sleep(1.2)
    d = Dispatcher(
        st.contexts["remitter"],
        st.remitter_service,
        twin=LocalVersionedStore(),
        queue=LocalQueue(1),
    )
    d.tick(inline=True)
    st.reset_faults()
    assert (
        st.ledger.eligibility(txn["transaction_id"], "STATUS_CHECK").current_state == "HANDOFF_UDIR"
    )
    return txn, d


def reconcile(st, txn_id, identity="DISPATCHER"):
    return st.client.post(
        "remitter", f"/api/v1/payments/{txn_id}/reconcile", {}, as_identity=identity
    )


def test_escalated_never_credited_payment_is_reconciled_as_reversed_once(stack_factory):
    st = stack_factory(deadline_seconds=1)
    txn, _ = escalate(st, unavailable=True)
    assert (
        balance(st.db, "A1-CUST-0001") == OPENING - 6_000
    )  # held while escalated, never blindly reversed
    r = reconcile(st, txn["transaction_id"]).json()
    assert r["resolved"] is True and r["result"]["state"] == "RESOLVED_BY_RECONCILIATION"
    assert r["result"]["financial_outcome"] == "REVERSED"
    assert balance(st.db, "A1-CUST-0001") == OPENING and balance(st.db, "B-CUST-0001") == 0
    again = reconcile(st, txn["transaction_id"])
    assert again.status_code == 409  # already resolved: no second effect
    assert (
        count(st.db, "SELECT count(*) FROM financial_operations WHERE operation_type='REVERSAL'")
        == 1
    )
    assert st.ledger.verify_ledger()["ok"]


def test_escalated_but_actually_credited_payment_is_reconciled_as_success(stack_factory):
    st = stack_factory(deadline_seconds=1, tier2_credit_cap=1, tier2_credit_refill_per_sec=0.001)
    # Burn the only Tier 2 credit so normal status checks are refused -> the txn escalates unresolved.
    pay(st, sender="A1-CUST-0005", beneficiary="B-CUST-0007", simulate="lost-response")
    txn, d = escalate(st, drop_response_rate=1.0)
    assert balance(st.db, "B-CUST-0001") == 6_000  # the beneficiary did credit it
    # Reconciliation has its own low-rate channel, independent of the exhausted Tier 2 credits.
    r = reconcile(st, txn["transaction_id"]).json()
    assert r["resolved"] is True and r["result"]["financial_outcome"] == "SUCCESS"
    assert balance(st.db, "A1-SETTLEMENT") >= 6_000 and balance(st.db, "A1-SUSPENSE") >= 0
    assert st.ledger.verify_ledger()["ok"]


def test_reconcile_requires_escalated_state_and_role(stack):
    txn = pay(stack).json()["transaction"]
    assert reconcile(stack, txn["transaction_id"]).status_code == 409
    assert reconcile(stack, txn["transaction_id"], identity="CHANNEL").status_code == 403


def test_reconciliation_channel_is_rate_limited(stack_factory):
    st = stack_factory(reconciliation_rate_per_sec=0.01, reconciliation_burst=1)
    body = {"transaction_id": "TXN-NOPE", "bank_id": "A1", "route_id": "A1-B"}
    first = st.client.post("switch", "/api/v1/reconciliation/status", body, as_identity="A1")
    second = st.client.post("switch", "/api/v1/reconciliation/status", body, as_identity="A1")
    assert first.status_code == 200 and second.status_code == 429


@pytest.mark.parametrize("escalate_flag,expected", [(False, "AMBIGUOUS"), (True, "HANDOFF_UDIR")])
def test_quarantine_release_is_admin_only(stack, escalate_flag, expected):
    txn = pay(stack, simulate="lost-response").json()["transaction"]
    stack.ledger.quarantine(txn["transaction_id"], "conflicting evidence", actor="SECURITY")
    path = f"/api/v1/payments/{txn['transaction_id']}/release-quarantine"
    body = {"reason": "reviewed by operator", "escalate": escalate_flag}
    assert stack.remitter.post(path, json=body).status_code == 403
    r = stack.remitter.post(path, json=body, headers={"X-AORTS-Admin-Token": ADMIN_TOKEN})
    assert r.status_code == 200 and r.json()["state"] == expected
